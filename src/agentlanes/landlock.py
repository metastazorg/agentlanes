"""Linux Landlock: kernel-enforced write lanes for `agentlanes run`, without root.

The ruleset is applied to the launched command and everything it starts (Landlock domains are inherited across
fork/exec and can only get narrower). Policy: read + execute everywhere, write only beneath the allowed paths.

ABI handling follows the kernel's documented best-effort pattern: ask the running kernel for its ABI version and
drop every access right it does not know, so the same binary works from Linux 5.13 (ABI 1) to the newest ABI.
Syscall numbers 444/445/446 are the same on every architecture Linux supports (they were allocated after the
syscall tables were unified), so no per-arch table is needed.
"""
import ctypes
import ctypes.util
import errno
import os
import sys

SYS_CREATE_RULESET, SYS_ADD_RULE, SYS_RESTRICT_SELF = 444, 445, 446
CREATE_RULESET_VERSION = 1 << 0
RULE_PATH_BENEATH, RULE_NET_PORT = 1, 2
PR_SET_NO_NEW_PRIVS = 38

# Filesystem access rights (include/uapi/linux/landlock.h)
FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_REMOVE_DIR = 1 << 4
FS_REMOVE_FILE = 1 << 5
FS_MAKE_CHAR = 1 << 6
FS_MAKE_DIR = 1 << 7
FS_MAKE_REG = 1 << 8
FS_MAKE_SOCK = 1 << 9
FS_MAKE_FIFO = 1 << 10
FS_MAKE_BLOCK = 1 << 11
FS_MAKE_SYM = 1 << 12
FS_REFER = 1 << 13        # ABI 2: link/rename across directories
FS_TRUNCATE = 1 << 14     # ABI 3
FS_IOCTL_DEV = 1 << 15    # ABI 5
NET_BIND_TCP = 1 << 0     # ABI 4
NET_CONNECT_TCP = 1 << 1  # ABI 4
SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0  # ABI 6
SCOPE_SIGNAL = 1 << 1                # ABI 6

FS_V1 = (1 << 13) - 1     # EXECUTE .. MAKE_SYM
FS_READ = FS_EXECUTE | FS_READ_FILE | FS_READ_DIR
FS_FILE_ONLY = FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE | FS_IOCTL_DEV   # rights valid on a non-dir

# Device files a normal tool writes to (terminal, /dev/null, ...). Everything in /dev except shared-memory and
# message-queue directories gets file-level write + ioctl, never create/remove.
DEV_SKIP = {'shm', 'mqueue', 'hugepages'}


def fs_rights(abi):
    """All filesystem rights the given ABI understands (what the ruleset 'handles' = denies unless allowed)."""
    if abi < 1:
        return 0
    r = FS_V1
    if abi >= 2:
        r |= FS_REFER
    if abi >= 3:
        r |= FS_TRUNCATE
    if abi >= 5:
        r |= FS_IOCTL_DEV
    return r


def net_rights(abi):
    return NET_BIND_TCP | NET_CONNECT_TCP if abi >= 4 else 0


def scope_rights(abi):
    return SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL if abi >= 6 else 0


class Unsupported(Exception):
    pass


class _RulesetAttr(ctypes.Structure):
    _fields_ = [('handled_access_fs', ctypes.c_uint64), ('handled_access_net', ctypes.c_uint64),
                ('scoped', ctypes.c_uint64)]


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [('allowed_access', ctypes.c_uint64), ('parent_fd', ctypes.c_int32)]


class _NetPort(ctypes.Structure):
    _fields_ = [('allowed_access', ctypes.c_uint64), ('port', ctypes.c_uint64)]


_libc = None


def _syscall():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(ctypes.util.find_library('c') or None, use_errno=True)
        _libc.syscall.restype = ctypes.c_long
        _libc.prctl.restype = ctypes.c_int
    return _libc


def _call(nr, *args):
    libc = _syscall()
    ctypes.set_errno(0)
    r = libc.syscall(ctypes.c_long(nr), *args)
    if r < 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    return r


def abi_version():
    """Landlock ABI of the running kernel (>= 1), or raise Unsupported with the reason."""
    if not sys.platform.startswith('linux'):
        raise Unsupported('Landlock is a Linux feature (this is %s)' % sys.platform)
    try:
        return _call(SYS_CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint32(CREATE_RULESET_VERSION))
    except OSError as e:
        if e.errno == errno.ENOSYS:
            raise Unsupported('this kernel has no Landlock (needs Linux 5.13+ built with CONFIG_SECURITY_LANDLOCK)')
        if e.errno == errno.EOPNOTSUPP:
            raise Unsupported('Landlock is built in but disabled; add "landlock" to the lsm= boot parameter')
        raise Unsupported('Landlock version query failed: %s' % e)
    except (AttributeError, TypeError) as e:   # no syscall() in this libc
        raise Unsupported('cannot call syscall(): %s' % e)


def describe_abi():
    """One line for `doctor`. Returns (level, text)."""
    try:
        abi = abi_version()
    except Unsupported as e:
        return ('info' if not sys.platform.startswith('linux') else 'warn'), 'Landlock unavailable: %s' % e
    have = ['filesystem writes']
    if abi >= 2:
        have.append('cross-dir rename/link (REFER)')
    if abi >= 3:
        have.append('truncate')
    if abi >= 4:
        have.append('TCP bind/connect')
    if abi >= 5:
        have.append('device ioctl')
    if abi >= 6:
        have.append('signal/abstract-socket scoping')
    return 'ok', 'Landlock ABI %d: `run` can enforce %s' % (abi, ', '.join(have))


