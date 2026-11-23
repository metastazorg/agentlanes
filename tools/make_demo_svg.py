#!/usr/bin/env python3
"""Render docs/demo.svg: an animated terminal replay of a real ./demo.sh run plus a real Claude Code session.

Part 1 runs ./demo.sh for real and records when each output line arrived; the SVG replays those timings.
Part 2 replays examples/landlock/real-session.txt (a recorded Claude Code session under `agentlanes run`).
Its lines are taken verbatim from that file; their spacing is spread over the run's real 6.4 s duration
(from the event log in the same file), so that timing is approximate.

Standard library only. Pure CSS keyframes, no JS, no fonts or requests, so it animates inside a GitHub README.

    python3 tools/make_demo_svg.py                    # runs demo.sh, writes docs/demo.svg (+ docs/demo-recording.json)
    python3 tools/make_demo_svg.py --from-recording   # re-render from docs/demo-recording.json without re-running
    python3 tools/make_demo_svg.py --freeze 14        # static SVG showing the frame at t=14 s (for a poster PNG)
"""
import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COLS, ROWS = 100, 26
FONT, LH = 13, 18.5           # px; char width assumed 0.6 em
CW = FONT * 0.6
PAD_X, PAD_TOP, BAR = 16, 14, 34
WIDTH = int(PAD_X * 2 + COLS * CW)
HEIGHT = int(BAR + PAD_TOP + ROWS * LH + 12)

BG, BAR_BG, FG, DIM = "#0d1117", "#161b22", "#c9d1d9", "#8b949e"
AGENT = {"agent-A": "#58a6ff", "agent-B": "#d2a8ff", "agent-C": "#e3b341", "agent-D": "#ff7b72"}
RED, GREEN, CYAN = "#ff7b72", "#7ee787", "#79c0ff"


def record():
    """Run demo.sh for real; return [(seconds, line)]."""
    work = Path(tempfile.mkdtemp(prefix=".demo-rec-", dir=ROOT.parent))   # not /tmp: may be noexec
    env = dict(os.environ, TMPDIR=str(work), AGENTLANES_AGENT="demo")
    env.pop("AGENTLANES_STATE_DIR", None)
    try:
        p = subprocess.Popen(["bash", str(ROOT / "demo.sh")], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, bufsize=0)
        t0, out, buf = time.monotonic(), [], b""
        while True:
            chunk = p.stdout.read1(4096) if hasattr(p.stdout, "read1") else p.stdout.read(1)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                out.append((round(time.monotonic() - t0, 2), line.decode("utf-8", "replace")))
        if buf:
            out.append((round(time.monotonic() - t0, 2), buf.decode("utf-8", "replace")))
        if p.wait() != 0:
            sys.exit("demo.sh exited %d" % p.returncode)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return [(t, scrub(l, work)) for t, l in out]


def scrub(s, work=None):
    if work:
        s = s.replace(str(work), "$TMPDIR")
    home = os.path.expanduser("~")
    s = s.replace(home, "~")
    return re.sub(r"/home/[^/\s]+", "~", s)


def landlock_lines():
    """Pick the essential lines of the real Landlock session, verbatim (long lines cut with an ellipsis)."""
    src = (ROOT / "examples/landlock/real-session.txt").read_text().splitlines()
    pick = []
    i = 0
    while i < len(src):
        l = src[i]
        if l.startswith("$ agentlanes run"):
            pick.append(("cmd", l))
            pick.append(("cmd", src[i + 1]))
            pick.append(("cmd", src[i + 2]))
            i += 3
            continue
        if l.startswith("agentlanes run: Landlock"):
            pick.append(("info", l))
        elif l.startswith(("[init]", "[tool_use]", "[assistant]", "[result]")):
            pick.append(("claude", l))
        elif l.startswith("[tool_result"):
            pick.append(("claude", l))
            pick.append(("deny", src[i + 2]))
        elif l.startswith(("$ test -e", "$ cd ")):
            pick.append(("cmd", l))
        elif l == "f.txt does not exist":
            pick.append(("ok", l))
        i += 1
    return pick


def color_for(line, kind=None):
    if kind == "cmd" or line.startswith("$ "):
        return FG
    if kind == "claude":
        return CYAN
    if kind == "deny" or "Permission denied" in line or "BLOCKED" in line:
        return RED
    if kind == "ok" or "lease ended" in line or "still says" in line or "nothing lost" in line:
        return GREEN
    if line.startswith(("--- ", "== ", "#")) or kind == "info":
        return DIM
    if kind == "claude":
        return CYAN
    m = re.match(r"\s*\d+s\s+(agent-[A-D])", line)
    if m:
        return AGENT[m.group(1)]
    return FG


def fit(s):
    return s if len(s) <= COLS - 2 else s[:COLS - 3] + "…"


def wrap(s, indent="  "):
    out, width = [], COLS - 2
    while len(s) > width:
        cut = s.rfind(" ", 0, width)
        cut = cut if cut > width // 2 else width
        out.append(s[:cut])
        s = indent + s[cut:].lstrip()
    return out + [s]


