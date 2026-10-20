# -*- coding: utf-8 -*-
"""River-autoreg dashboard — single-file, stdlib only.

Run:  python dashboard.py [port]        (default 8910)

Shows: bank accounts (data/river_bank.jsonl), 2nd-no account pool
(data/secondno/state.json), used numbers, WorkOS Radar block countdown,
mail pool size, sidecar captcha health, reg log tail.

Actions (POST):
  /api/reg            {"n":1,"phone":"secondno"}  -> river_reg.py wave (detached)
  /api/reg2no         {"n":1}                     -> secondno_register.py (detached)
  /api/complete       {"phone":"secondno"}        -> complete_pending.py (detached)
  /api/reserve_test                               -> live 2nd-no reservation test (sync)

Secrets are never rendered: passwords/keys are masked server-side.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
SN_HOME = os.path.join(DATA, "secondno")
PY = sys.executable
SIDECAR = "http://127.0.0.1:8877/health"
RESERVE_LOG = os.path.join(DATA, "reserve_test.log")


def _mask(s, keep=2):
    if not s:
        return ""
    s = str(s)
    return s[:keep] + "*" * max(4, len(s) - keep * 2) + s[-keep:] if len(s) > keep * 2 + 2 else "***"


def read_jsonl(path):
    out = []
    try:
        for ln in open(path, encoding="utf-8"):
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except Exception:
                    pass
    except Exception:
        pass
    return out


def read_json(path, default=None):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default if default is not None else {}


def radar_state():
    p = os.path.join(DATA, "radar_block")
    try:
        until = int(open(p, encoding="utf-8").read().strip())
    except Exception:
        return {"blocked": False, "until": 0, "left_s": 0}
    left = until - int(time.time())
    return {"blocked": left > 0, "until": until, "left_s": max(0, left)}


def bank_stats(bank):
    ok = sum(1 for b in bank if b.get("ok") and (b.get("api_key") or b.get("key")))
    pend = sum(1 for b in bank if not b.get("ok") and "radar" in str(b.get("stage", "")) + str(b.get("err", "")))
    fail = len(bank) - ok - pend
    return {"total": len(bank), "ok": ok, "pending": pend, "failed": fail}


def sidecar_ok():
    try:
        import urllib.request
        with urllib.request.urlopen(SIDECAR, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def snapshot():
    bank = read_jsonl(os.path.join(DATA, "river_bank.jsonl"))
    rows = []
    for b in bank:
        rows.append({
            "email": b.get("email", "?"),
            "stage": b.get("stage", ""),
            "ok": bool(b.get("ok")),
            "err": str(b.get("err", ""))[:90],
            "has_key": bool(b.get("api_key") or b.get("key")),
            "key": _mask(b.get("api_key") or b.get("key"), 4),
            "ts": b.get("ts") or b.get("time") or "",
        })
    sn = read_json(os.path.join(SN_HOME, "state.json"), {})
    sn_accs = []
    for a in sn.get("accounts", []):
        sn_accs.append({
            "email": a.get("email", "?"),
            "pass": _mask(a.get("password") or a.get("pass", ""), 2),
            "created": a.get("created") or a.get("ts") or "",
            "reservations": len(a.get("reservations") or []) if isinstance(a.get("reservations"), list) else a.get("reservations", 0),
        })
    used = read_json(os.path.join(SN_HOME, "used_numbers.json"), [])
    mail_pool = 0
    try:
        mail_pool = sum(1 for ln in open(os.path.join(DATA, "mail_pool.txt"), encoding="utf-8") if ln.strip())
    except Exception:
        pass
    log_tail = ""
    for lf in ("reg2no.log", "reserve_test.log", "wave.log"):
        p = os.path.join(DATA, lf)
        try:
            txt = open(p, encoding="utf-8", errors="ignore").read()
            log_tail += f"--- {lf} ---\n" + "\n".join(txt.splitlines()[-12:]) + "\n"
        except Exception:
            pass
    return {
        "now": int(time.time()),
        "radar": radar_state(),
        "bank": rows,
        "stats": bank_stats(bank),
        "secondno": {"accounts": sn_accs, "used_numbers": list(used)[-20:], "used_count": len(used)},
        "mail_pool": mail_pool,
        "sidecar": sidecar_ok(),
        "log_tail": log_tail[-3000:],
    }


def spawn(args, logfile):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.setdefault("SYSTEMDRIVE", "C:")
    f = open(os.path.join(DATA, logfile), "a", encoding="utf-8")
    f.write(f"\n=== {time.strftime('%H:%M:%S')} {' '.join(args)} ===\n")
    f.flush()
    p = subprocess.Popen([PY] + args, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT,
                         env=env, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    return p.pid


def reserve_test():
    """Sync reservation test against 2nd-no (no OTP wait): proves login+captcha+reserve."""
    accs = read_json(os.path.join(SN_HOME, "state.json"), {}).get("accounts", [])
    if not accs:
        return {"ok": False, "err": "no 2nd-no accounts (run secondno_register.py)"}
    os.environ.setdefault("SECONDNO_HOME", SN_HOME)
    sys.path.insert(0, ROOT)
    try:
        from secondno import SecondNo, get_solver
    except Exception as e:
        return {"ok": False, "err": f"import: {e}"}
    try:
        solver = get_solver()
    except Exception:
        solver = None
    sn = SecondNo(solver=solver, headless=True)
    try:
        acc = accs[0]
        sn.login(acc["email"], acc.get("password") or acc.get("pass", ""))
        info = None
        try:
            info = sn.reuse_active(set(read_json(os.path.join(SN_HOME, "used_numbers.json"), [])))
        except Exception:
            pass
        mode = "reuse"
        if not info:
            info = sn.get_number()
            mode = "fresh"
        if not info:
            return {"ok": False, "err": "no reservable number (cap/used)"}
        return {"ok": True, "mode": mode, "number": info.get("full"), "number_id": info.get("number_id")}
    except Exception as e:
        return {"ok": False, "err": str(e)[:200]}
    finally:
        try:
            sn.close()
        except Exception:
            pass


HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>River Autoreg Dashboard</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--bd:#30363d;--fg:#e6edf3;--mut:#8b949e;
--ok:#3fb950;--warn:#d29922;--err:#f85149;--acc:#ff6b35}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 ui-monospace,'Cascadia Mono',Consolas,monospace}
header{display:flex;align-items:center;gap:14px;padding:14px 20px;border-bottom:1px solid var(--bd)}
header h1{font-size:17px;margin:0;color:var(--acc)}header .sub{color:var(--mut);font-size:12px}
.wrap{padding:16px 20px;max-width:1280px;margin:0 auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:12px}
.card .n{font-size:24px;font-weight:700}.card .l{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
.ok{color:var(--ok)}.warn{color:var(--warn)}.err{color:var(--err)}.acc{color:var(--acc)}
.panel{background:var(--card);border:1px solid var(--bd);border-radius:8px;margin-bottom:16px;overflow:hidden}
.panel h2{font-size:13px;margin:0;padding:10px 14px;border-bottom:1px solid var(--bd);color:var(--acc);
text-transform:uppercase;letter-spacing:.5px;display:flex;justify-content:space-between;align-items:center}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{color:var(--mut);text-align:left;padding:6px 10px;border-bottom:1px solid var(--bd);font-weight:400}
td{padding:6px 10px;border-bottom:1px solid #21262d;vertical-align:top;word-break:break-all}
tr:last-child td{border-bottom:none}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;border:1px solid}
.b-ok{color:var(--ok);border-color:var(--ok)}.b-err{color:var(--err);border-color:var(--err)}
.b-warn{color:var(--warn);border-color:var(--warn)}.b-mut{color:var(--mut);border-color:var(--bd)}
.actions{display:flex;gap:8px;flex-wrap:wrap;padding:12px 14px}
button{background:#21262d;color:var(--fg);border:1px solid var(--bd);border-radius:6px;
padding:7px 14px;font:12.5px inherit;cursor:pointer}
button:hover{border-color:var(--acc);color:var(--acc)}button:disabled{opacity:.5;cursor:wait}
pre.log{margin:0;padding:12px 14px;font-size:11.5px;color:var(--mut);white-space:pre-wrap;
max-height:260px;overflow-y:auto}
input[type=number]{width:52px;background:#0d1117;border:1px solid var(--bd);color:var(--fg);
border-radius:4px;padding:5px;font:12px inherit}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}
.d-on{background:var(--ok)}.d-off{background:var(--err)}
</style></head><body>
<header><h1>⚡ RIVER AUTOREG</h1><span class="sub">river.ai · WorkOS · 2nd-no SMS</span>
<span class="sub" id="clock" style="margin-left:auto"></span></header>
<div class="wrap">
<div class="cards" id="cards"></div>
<div class="panel"><h2>Действия</h2><div class="actions">
<button onclick="act('reg2no')">+ Рега 2nd-no ×<input type="number" id="n2no" value="1" min="1" max="10"></button>
<button onclick="act('reg')">🚀 River-волна ×<input type="number" id="nreg" value="1" min="1" max="50"> (--phone secondno)</button>
<button onclick="act('complete')">⏳ Complete pending</button>
<button onclick="act('reserve_test')">📱 Reserve-тест 2nd-no</button>
<button onclick="load()">🔄 Обновить</button>
<label style="color:var(--mut);font-size:12px;align-self:center"><input type="checkbox" id="auto" checked> авто-обновление 15с</label>
</div></div>
<div class="panel"><h2>Аккаунты River <span id="bankcount" class="sub"></span></h2>
<div style="overflow-x:auto"><table id="bank"><thead><tr><th>email</th><th>stage</th><th>статус</th><th>key</th><th>ошибка</th></tr></thead><tbody></tbody></table></div></div>
<div class="panel"><h2>Пул 2nd-no.com <span id="sncount" class="sub"></span></h2>
<div style="overflow-x:auto"><table id="sn"><thead><tr><th>email</th><th>pass</th><th>создан</th></tr></thead><tbody></tbody></table></div></div>
<div class="panel"><h2>Логи</h2><pre class="log" id="log"></pre></div>
</div>
<script>
let S=null;
function fmtLeft(s){if(s<=0)return'—';const m=Math.floor(s/60),h=Math.floor(m/60);return h>0?h+'ч '+(m%60)+'м':m+'м '+ (s%60)+'с';}
function render(){
 if(!S)return;
 const r=S.radar, st=S.stats;
 document.getElementById('clock').textContent=new Date().toLocaleTimeString('ru-RU');
 document.getElementById('cards').innerHTML=[
  ['River OK',st.ok,'ok'],['pending (phone/radar)',st.pending,'warn'],['failed',st.failed,'err'],['всего в банке',st.total,'acc'],
  ['Radar-лок',r.blocked?'<span class=err>'+fmtLeft(r.left_s)+'</span>':'<span class=ok>свободен</span>',''],
  ['2nd-no акков',S.secondno.accounts.length,S.secondno.accounts.length?'ok':'warn'],
  ['номеров сожжено',S.secondno.used_count,''],
  ['mail pool',S.mail_pool,S.mail_pool?'ok':'err'],
  ['sidecar капча','<span class="dot '+(S.sidecar?'d-on':'d-off')+'"></span>'+(S.sidecar?'жив':'мёртв'),S.sidecar?'ok':'err'],
 ].map(c=>'<div class=card><div class="n '+c[2]+'">'+c[1]+'</div><div class=l>'+c[0]+'</div></div>').join('');
 document.getElementById('bankcount').textContent=st.total+' записей';
 const tb=document.querySelector('#bank tbody');
 tb.innerHTML=S.bank.slice().reverse().map(b=>'<tr><td>'+b.email+'</td><td>'+b.stage+'</td><td>'+
  (b.ok?'<span class="badge b-ok">OK</span>':'<span class="badge b-err">fail</span>')+'</td><td>'+
  (b.has_key?'<span class="badge b-ok">'+b.key+'</span>':'—')+'</td><td style="color:var(--mut)">'+b.err+'</td></tr>').join('')||'<tr><td colspan=5 style="color:var(--mut)">пусто</td></tr>';
 document.getElementById('sncount').textContent=S.secondno.accounts.length+' акк · '+S.secondno.used_count+' номеров использовано';
 document.querySelector('#sn tbody').innerHTML=S.secondno.accounts.map(a=>'<tr><td>'+a.email+'</td><td>'+a.pass+'</td><td>'+a.created+'</td></tr>').join('')||'<tr><td colspan=3 style="color:var(--mut)">нет акков — жми «+ Рега 2nd-no»</td></tr>';
 document.getElementById('log').textContent=S.log_tail||'(пусто)';
}
async function load(){try{const r=await fetch('/api/state');S=await r.json();render();}catch(e){}}
async function act(a){
 const btn=event&&event.target;let body={};
 if(a==='reg2no')body={n:+document.getElementById('n2no').value||1};
 if(a==='reg'){body={n:+document.getElementById('nreg').value||1,phone:'secondno'};
  if(S&&S.radar.blocked&&!confirm('Radar-лок активен ещё '+fmtLeft(S.radar.left_s)+'. Попытка продлит блок. Всё равно?'))return;}
 if(a==='reserve_test'&&!confirm('Живой резерв номера 2nd-no (тратит капчу если нет reuse). ОК?'))return;
 if(btn)btn.disabled=true;
 try{const r=await fetch('/api/'+a,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json();alert(JSON.stringify(j,null,1));}catch(e){alert('err: '+e);}
 if(btn)btn.disabled=false;load();
}
load();setInterval(()=>{if(document.getElementById('auto').checked)load();},15000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path).path
        if u in ("/", "/index.html"):
            self._send(200, HTML, "text/html")
        elif u == "/api/state":
            try:
                self._send(200, json.dumps(snapshot(), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"err": str(e)}))
        else:
            self._send(404, '{"err":"nf"}')

    def do_POST(self):
        u = urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        try:
            if u == "/api/reg2no":
                cnt = max(1, min(10, int(body.get("n", 1))))
                pid = spawn(["secondno_register.py", str(cnt)], "reg2no.log")
                self._send(200, json.dumps({"ok": True, "pid": pid, "log": "data/reg2no.log"}))
            elif u == "/api/reg":
                cnt = max(1, min(50, int(body.get("n", 1))))
                phone = body.get("phone", "secondno")
                args = ["river_reg.py", str(cnt), "--phone", phone, "--imap-timeout", "780"]
                pid = spawn(args, "wave.log")
                self._send(200, json.dumps({"ok": True, "pid": pid, "log": "data/wave.log"}))
            elif u == "/api/complete":
                pid = spawn(["complete_pending.py", "--phone", body.get("phone", "secondno")], "wave.log")
                self._send(200, json.dumps({"ok": True, "pid": pid, "log": "data/wave.log"}))
            elif u == "/api/reserve_test":
                self._send(200, json.dumps(reserve_test(), ensure_ascii=False))
            else:
                self._send(404, '{"err":"nf"}')
        except Exception as e:
            self._send(500, json.dumps({"err": str(e)[:200]}))


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8910
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(SN_HOME, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"river dashboard: http://127.0.0.1:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
