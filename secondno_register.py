# -*- coding: utf-8 -*-
"""Register fresh 2nd-no.com account(s) into data/secondno/state.json.

Usage: python secondno_register.py [N] [--gmail]
  default inbox: tempmail.lol (built-in, no config)
  --gmail: catch-all domain -> ForwardEmail -> Gmail (needs GMAIL_ADDR, GMAIL_APP_PW, MAIL_DOMAIN env)

Each account = 3 free PL numbers / 3 days (renewed on SMS receipt).
Login wall is a React SPA -> playwright required (Python 3.11; PYTHONPATH="" on Windows).
Reservation turnstile: captcha sidecar :8877 (FREE) or 2captcha (TWOCAPTCHA_API_KEY).

These accounts feed `river_reg.py --phone secondno` (WorkOS Radar phone gate) and
any other phone-gated target via the SecondNoPhone provider.
"""
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("SECONDNO_HOME", os.path.join(ROOT, "data", "secondno"))
sys.path.insert(0, ROOT)

from secondno import full_cycle, GmailForwardInbox, TOnlineIMAP  # noqa: E402


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    n = int(args[0]) if args else 1
    inbox_factory = None
    if "--gmail" in sys.argv:
        inbox_factory = GmailForwardInbox
    elif "--tonline" in sys.argv:
        inbox_factory = TOnlineIMAP
    results = full_cycle(n, inbox_factory=inbox_factory, headless=True)
    ok = 0
    for r in results:
        print(json.dumps(r, ensure_ascii=False), flush=True)
        if not r.get("error"):
            ok += 1
    print(f"DONE {ok}/{n} accounts registered", flush=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