def build_timeline(rec):
    """Return segments: dict(title, start, end, lines=[(t_abs, text, color, typed)])."""
    segs = []
    # Segment 1: ./demo.sh, real timings.
    t = 0.0
    lines = [(t + 0.4, "$ ./demo.sh", FG, True)]
    start_out = t + 1.6
    for dt, l in rec:
        lines.append((start_out + dt, fit(l), color_for(l), False))
    end1 = lines[-1][0] + 4.0
    segs.append(dict(title="./demo.sh  ·  recorded run, real timing", start=0.0, end=end1, lines=lines))
    # Segment 2: real Claude Code session under Landlock (replay of the recorded transcript).
    t = end1
    pick = landlock_lines()
    lines = [(t + 0.3, "# Replay of a real Claude Code session under `agentlanes run` (Landlock), recorded on Linux:", DIM, False)]
    lines.append((t + 0.3, "# agent may write only ~/demo/A; the prompt asks it to write ~/demo/B/f.txt.", DIM, False))
    clock = t + 1.2
    # Spread the session's own lines over its real 6.4 s run (run.start -> run.stop in the event log).
    steps = {"[init]": 0.8, "[tool_use]": 2.6, "[tool_result": 1.4, "[assistant]": 1.2, "[result]": 0.4}
    for kind, l in pick:
        typed = kind == "cmd" and l.startswith("$ ")
        if l.startswith("$ test"):
            clock += 1.0
        for k, v in steps.items():
            if l.startswith(k):
                clock += v
        if kind == "cmd" and not typed:
            pass
        elif typed:
            clock += 0.3
        if kind == "claude" and l.startswith("[assistant]"):
            for part in wrap(l):
                lines.append((clock, part, color_for(l, kind), False))
        else:
            lines.append((clock, fit(l), color_for(l, kind), typed))
        if typed:
            clock += 0.9
    end2 = clock + 4.5
    segs.append(dict(title="real Claude Code session  ·  agentlanes run (Landlock)", start=t, end=end2, lines=lines))
    return segs, end2


def pct(x, total):
    return "%.3f%%" % (100.0 * max(0.0, min(x, total)) / total)


