"""2nd-no.com (2nr.pl) client — free Polish SMS numbers.

Flow: register(temp-mail) -> confirm email -> login -> turnstile -> reserve number -> poll SMS.
API: POST https://2no.pl/ JSON-RPC, auth header x-auth-token (sessionStorage '2NR-TOKEN').
Free tier: 3 numbers per account, 3-day reservations (extended on SMS receipt).

All API calls run INSIDE the logged-in browser context (page.evaluate + fetch):
direct HTTP to 2no.pl from outside the browser is blocked by Cloudflare (403).

JSON-RPC id map (from the site bundle):
  310 - random free number (+ 'captcha':'turnstile' flag when reservation needs a token)
  301 - reserve number {number_id, name, color, right_to_transfer_number, response_key=<turnstile token>}
  311 - my numbers {offset, limit} -> result[]: {number, number_id, reservation_to, ...}
  415 - getMessagesForNumber_v2 {phone: STRING, number_id: INT, limit, offset}
        NOTE: actual messages arrive under badges.messages[], data.messages stays empty.

Turnstile sitekey (public, from site bundle index-*.js): 0x4AAAAAAAh6YYTPTzEcN3Ep
"""
from __future__ import annotations

import json
import os
import random
import re
import string
import time

import httpx
from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
SITEKEY = "0x4AAAAAAAh6YYTPTzEcN3Ep"
API = "https://2no.pl/"
SITE = "https://2nd-no.com"

HOME = os.environ.get("SECONDNO_HOME") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(HOME, exist_ok=True)
STATE_FILE = os.path.join(HOME, "state.json")


class TempMailLol:
    """tempmail.lol v2 — disposable inbox. NOTE: token goes in QUERY param (?token=), not Bearer."""
    BASE = "https://api.tempmail.lol/v2"

    def __init__(self):
        self.c = httpx.Client(timeout=30)
        self.address = None
        self.token = None

    def create(self):
        d = self.c.post(self.BASE + "/inbox/create").json()
        self.address, self.token = d["address"], d["token"]
        return self.address

    def wait_email(self, sender=None, timeout=180, poll=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.c.get(self.BASE + "/inbox", params={"token": self.token})
            if r.status_code == 200:
                for m in r.json().get("emails", []):
                    hay = (str(m.get("from", "")) + str(m.get("subject", ""))).lower()
                    if sender is None or sender.lower() in hay:
                        return m
            time.sleep(poll)
        return None


class GmailForwardInbox:
    """Catch-all domain -> ForwardEmail -> Gmail. Poll Gmail IMAP for mail addressed to target.

    Env: GMAIL_ADDR, GMAIL_APP_PW, MAIL_DOMAIN (all required for this backend).
    """

    def __init__(self):
        import imaplib  # noqa: F401
        self.addr = os.environ.get("GMAIL_ADDR", "")
        self.app_pw = os.environ.get("GMAIL_APP_PW", "")
        self.domain = os.environ.get("MAIL_DOMAIN", "")
        if not (self.addr and self.app_pw and self.domain):
            raise RuntimeError("GmailForwardInbox needs GMAIL_ADDR, GMAIL_APP_PW, MAIL_DOMAIN env")
        self._imaplib = __import__("imaplib")

    def make_address(self, tag=None):
        tag = tag or ("u" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8)))
        return f"{tag}@{self.domain}"

    def wait_email(self, target_email, timeout=240, poll=8):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                M = self._imaplib.IMAP4_SSL("imap.gmail.com", 993)
                M.login(self.addr, self.app_pw)
                for box in ["INBOX", '"[Gmail]/All Mail"']:
                    try:
                        typ, _ = M.select(box, readonly=True)
                        if typ != "OK":
                            continue
                        typ, data = M.search(None, f'(TEXT "{target_email}")')
                        ids = data[0].split()
                        if ids:
                            typ, msgdata = M.fetch(ids[-1], "(RFC822)")
                            raw = msgdata[0][1].decode("utf-8", "ignore")
                            M.logout()
                            return {"raw": raw}
                    except Exception:
                        continue
                M.logout()
            except Exception:
                pass
            time.sleep(poll)
        return None


_FILL_JS = """(args) => {
    let n = 0;
    const setVal = (el, v) => { if (!el) return; try {
        const s = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value').set;
        s.call(el, v); el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true})); n++;
    } catch (e) {} };
    const inputs = Array.from(document.querySelectorAll('input'));
    setVal(inputs.find(i => i.type === 'email' || i.type === 'text'), args[0]);
    setVal(inputs.find(i => i.type === 'password'), args[1]);
    return n;
}"""