class Policy:
    """What to allow. Paths that do not exist are reported in `skipped` (a rule needs an open fd)."""

    def __init__(self, write=(), no_net=False, connect_ports=(), bind_ports=(), isolate=False, dev=True):
        self.write = [os.path.realpath(os.path.expanduser(p)) for p in write]
        self.no_net = no_net or bool(connect_ports) or bool(bind_ports)
        self.connect_ports = sorted({int(p) for p in connect_ports})
        self.bind_ports = sorted({int(p) for p in bind_ports})
        self.isolate = isolate
        self.dev = dev


def plan(policy, abi, listdir=os.listdir, isdir=os.path.isdir, exists=os.path.exists, islink=os.path.islink):
    """Pure function: the ruleset for POLICY on ABI. Returns dict with handled masks, rules and warnings.

    Kept free of syscalls so ABI degradation can be unit-tested on any kernel."""
    fs = fs_rights(abi)
    out = {'abi': abi, 'handled_fs': fs, 'handled_net': 0, 'scoped': 0, 'rules': [], 'net_rules': [],
           'warnings': [], 'skipped': []}

    def add(path, rights):
        if not exists(path):
            out['skipped'].append(path)
            return
        if not isdir(path):
            rights &= FS_FILE_ONLY
        out['rules'].append((path, rights & fs))

    add('/', FS_READ)
    if policy.dev and exists('/dev'):
        for name in sorted(listdir('/dev')):
            if name not in DEV_SKIP and not islink('/dev/' + name):   # stdin/fd/... point into /proc
                add('/dev/' + name, FS_READ | FS_WRITE_FILE | FS_TRUNCATE | FS_IOCTL_DEV)
    for p in policy.write:
        add(p, fs)
    if abi < 2:
        out['warnings'].append('Landlock ABI 1: renaming or hard-linking a file into another directory is always '
                               'denied (EXDEV), even inside allowed paths; tools that do that may fail')
    if abi < 3:
        out['warnings'].append('Landlock ABI %d: truncate() is not restricted (ABI 3+); a file outside the lanes can '
                               'still be emptied with truncate, though not written' % abi)
    if policy.no_net:
        if abi >= 4:
            out['handled_net'] = net_rights(abi)
            out['net_rules'] = ([(p, NET_CONNECT_TCP) for p in policy.connect_ports] +
                                [(p, NET_BIND_TCP) for p in policy.bind_ports])
        else:
            out['warnings'].append('network restriction needs Landlock ABI 4 (Linux 6.7+); this kernel has ABI %d, '
                                   'so TCP is NOT restricted' % abi)
    if policy.isolate:
        if abi >= 6:
            out['scoped'] = scope_rights(abi)
        else:
            out['warnings'].append('--isolate needs Landlock ABI 6 (Linux 6.12+); this kernel has ABI %d, so signals '
                                   'to outside processes are NOT restricted' % abi)
    return out


def summary(p):
    w = [path for path, r in p['rules'] if r & FS_MAKE_REG or r & FS_WRITE_FILE and not path.startswith('/dev/')]
    s = {'abi': p['abi'], 'write': w, 'tcp': 'unrestricted'}
    if p['handled_net']:
        s['tcp'] = 'connect %s; bind %s' % (
            ','.join(str(x) for x, r in p['net_rules'] if r == NET_CONNECT_TCP) or 'none',
            ','.join(str(x) for x, r in p['net_rules'] if r == NET_BIND_TCP) or 'none')
    if p['scoped']:
        s['scoped'] = 'signals+abstract-unix'
    if p['skipped']:
        s['skipped'] = p['skipped']
    return s


def build(p):
    """Create the kernel ruleset for plan P. Returns the ruleset fd (O_CLOEXEC)."""
    abi = p['abi']
    attr = _RulesetAttr(p['handled_fs'], p['handled_net'], p['scoped'])
    size = 8 if abi < 4 else 16 if abi < 6 else 24   # older kernels reject unknown trailing fields
    fd = _call(SYS_CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(size), ctypes.c_uint32(0))
    try:
        for path, rights in p['rules']:
            if not rights:
                continue
            pfd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = _PathBeneath(rights, pfd)
                _call(SYS_ADD_RULE, ctypes.c_int(fd), ctypes.c_int(RULE_PATH_BENEATH), ctypes.byref(rule),
                      ctypes.c_uint32(0))
            except OSError as e:
                raise OSError(e.errno, 'landlock_add_rule(%s): %s' % (path, e.strerror))
            finally:
                os.close(pfd)
        for port, rights in p['net_rules']:
            rule = _NetPort(rights, port)
            _call(SYS_ADD_RULE, ctypes.c_int(fd), ctypes.c_int(RULE_NET_PORT), ctypes.byref(rule), ctypes.c_uint32(0))
    except Exception:
        os.close(fd)
        raise
    return fd


def restrict_self(fd):
    """Enforce ruleset FD on the calling process (and its future children). Safe to call between fork and exec."""
    libc = _syscall()
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        e = ctypes.get_errno()
        raise OSError(e, 'prctl(NO_NEW_PRIVS): ' + os.strerror(e))
    _call(SYS_RESTRICT_SELF, ctypes.c_int(fd), ctypes.c_uint32(0))
