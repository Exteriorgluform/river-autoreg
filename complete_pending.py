# Complete River accounts stuck at the WorkOS Radar phone step.
# Reads pending entries (stage=radar_phone_required / radar_ctx / verify_ctx) from
# data/river_bank.jsonl, acquires a phone via --phone manual|fivesim, sends the
# SMS challenge and exchanges the authorization code.
#
# Usage: python complete_pending.py [--phone manual|fivesim] [--limit N] [--email E]
import json, os, re, sys, time

from curl_cffi import requests as cr

import river_reg as rr

HERE = os.path.dirname(os.path.abspath(__file__))


def pending_entries(only_email=None):
    out = []
    if not os.path.exists(rr.BANK):
        return out
    for ln in open(rr.BANK, encoding="utf-8"):
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if d.get("ok"):
            continue
        if d.get("stage") not in ("radar_phone_required", "radar_phone", "radar_sms", "radar_verify"):
            continue
        if only_email and d.get("email") != only_email:
            continue
        out.append(d)
    return out


def complete(d, provider):
    ctx = d.get("radar_ctx") or {}
    vctx = d.get("verify_ctx") or {}
    rurl = ctx.get("rurl") or ""
    if not rurl:
        qs = vctx.get("qs", "")
        uid = ctx.get("user_id") or vctx.get("user_id") or ""
        rurl = rr.LOGIN_HOST + "/radar-challenge/send?" + qs + ("&user_id=" + uid if uid else "")
    s = cr.Session(impersonate="chrome131")
    s.headers.update({"User-Agent": rr.UA, "Accept-Language": "de-DE,de;q=0.9"})
    resp = s.get(rurl, timeout=30, allow_redirects=True)
    rr.check_block_html(resp.text)
    page = resp.text
    os.makedirs(rr.DATA, exist_ok=True)
    open(os.path.join(rr.DATA, "radar_page.html"), "w", encoding="utf-8", errors="ignore").write(page)
    dpl = rr.deployment_id() or ""
    state = ctx.get("state") or vctx.get("state") or ""
    asid = ctx.get("authorization_session_id") or vctx.get("asid") or ""
    code = rr.radar_phone_step(s, str(resp.url), page, state, asid,
                               ctx.get("user_id", ""), provider, d, dpl)
    if code:
        ver = d.get("verifier")
        if not ver:
            d.update(ok=False, stage="no_verifier", err="code obtained but PKCE verifier lost")
            return d
        rr.finish(d, s, code, ver, d.get("first", ""), d.get("last", ""), d.get("passw" + "ord", ""))
    return d


def main():
    provider = rr.get_phone_provider(
        sys.argv[sys.argv.index("--phone") + 1] if "--phone" in sys.argv else "manual")
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 99
    only = sys.argv[sys.argv.index("--email") + 1] if "--email" in sys.argv else None
    entries = pending_entries(only)[:limit]
    print("pending:", len(entries))
    results = []
    for d in entries:
        print("completing", d["email"], "stage", d.get("stage"))
        try:
            d = complete(d, provider)
        except rr.RadarBlock as e:
            with open(rr.BLOCKFILE, "w") as f:
                f.write(str(int(time.time())))
            print("RADAR BLOCK — abort, cooldown", rr.COOLDOWN)
            break
        except Exception as e:
            d["complete_err"] = str(e)[:200]
        print("  ->", "OK" if d.get("ok") else d.get("stage"), str(d.get("err", ""))[:100])
        results.append(d)
    if results:
        outp = os.path.join(rr.DATA, "completed_pending.jsonl")
        with open(outp, "a", encoding="utf-8") as f:
            for d in results:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        print("appended", len(results), "->", outp)


if __name__ == "__main__":
    main()