class SecondNo:
    """Browser-driven 2nd-no.com client (Cloudflare only passes inside the browser)."""

    def __init__(self, solver=None, headless=False):
        self._pw = None
        self._browser = None
        self._pg = None
        self.solver = solver
        self.headless = headless
        self.creds = None

    # ---- lifecycle ----
    def _start(self):
        if self._pg is None:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=self.headless, channel="chrome",
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                      "--window-position=2400,2400"])
            ctx = self._browser.new_context(user_agent=UA, locale="en-US")
            self._pg = ctx.new_page()
            self._pg.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        return self._pg

    def close(self):
        for fn in (lambda: self._browser.close(), lambda: self._pw.stop()):
            try:
                fn()
            except Exception:
                pass
        self._pg = None

    # ---- api via browser context ----
    def _call(self, id_, query=None):
        body = {"id": id_}
        if query is not None:
            body["query"] = query
        pg = self._start()
        res = pg.evaluate("""async (body) => {
            const tok = sessionStorage.getItem('2NR-TOKEN');
            const r = await fetch('https://2no.pl/', {method:'POST',
                headers:{'Content-Type':'application/json','x-auth-token':tok||''},
                body: JSON.stringify(body)});
            return await r.text();
        }""", body)
        try:
            return json.loads(res)
        except Exception:
            return {"raw": res[:200]}

    # ---- steps ----
    def register(self, password=None, inbox=None):
        """Register a fresh account. inbox: object with .create()/.make_address() + .wait_email()."""
        pg = self._start()
        if inbox is None:
            inbox = TempMailLol()
        if hasattr(inbox, "create"):
            email = inbox.create()
        else:
            email = inbox.make_address()
        pwd = password or ("Nx" + "".join(random.choices(string.ascii_letters, k=8)) + "7!q")
        pg.goto(SITE + "/auth/register", wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_selector("input[type=password]", timeout=30000)
        pg.wait_for_timeout(1500)
        ok = pg.evaluate("""(args) => {
            const setVal = (el, v) => { const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value').set;
                setter.call(el, v); el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true})); };
            const inputs = Array.from(document.querySelectorAll('input'));
            const txt = inputs.find(i => i.type === 'text' || i.type === 'email');
            const pws = inputs.filter(i => i.type === 'password');
            const cb = inputs.find(i => i.type === 'checkbox');
            if (!txt || pws.length < 2) return 'inputs missing';
            setVal(txt, args[0]); setVal(pws[0], args[1]); setVal(pws[1], args[1]);
            if (cb && !cb.checked) cb.click();
            return 'ok';
        }""", [email, pwd])
        if ok != "ok":
            raise RuntimeError(f"register fill failed: {ok}")
        pg.evaluate("() => document.querySelector('button[type=submit]').click()")
        pg.wait_for_timeout(6000)
        # confirm email
        msg = None
        if hasattr(inbox, "wait_email"):
            try:
                msg = inbox.wait_email(sender="2nr", timeout=180)
            except TypeError:
                msg = inbox.wait_email(email, timeout=180)
        if msg is None and hasattr(inbox, "wait_email"):
            try:
                msg = inbox.wait_email(email, timeout=60)
            except Exception:
                msg = None
        link = None
        if msg:
            body = (msg.get("body") or "") + (msg.get("html") or "") + (msg.get("raw") or "")
            links = re.findall(r"https?://[^\s\"'<>]+", body)
            link = next((l for l in links if "create-account" in l or "confirm" in l), None)
        if not link:
            raise RuntimeError("no confirmation link")
        pg.goto(link, wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(3000)
        self.creds = {"email": email, "password": pwd}
        return self.creds

    def login(self, email, password):
        """React-hydration-safe login: wait for password input, guarded setVal, one retry.

        The site changed on 2026-10-12: instant evaluate right after domcontentloaded
        hits un-hydrated React inputs ('inputs missing' / setter TypeError). This
        robust version waits for input[type=password] and retries the fill once.
        """
        pg = self._start()
        pg.goto(SITE + "/auth/login", wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_selector("input[type=password]", timeout=30000)
        pg.wait_for_timeout(1500)
        n = pg.evaluate(_FILL_JS, [email, password])
        if n < 2:
            pg.wait_for_timeout(4000)
            n = pg.evaluate(_FILL_JS, [email, password])
        if n < 2:
            raise RuntimeError(f"login fill incomplete ({n}/2)")
        pg.wait_for_timeout(600)
        pg.evaluate("() => { const b=document.querySelector('button[type=submit]'); if(b) b.click(); }")
        try:
            pg.wait_for_url("**/app/**", timeout=30000)
        except Exception:
            pass
        pg.wait_for_timeout(3000)
        self.creds = {"email": email, "password": password}
        return True

    # ---- numbers ----
    def get_number(self, name="farm", color="Red"):
        """Reserve a free Polish number. Solves Turnstile via self.solver.turnstile()."""
        rnd = self._call(310)
        result = rnd.get("result") or []
        if not result:
            raise RuntimeError(f"no free number: {str(rnd)[:120]}")
        num_id, number = result[0]["id"], result[0]["number"]
        ts = ""
        if rnd.get("captcha") == "turnstile" and self.solver:
            ts = self.solver.turnstile(SITEKEY, SITE + "/")
        res = self._call(301, {"number_id": num_id, "name": name, "color": color,
                               "right_to_transfer_number": True, "response_key": ts})
        if not res.get("success"):
            raise RuntimeError(f"reserve failed: {str(res)[:150]}")
        my = self._call(311, {"offset": 0, "limit": 10})
        entry = (my.get("result") or [{}])[0]
        return {"number": number, "full": "+48" + str(number), "number_id": num_id,
                "user_number_id": entry.get("id"), "expires": entry.get("reservation_to")}

    def list_numbers(self):
        my = self._call(311, {"offset": 0, "limit": 50})
        return my.get("result") or []

    def reuse_active(self, used: set):
        """REUSE-FIRST: return an active, not-yet-used reservation (skips turnstile cost
        once the 3-number limit is reached). Mark numbers used ONLY after a real OTP."""
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        for r0 in self.list_numbers():
            num = str(r0.get("number") or "")
            if num in used:
                continue
            if str(r0.get("reservation_to") or "") < now:
                continue
            return {"full": "+48" + num, "number_id": r0.get("number_id"),
                    "expires": r0.get("reservation_to")}
        return None

    # ---- sms ----
    def read_sms(self, phone, number_id, timeout=300, poll=8):
        """Poll SMS for a reserved number (id 415: phone=STRING, number_id=INT;
        messages arrive under badges.messages, data.messages stays empty)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            d = self._call(415, {"phone": str(phone), "number_id": int(number_id),
                                 "limit": 25, "offset": 0})
            msgs = []
            for b in (d.get("badges") or {}).get("messages") or []:
                msgs.extend(b.get("messages") or [])
            msgs.extend((d.get("data", {}) or {}).get("messages") or [])
            msgs.extend(d.get("result") or [])
            if msgs:
                return msgs
            time.sleep(poll)
        return []

    def wait_otp(self, phone, number_id, timeout=300, poll=8, digits=(4, 8), min_ts=None):
        """Wait for first SMS and extract the OTP code."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for m in self.read_sms(phone, number_id, timeout=poll * 2):
                text = m.get("text") or m.get("body") or ""
                ts = str(m.get("created_at") or m.get("date") or m.get("time") or "")
                if min_ts and ts and ts < min_ts:
                    continue
                mm = re.search(r"code is (\d{4,8})", text)
                if mm:
                    return mm.group(1), text
                for n in digits:
                    mm = re.search(rf"\b(\d{{{n}}})\b", text)
                    if mm:
                        return mm.group(1), text
            time.sleep(poll)
        return None, None


def save_account(info: dict):
    """Append account record to data/state.json."""
    state = {}
    try:
        state = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        state = {"accounts": []}
    state.setdefault("accounts", []).append(info)
    json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), indent=1)


def full_cycle(count=1, solver=None, inbox_factory=None):
    """Register fresh account(s) and grab number(s). Returns list of dicts."""
    if solver is None:
        from .captcha import get_solver
        solver = get_solver()
    out = []
    for _ in range(count):
        s = SecondNo(solver=solver)
        try:
            inbox = inbox_factory() if inbox_factory else None
            creds = s.register(inbox=inbox)
            s.login(creds["email"], creds["password"])
            info = s.get_number()
            rec = {**creds, **info, "ts": int(time.time())}
            save_account(rec)
            out.append(rec)
        except Exception as e:
            out.append({"error": str(e)[:150]})
        finally:
            s.close()
    return out