def render(segs, total, freeze=None):
    E = 0.02  # s, "instant" step
    css, body = [], []
    css.append("text{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,'DejaVu Sans Mono','Liberation Mono',monospace;"
               "font-size:%dpx;white-space:pre}" % FONT)
    css.append(".a{animation-duration:%.2fs;animation-iteration-count:infinite;animation-fill-mode:both}" % total)
    n = 0

    def anim(name, frames):
        # frames: list of (t_seconds, "css decl", timing_fn or None)
        nonlocal n
        n += 1
        k = "k%d" % n
        parts = []
        for tt, decl, fn in frames:
            parts.append("%s{%s%s}" % (pct(tt, total), decl, (";animation-timing-function:" + fn) if fn else ""))
        css.append("@keyframes %s{%s}" % (k, "".join(parts)))
        css.append(".%s{animation-name:%s}" % (name, k))

    def static_at(frames):
        cur = frames[0][1]
        for tt, decl, _ in frames:
            if tt <= freeze:
                cur = decl
        return cur

    def el(cls, frames):
        if freeze is not None:
            return ' style="%s"' % static_at(frames)
        anim(cls, frames)
        return ' class="a %s"' % cls

    # Window chrome.
    body.append('<rect width="%d" height="%d" rx="10" fill="%s"/>' % (WIDTH, HEIGHT, BG))
    body.append('<path d="M0 10a10 10 0 0 1 10-10h%d a10 10 0 0 1 10 10v%dh-%dz" fill="%s"/>'
                % (WIDTH - 20, BAR - 10, WIDTH, BAR_BG))
    for i, c in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        body.append('<circle cx="%d" cy="%d" r="6" fill="%s"/>' % (20 + i * 20, BAR // 2, c))
    body.append('<clipPath id="scr"><rect x="0" y="%d" width="%d" height="%d"/></clipPath>'
                % (BAR + PAD_TOP + 1, WIDTH, HEIGHT - BAR - PAD_TOP - 1))

    for si, s in enumerate(segs):
        vis = [(0, "opacity:0", None), (s["start"] - E, "opacity:0", None), (s["start"], "opacity:1", None), (s["end"] - E, "opacity:1", None),
               (s["end"], "opacity:0", None), (total, "opacity:0", None)]
        if si == 0:
            vis = [(0, "opacity:1", None), (s["end"] - E, "opacity:1", None), (s["end"], "opacity:0", None),
                   (total, "opacity:0", None)]
        if s["end"] >= total:
            vis = [v for v in vis if v[0] < s["end"] - E] + [(total, "opacity:1", None)]
        body.append('<g%s>' % el("s%d" % si, vis))
        body.append('<text x="%d" y="%d" fill="%s" text-anchor="middle" style="font-size:12px">%s</text>'
                    % (WIDTH // 2, BAR // 2 + 4, DIM, html.escape(s["title"])))
        # Scrolling: once line i (0-based) appears and i >= ROWS, shift up so it is the last visible row.
        scroll = [(s["start"], "transform:translateY(0px)", None)]
        for i, (tt, *_r) in enumerate(s["lines"]):
            if i >= ROWS:
                scroll.append((tt - E, scroll[-1][1], None))
                scroll.append((tt, "transform:translateY(%.1fpx)" % (-(i - ROWS + 1) * LH), None))
        scroll.append((total, scroll[-1][1], None))
        body.append('<g clip-path="url(#scr)"><g%s>' % el("sc%d" % si, scroll))
        for i, (tt, text, color, typed) in enumerate(s["lines"]):
            y = BAR + PAD_TOP + (i + 1) * LH - 5
            if typed:
                # Prompt, then the command "typed" by a cover rect sliding right in character steps.
                prompt, cmd = text[:2], text[2:]
                dur = min(0.9, 0.035 * len(cmd))
                body.append('<text x="%d" y="%.1f" fill="%s"%s>%s</text>' % (
                    PAD_X, y, GREEN, el("p%d_%d" % (si, i), [(0, "opacity:0", None), (tt - dur - 0.3 - E, "opacity:0", None),
                                                               (tt - dur - 0.3, "opacity:1", None),
                                                               (total, "opacity:1", None)]), "$"))
                x0 = PAD_X + 2 * CW
                body.append('<text x="%.1f" y="%.1f" fill="%s">%s</text>' % (x0, y, color, html.escape(cmd)))
                w = len(cmd) * CW + CW
                cover = [(0, "transform:translateX(0px)", None), (tt - dur, "transform:translateX(0px)", "steps(%d,end)" % len(cmd)),
                         (tt, "transform:translateX(%.1fpx)" % w, None), (total, "transform:translateX(%.1fpx)" % w, None)]
                body.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"%s/>' % (
                    x0, y - LH + 5, w + 2, LH, BG, el("c%d_%d" % (si, i), cover)))
            else:
                frames = [(0, "opacity:0", None), (tt - E, "opacity:0", None), (tt, "opacity:1", None), (total, "opacity:1", None)]
                body.append('<text x="%d" y="%.1f" fill="%s"%s>%s</text>' % (PAD_X, y, color, el("l%d_%d" % (si, i), frames),
                                                                             html.escape(text)))
        body.append('</g></g></g>')

    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" xml:space="preserve" '
            'role="img" aria-label="agentlanes demo: three agents share one file and one build slot; a real Claude Code '
            'session is stopped by Landlock">\n<style>%s</style>\n%s\n</svg>\n'
            % (WIDTH, HEIGHT, WIDTH, HEIGHT, "\n".join(css), "\n".join(body)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-recording", action="store_true", help="reuse docs/demo-recording.json")
    ap.add_argument("--freeze", type=float, help="write a static frame at this second instead of the animation")
    ap.add_argument("--frames", help="write one static SVG per screen change into this dir + ffconcat list (for video)")
    ap.add_argument("-o", "--out", help="output path (default docs/demo.svg, or docs/demo-frame.svg with --freeze)")
    a = ap.parse_args()
    docs = ROOT / "docs"
    docs.mkdir(exist_ok=True)
    rec_path = docs / "demo-recording.json"
    if a.from_recording:
        rec = [tuple(x) for x in json.loads(rec_path.read_text())["lines"]]
    else:
        rec = record()
        rec_path.write_text(json.dumps({"source": "./demo.sh", "recorded": time.strftime("%Y-%m-%d %H:%M"),
                                        "lines": rec}, indent=0) + "\n")
    segs, total = build_timeline(rec)
    if a.frames:
        fd = Path(a.frames)
        fd.mkdir(parents=True, exist_ok=True)
        times = sorted({0.0} | {round(t, 2) for s in segs for t, *_ in s["lines"]} | {round(s["start"], 2) for s in segs})
        times = [t for t in times if t < total]
        lst = ["ffconcat version 1.0"]
        for i, t in enumerate(times):
            (fd / ("f%03d.svg" % i)).write_text(render(segs, total, t + 0.005))
            nxt = times[i + 1] if i + 1 < len(times) else total
            lst += ["file f%03d.png" % i, "duration %.3f" % (nxt - t)]
        lst.append("file f%03d.png" % (len(times) - 1))   # concat demuxer ignores the last duration otherwise
        (fd / "list.ffconcat").write_text("\n".join(lst) + "\n")
        print("%d frames in %s" % (len(times), fd))
        return
    out = Path(a.out) if a.out else docs / ("demo-frame.svg" if a.freeze is not None else "demo.svg")
    out.write_text(render(segs, total, a.freeze))
    print("%s: %.1f s loop, %d + %d lines" % (out, total, len(segs[0]["lines"]), len(segs[1]["lines"])))


if __name__ == "__main__":
    main()
