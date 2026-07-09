"""Dashboard web accesible por LAN — funciona aunque el WAN esté caído.

Sirve una página autocontenida (sin recursos externos) con el estado en vivo y
el historial de eventos, y un endpoint JSON. Pensado para abrir desde el celu
en el WiFi de casa durante un corte.
"""
import json
import time

from aiohttp import web

import config

PAGE = """<!doctype html>
<html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>crappy-ISP · watchdog</title>
<style>
:root{color-scheme:dark light}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
 background:#0e1116;color:#e6edf3}
header{padding:18px 20px;border-bottom:1px solid #222a35;display:flex;
 align-items:center;gap:12px;flex-wrap:wrap}
h1{font-size:18px;margin:0;font-weight:600}
.sub{color:#8b949e;font-size:13px}
.wrap{max-width:900px;margin:0 auto;padding:18px 16px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.card{background:#161b22;border:1px solid #222a35;border-radius:12px;padding:14px}
.card .k{color:#8b949e;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.card .v{font-size:22px;font-weight:700;margin-top:4px}
.big{grid-column:1/-1;display:flex;align-items:center;gap:14px}
.dot{width:16px;height:16px;border-radius:50%;flex:none}
.up{background:#2ea043;box-shadow:0 0 12px #2ea04388}
.down{background:#f85149;box-shadow:0 0 12px #f8514988}
.warn{background:#d29922}
.pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:12px;font-weight:600}
table{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #222a35;vertical-align:top}
th{color:#8b949e;font-weight:600;font-size:11px;text-transform:uppercase}
td.t{white-space:nowrap;color:#8b949e;font-variant-numeric:tabular-nums}
.ev{font-weight:600}
.foot{color:#586069;font-size:12px;margin-top:20px}
code{background:#222a35;padding:1px 5px;border-radius:5px}
@media (prefers-color-scheme:light){body{background:#f6f8fa;color:#1f2328}
 .card,header{background:#fff;border-color:#d0d7de}.big .k{color:#656d76}}
</style></head>
<body>
<header>
 <span class="dot" id="hdot"></span>
 <div><h1>crappy-ISP · watchdog del ONU</h1>
 <div class="sub" id="hsub">cargando…</div></div>
</header>
<div class="wrap">
 <div class="cards">
  <div class="card big"><span class="dot" id="wdot"></span>
   <div><div class="k">Internet (WAN)</div><div class="v" id="wan">—</div></div>
   <div style="margin-left:auto;text-align:right">
    <div class="k">ONU</div><div class="v" id="onu">—</div></div>
  </div>
  <div class="card"><div class="k">Estado hace</div><div class="v" id="since">—</div></div>
  <div class="card"><div class="k">Reboots (ventana)</div><div class="v" id="reb">—</div></div>
  <div class="card"><div class="k">Cooldown</div><div class="v" id="cd">—</div></div>
  <div class="card"><div class="k">Uptime bot</div><div class="v" id="up">—</div></div>
 </div>
 <h3 style="margin:22px 0 4px">Eventos</h3>
 <table><thead><tr><th>Hora</th><th>Evento</th><th>Detalle</th></tr></thead>
  <tbody id="events"><tr><td colspan="3">…</td></tr></tbody></table>
 <div class="foot" id="cfg"></div>
</div>
<script>
function dur(s){s=Math.max(0,Math.floor(s));var d=Math.floor(s/86400);s%=86400;
 var h=Math.floor(s/3600);s%=3600;var m=Math.floor(s/60);s%=60;
 if(d)return d+"d "+h+"h";if(h)return h+"h "+m+"m";if(m)return m+"m "+s+"s";return s+"s";}
var EMO={wan_down:"🔴",wan_up:"🟢",reboot_start:"🔄",reboot_ok:"✅",
 reboot_fail:"❌",reboot_skip:"⏸️",info:"ℹ️"};
async function tick(){
 try{var r=await fetch('/api/status');var d=await r.json();}catch(e){
  document.getElementById('hsub').textContent='sin conexión al bot';return;}
 var wu=d.wan_up, ou=d.onu_up;
 function set(id,cls){var e=document.getElementById(id);e.className='dot '+cls;}
 set('wdot', wu===true?'up':wu===false?'down':'warn');
 set('hdot', wu===true?'up':wu===false?'down':'warn');
 document.getElementById('wan').textContent = wu===true?'ONLINE':wu===false?'CAÍDO':'—';
 document.getElementById('onu').textContent = ou===true?'ONLINE':ou===false?'CAÍDO':'—';
 var sub = wu===false && d.outage_secs ? ('⚠️ corte hace '+dur(d.outage_secs)) :
           wu===true ? 'todo OK' : 'evaluando…';
 document.getElementById('hsub').textContent = sub + ' · actualizado '+new Date().toLocaleTimeString();
 document.getElementById('since').textContent = d.since?dur(d.now-d.since):'—';
 document.getElementById('reb').textContent = d.reboots_in_window+' / '+d.max_reboots;
 document.getElementById('cd').textContent = d.cooldown_remaining>0?dur(d.cooldown_remaining):'listo';
 document.getElementById('up').textContent = dur(d.uptime_secs);
 var tb=document.getElementById('events');tb.innerHTML='';
 (d.events||[]).forEach(function(e){
  var tr=document.createElement('tr');
  tr.innerHTML='<td class="t">'+e.iso+'</td><td class="ev">'+(EMO[e.type]||'•')+' '+
   e.type+'</td><td>'+ (e.msg||'').replace(/[<>]/g,'') +'</td>';
  tb.appendChild(tr);});
 var c=d.config||{};
 document.getElementById('cfg').innerHTML='ONU <code>'+c.onu_url+'</code> · targets '+
  (c.wan_targets||[]).join(', ')+' · check '+c.check_interval+'s · '+
  (c.dry_run?'<b>DRY_RUN</b> · ':'')+(c.discord?'discord✓ ':'discord✗ ')+
  (c.ntfy?'ntfy✓':'ntfy✗');
}
tick();setInterval(tick,5000);
</script>
</body></html>"""


async def _index(_req):
    return web.Response(text=PAGE, content_type="text/html")


def make_app(store):
    async def _status(_req):
        return web.json_response(store.snapshot())

    app = web.Application()
    app.router.add_get("/", _index)
    app.router.add_get("/api/status", _status)
    app.router.add_get("/healthz", lambda _r: web.Response(text="ok"))
    return app


async def start_dashboard(store):
    app = make_app(store)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.DASHBOARD_HOST, config.DASHBOARD_PORT)
    await site.start()
    print(f"[dashboard] escuchando en http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}", flush=True)
    return runner
