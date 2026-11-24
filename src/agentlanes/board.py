"""agentlanes board: a read-only live web page of who is doing what on this machine.

Standard library only. Binds 127.0.0.1 by default; the page has no write actions and loads nothing from outside.
  agentlanes board [--port 8765] [--host 127.0.0.1]
"""
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import NAME, __version__, claims, core, jobs, procs, slots

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>agentlanes board</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#16181d;--mute:#667085;--line:#e4e7ec;--ok:#12b76a;--warn:#f79009;--bad:#f04438;--acc:#2e90fa}
@media (prefers-color-scheme:dark){:root{--bg:#0d1017;--card:#161b26;--fg:#e9ecf2;--mute:#94a0b8;--line:#262d3b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;gap:12px;align-items:baseline;padding:16px 20px;border-bottom:1px solid var(--line)}
h1{font-size:17px;margin:0}header small{color:var(--mute)}#mem{margin-left:auto;font-variant-numeric:tabular-nums}
main{display:grid;gap:14px;padding:16px 20px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;min-width:0}
h2{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--mute);margin:0 0 8px}
.row{display:flex;gap:10px;align-items:center;padding:7px 0;border-top:1px solid var(--line)}.row:first-of-type{border-top:0}
.av{flex:none;width:30px;height:30px;border-radius:50%;display:grid;place-items:center;color:#fff;font-weight:600;font-size:12px}
.grow{flex:1;min-width:0}.t{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.s{color:var(--mute);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pill{font-size:11px;padding:2px 8px;border-radius:99px;border:1px solid var(--line);color:var(--mute);white-space:nowrap}
.busy{color:var(--acc);border-color:var(--acc)}.stale,.conflict{color:var(--bad);border-color:var(--bad)}.free{color:var(--ok);border-color:var(--ok)}
.empty{color:var(--mute);padding:6px 0}#ev .row{font-family:ui-monospace,Menlo,monospace;font-size:12px}
</style></head><body>
<header><h1>agentlanes board</h1><small id="st">connecting…</small><span id="mem"></span></header>
<main>
<section><h2>Agents</h2><div id="agents"></div></section>
<section><h2>Claims</h2><div id="claims"></div></section>
<section><h2>Slots</h2><div id="slots"></div></section>
<section><h2>Jobs</h2><div id="jobs"></div></section>
<section style="grid-column:1/-1" id="ev"><h2>Recent events</h2><div id="events"></div></section>
</main>
<script>
const C=['#2e90fa','#7a5af8','#ee46bc','#f79009','#12b76a','#06aed4','#f04438','#667085'];
function col(s){let h=0;for(const c of String(s))h=(h*31+c.charCodeAt(0))>>>0;return C[h%C.length]}
function el(t,cls,txt){const e=document.createElement(t);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e}
function row(who,title,sub,pill,pcls){const r=el('div','row');if(who!==null){const a=el('div','av',String(who||'?').slice(0,2).toUpperCase());a.style.background=col(who);r.append(a)}
const g=el('div','grow');g.append(el('div','t',title));if(sub)g.append(el('div','s',sub));r.append(g);if(pill)r.append(el('span','pill '+(pcls||''),pill));return r}
function fill(id,rows,empty){const d=document.getElementById(id);d.replaceChildren(...(rows.length?rows:[el('div','empty',empty)]))}
function ago(s){s=Math.max(0,Math.round(s));return s<60?s+'s':s<3600?Math.round(s/60)+'m':Math.round(s/3600)+'h'}
async function tick(){try{const r=await fetch('api/state',{cache:'no-store'});const d=await r.json();const now=d.now;
document.getElementById('st').textContent=d.state_dir+' · '+new Date(now*1000).toLocaleTimeString();
document.getElementById('mem').textContent=d.mem_mb==null?'':d.mem_mb+' MB free';
fill('agents',d.agents.map(a=>row(a.kind+'-'+a.pid,a.kind+'  '+a.pid,(a.claims.join(', ')||'no claims')+' · '+a.mem_mb+' MB · up '+ago(a.age_s),a.headless?'batch':'chat')),'no coding agents running');
fill('claims',Object.entries(d.claims).map(([k,c])=>row(c.by,k,(c.desc||'')+((c.paths||[]).length?' · '+c.paths.join(', '):''),ago(now-c.since))),'nothing claimed');
fill('slots',d.slots.flatMap(s=>s.slots.map(x=>row(x.who&&x.who.by||null,s.name+' #'+x.no,x.who&&x.who.cmd||'',x.state,x.state)).concat(s.waiting?[row(null,s.name,s.waiting+' waiting')]:[])),'no slots used yet');
fill('jobs',d.jobs.map(j=>row(j.by||null,'#'+j.id+' '+j.title,j.state+(j.by?' · '+j.by:''),'p'+j.prio,j.state=='taken'?'busy':'')),'queue empty');
fill('events',d.events.slice().reverse().map(e=>row(null,e.line,null,e.kind,e.kind.includes('conflict')||e.kind.includes('blocked')?'conflict':'')),'no events in the last 24 h');
}catch(e){document.getElementById('st').textContent='disconnected'}}
tick();setInterval(tick,2000);
</script></body></html>"""


def state():
    now = time.time()
    rows = procs.list_agents(False)
    cl = claims.snapshot()
    for a in rows:
        a['claims'] = sorted(k for k, c in cl.items() if a['pid'] in (c.get('apid'), c.get('pid')))
    sl = []
    for n in slots.all_names():
        sl.append({'name': n, 'waiting': len(slots.waiters(n)),
                   'slots': [{'no': i, 'state': st, 'who': who} for i, st, who in slots.status(n, clean=False)]})
    evs = list(core.read_events(since=now - 86400))[-30:]
    return {'now': now, 'version': __version__, 'state_dir': core.state_dir(), 'mem_mb': procs.mem_available_mb(),
            'agents': rows, 'claims': cl, 'slots': sl, 'jobs': jobs.items(),
            'events': [{'kind': e.get('kind', ''), 'line': core.fmt_event(e)} for e in evs]}


class Handler(BaseHTTPRequestHandler):
    server_version = '%s/%s' % (NAME, __version__)

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        b = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path.split('?', 1)[0]
        if p in ('/', '/index.html'):
            return self._send(200, PAGE, 'text/html; charset=utf-8')
        if p == '/api/state':
            return self._send(200, json.dumps(state(), default=str), 'application/json')
        return self._send(404, 'not found\n', 'text/plain; charset=utf-8')


def serve(host='127.0.0.1', port=8765, out=print):
    srv = ThreadingHTTPServer((host, port), Handler)
    out('%s board on http://%s:%d/  (read-only; Ctrl-C to stop)' % (NAME, host, srv.server_address[1]))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
