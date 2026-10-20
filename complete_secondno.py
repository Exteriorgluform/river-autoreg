# -*- coding: utf-8 -*-
"""Complete a 2nd-no.com account that already SIGNED UP (confirmation email sent)
but is not yet confirmed/logged-in. Reads credentials from env (no secrets in file).

Usage:
  ACC_EMAIL=... ACC_PWD=... SECONDNO_PROXY=http://127.0.0.1:8899 \
    python complete_secondno.py

Flow: wait confirm email (t-online IMAP, UID scan, specific 2nd-no keywords)
      -> click confirm link (browser via proxy) -> login -> reserve number
      -> save to data/secondno/state.json
"""
import json
import os
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("SECONDNO_HOME", os.path.join(ROOT, "data", "secondno"))
sys.path.insert(0, ROOT)

from secondno import SecondNo, TOnlineIMAP, get_solver  # noqa: E402
from secondno.client import save_account  # noqa: E402


def main():
    email = os.environ.get("ACC_EMAIL", "")
    pwd = os.environ.get("ACC_PWD", "")  # 2nd-no.com account password
    if not email or not pwd:
        print(json.dumps({"error": "need ACC_EMAIL and ACC_PWD env"}))
        sys.exit(2)

    # The t-online IMAP password (to READ the confirmation mail) is DIFFERENT from
    # the 2nd-no account password (to LOGIN). Look up the IMAP creds in mail_pool.
    pool = TOnlineIMAP._load_pool(os.path.join(ROOT, "data", "mail_pool.txt"))
    imap_acc = next((a for a in pool if a[0] == email), None)
    if not imap_acc:
        print(json.dumps({"error": f"ACC_EMAIL {email[:8]}*** not found in mail_pool.txt"}))
        sys.exit(2)

    ib = TOnlineIMAP(account=imap_acc)  # t-online IMAP creds
    # Confirmation email was already sent at signup -> do NOT snapshot a baseline
    # now (it would skip the email). Empty baseline = scan all, keyword-filtered.
    ib._baseline = set()

    s = SecondNo(solver=get_solver(), headless=True)
    try:
        print(f"[complete] waiting confirm email for {email[:8]}*** ...", flush=True)
        msg = ib.wait_email(email, timeout=900)
        if not msg:
            print(json.dumps({"error": "no confirm email (t-online lag or signup failed)"}))
            sys.exit(1)
        body = msg.get("raw", "") or ""
        links = re.findall(r"https?://[^\s\"'<>]+", body)
        link = next((l for l in links if "create-account" in l or "confirm" in l), None)
        if not link:
            print(json.dumps({"error": "no confirm link in email", "links": links[:3]}))
            sys.exit(1)
        print(f"[complete] confirm link found, verifying ...", flush=True)
        pg = s._start()
        pg.goto(link, wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(3000)
        print(f"[complete] confirmed, logging in ...", flush=True)
        s.login(email, pwd)
        print(f"[complete] logged in, reserving number ...", flush=True)
        info = s.get_number()
        rec = {"email": email, "password": pwd, **info, "ts": int(time.time())}
        save_account(rec)
        print(json.dumps({"ok": True, "email": email[:8] + "***",
                          "number": info.get("full"), "expires": info.get("expires")},
                         ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"error": str(e)[:200]}))
        sys.exit(1)
    finally:
        s.close()


if __name__ == "__main__":
    main()
