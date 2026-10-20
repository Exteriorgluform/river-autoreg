#!/usr/bin/env python3
"""complete_pending_2no.py -- finish 2nd-no accounts saved in pending.json.

For each pending account (signup done, password retained): login -> reserve a
free PL number -> save to state.json -> drop from pending. Run after reg2no_fast
exited on t-online lag, or to reserve a number for an already-confirmed account.

Env: SECONDNO_PROXY (fresh IP for the browser).
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("SECONDNO_HOME", os.path.join(ROOT, "data", "secondno"))

from secondno import SecondNo, get_solver

PEND = os.path.join(ROOT, "data", "secondno", "pending.json")
STATE = os.path.join(ROOT, "data", "secondno", "state.json")


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def load(p, default):
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return default
    return default


def save(p, obj):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


def main():
    pend = load(PEND, [])
    if not pend:
        log("no pending accounts"); return
    state = load(STATE, {"accounts": []})
    state.setdefault("accounts", [])
    done_emails = {a.get("email") for a in state["accounts"]}

    s = SecondNo(solver=get_solver(), headless=True)
    s._start()
    still = []
    try:
        for p in pend:
            email = p.get("email"); pwd = p.get("password")
            if not email or not pwd:
                continue
            if email in done_emails:
                log("already in state:", email[:8] + "***"); continue
            try:
                log("login", email[:8] + "***")
                s.login(email, pwd)
                num = s.get_number()
                log("RESERVED", num.get("full"), "expires", num.get("expires"))
                state["accounts"].append({
                    "email": email, "password": pwd,
                    "imap_user": p.get("imap_user"), "imap_pw": p.get("imap_pw"),
                    "full": num.get("full"), "number": num.get("number"),
                    "country": "PL", "number_id": num.get("number_id"),
                    "user_number_id": num.get("user_number_id"),
                    "reserved_at": int(time.time()), "expires": num.get("expires", ""),
                    "used": False, "last_otp_at": 0,
                })
                save(STATE, state)
                done_emails.add(email)
                log("SAVED -> state.json accounts:", len(state["accounts"]))
            except Exception as e:
                log("FAIL", email[:8] + "***", repr(e)[:140])
                still.append(p)
        # keep only the ones that failed
        save(PEND, still)
        log("pending remaining:", len(still))
    finally:
        s.close()


if __name__ == "__main__":
    main()
