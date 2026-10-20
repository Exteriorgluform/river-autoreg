#!/usr/bin/env python3
"""reg2no_fast.py -- register a 2nd-no.com account and CONFIRM it FAST (fresh token).

Acc#2 died because the create-account token expired between signup and confirm
(t-online lag + retries). This does signup -> poll confirm email every 5s -> goto
link immediately -> verify creation -> login -> reserve -> save. One tight flow.

Usage: python reg2no_fast.py [pool_index]
Env: SECONDNO_PROXY (fresh IP, e.g. http://127.0.0.1:8899)
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
import os
import random
import re
import string
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("SECONDNO_HOME", os.path.join(ROOT, "data", "secondno"))

import imaplib
import email as _email

from secondno import SecondNo, get_solver
from secondno.client import TOnlineIMAP, _FILL_JS, SITE

POOL_FILE = os.path.join(ROOT, "data", "mail_pool.txt")
STATE = os.path.join(ROOT, "data", "secondno", "state.json")


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def fetch_link(imap_user, imap_pw, target, max_wait=180):
    """Poll t-online IMAP for the 2nd-no create-account link (fresh)."""
    since = (_dt.date.today() - _dt.timedelta(days=1)).strftime("%d-%b-%Y")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            M = imaplib.IMAP4_SSL("secureimap.t-online.de", 993, timeout=30)
            M.login(imap_user, imap_pw)
            M.select("INBOX")
            typ, data = M.uid("SEARCH", None, f"(SENTSINCE {since})")
            uids = data[0].split() if data and data[0] else []
            link = ""
            for uid in reversed(uids[-25:]):
                typ, md = M.uid("FETCH", uid, "(RFC822)")
                if not md or not md[0]:
                    continue
                msg = _email.message_from_bytes(md[0][1])
                body = ""
                for part in msg.walk():
                    if part.get_content_type() in ("text/plain", "text/html"):
                        try:
                            body += part.get_payload(decode=True).decode(
                                part.get_content_charset() or "utf-8", "ignore")
                        except Exception:
                            pass
                if "create-account" in body.lower():
                    ls = re.findall(r"https?://[^\s\"'<>]+", body)
                    link = next((l for l in ls if "create-account" in l), "")
                    if link:
                        break
            M.logout()
            if link:
                return link
        except Exception as e:
            log("imap retry:", repr(e)[:60])
        time.sleep(5)
    return ""


def main():
    idx = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    pool = TOnlineIMAP._load_pool(POOL_FILE)
    if idx >= len(pool):
        log("pool index out of range"); sys.exit(2)
    imap_user, imap_pw = pool[idx]
    email_addr = imap_user
    pwd = "Nx" + "".join(random.choices(string.ascii_letters + string.digits, k=8)) + "7!q"
    log(f"acc#{idx} email={email_addr[:8]}*** proxy={os.environ.get('SECONDNO_PROXY','none')}")

    s = SecondNo(solver=get_solver(), headless=True)
    pg = s._start()
    try:
        # 1) signup
        pg.goto(SITE + "/auth/register", wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_selector("input[type=password]", timeout=30000)
        pg.wait_for_timeout(1500)
        # register() fills BOTH password fields (password + confirm) + checkbox.
        # _FILL_JS only fills ONE password -> confirm empty -> submit blocked (st=None).
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
        }""", [email_addr, pwd])
        log("fill:", ok)
        pg.wait_for_timeout(500)
        signup = {}
        def cap(r):
            if r.request.method == "POST" and "2no.pl" in r.url and '"id":103' in (r.request.post_data or ""):
                try:
                    signup["st"] = r.status; signup["resp"] = r.text()[:120]
                except Exception:
                    pass
        pg.on("response", cap)
        pg.click("button[type=submit]", timeout=8000)
        pg.wait_for_timeout(6000)
        log("signup:", signup.get("st"), signup.get("resp", "")[:60])
        if signup.get("st") != 200:
            log("SIGNUP FAILED (likely IP limit) ->", signup.get("resp", ""))
            sys.exit(1)

        # 1b) SAVE PENDING CREDS IMMEDIATELY (never lose the password if the
        # confirm email lags past our wait -- t-online can take 10+ min).
        pend_file = os.path.join(ROOT, "data", "secondno", "pending.json")
        os.makedirs(os.path.dirname(pend_file), exist_ok=True)
        pend = []
        if os.path.exists(pend_file):
            try:
                pend = json.load(open(pend_file, encoding="utf-8"))
            except Exception:
                pend = []
        pend = [p for p in pend if p.get("email") != email_addr]
        pend.append({"email": email_addr, "password": pwd,
                     "imap_user": imap_user, "imap_pw": imap_pw,
                     "signed_at": int(time.time())})
        with open(pend_file, "w", encoding="utf-8") as f:
            json.dump(pend, f, indent=2, ensure_ascii=False)
        log("pending creds saved (password retained)")

        # 2) fetch confirm link FAST (t-online lag -> wait up to 7 min)
        t0 = time.time()
        link = fetch_link(imap_user, imap_pw, email_addr, max_wait=420)
        if not link:
            log("no confirm email in 180s (t-online lag) -- retry later")
            sys.exit(1)
        log(f"confirm link found in {time.time()-t0:.0f}s, confirming NOW")

        # 3) goto create-account link (fresh token)
        pg.goto(link, wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(5000)
        body = pg.evaluate("()=>document.body.innerText.replace(/\\s+/g,' ').slice(0,200)")
        log("create-account page:", repr(body[:120]))
        if "not created" in body.lower() or "went wrong" in body.lower():
            log("ACCOUNT CREATION FAILED (token/page issue)")
            sys.exit(1)

        # 4) login + reserve
        ok = s.login(email_addr, pwd)
        log("login:", ok, "url:", pg.url)
        num = s.get_number()
        if not num:
            log("reserve failed")
            sys.exit(1)
        log("RESERVED:", num.get("full"))

        # 5) save
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        st = {"accounts": []}
        if os.path.exists(STATE):
            try:
                st = json.load(open(STATE, encoding="utf-8"))
            except Exception:
                st = {"accounts": []}
        st.setdefault("accounts", []).append({
            "email": email_addr, "password": pwd,
            "imap_user": imap_user, "imap_pw": imap_pw,
            "full": num.get("full"), "country": num.get("country"),
            "reservation_id": num.get("reservation_id"),
            "reserved_at": int(time.time()), "expires": num.get("expires", ""),
            "used": False, "last_otp_at": 0,
        })
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=2, ensure_ascii=False)
        os.replace(tmp, STATE)
        log("SAVED to state.json -- accounts:", len(st["accounts"]))
    except Exception as e:
        log("ERR", repr(e)[:200])
        sys.exit(1)
    finally:
        s.close()


if __name__ == "__main__":
    main()
