# River.ai (console.river.ai) fast HTTP autoreg — WorkOS AuthKit Next-action replay.
#
# Flow:
#   PKCE authorize
#   -> POST /sign-up            (email step, Next.js server action)
#   -> POST /sign-up/password   (password step, Next.js server action)
#   -> GET  /email-verification (parse hidden fields)
#   -> IMAP poll 6-digit code   (t-online.de pool, To-header filter + INTERNALDATE)
#   -> POST /email-verification (submit code)
#   -> [WorkOS Radar] /radar-challenge/send  = phone SMS step (optional, --phone)
#   -> intercept ?code=...      (authorization code)
#   -> POST auth.console.river.ai/user_management/authenticate (code + verifier)
#   -> console /api/me, /api/billing/account, POST /api/keys  (rv_* API key)
#
# Usage:
#   python river_reg.py [N] [--start IDX] [--imap-timeout SEC] [--pool FILE]
#                       [--human] [--phone PROVIDER] [--force]
#
# Env:
#   RIVER_MAIL_POOL   mail pool file (email:imap_pass per line)
#   RIVER_COOLDOWN    seconds to pause after WorkOS "Access blocked" (default 14400)
#   FIVESIM_API_KEY   5sim.net key for --phone fivesim
#   FIVESIM_SERVICE   5sim service name (default "any")
#   FIVESIM_COUNTRY   5sim country (default "indonesia")
#
# IMPORTANT (learned the hard way):
# * WorkOS Radar keeps a GLOBAL IP-level signup lock: after a handful of attempts
#   it answers "Access blocked, please contact support" even in a real browser,
#   and EVERY further attempt extends the lock (~3-4h). This driver detects the
#   block, writes data/radar_block and aborts the wave. Do not --force through it.
# * t-online.de alias pitfall: base-0001 / base-001 / base-01 / base-1 are the
#   SAME mailbox. IMAP filter must match the To-header, and letters lag up to
#   7+ minutes (use --imap-timeout 780).
# * curl_cffi multipart works ONLY via CurlMime().addpart(name=, data=bytes);
#   files=/multipart=dict raise "'dict' object has no attribute '_form'".
import sys, json, time, base64, hashlib, secrets, random, re, os, calendar, imaplib
import email as emaillib

from curl_cffi import requests as cr, CurlMime

CLIENT_ID = "client_01M0TG5W3GBNMFS87K40PYHM98"
AUTH_HOST = "https://auth.console.river.ai"
LOGIN_HOST = "https://login.console.river.ai"
CONSOLE = "https://console.river.ai"
REDIRECT_URI = CONSOLE + "/auth/callback"
REDIRECT_URI_ENC = REDIRECT_URI.replace(":", "%3A").replace("/", "%2F")

# Known-good Next.js server-action ids (hosted-authkit deployment of 2026-10).
# If WorkOS redeploys, ids may drift -> discover_action() re-extracts them
# from the page's JS chunk automatically on 404.
ACTION_SIGNUP = "408cf05905813ee0522a7bfa5e0b517a06b23dd9aa"
ACTION_PASSWORD = "409a26bdef522a0e9b7e04b2ace7cc41756b225c83"
ACTION_VERIFY = "400f86f09b430b0bfc378d019394748599a569eb81"
ACTION_RADAR_SEND = "4037b59e1a955702b0f9f91cf0a01766c0b9541fdd"
DEPLOYMENT_ID = "hosted-authkit-b8ec20a00b2b35cd2ed9bbae575168c8e53cbb47-39302027821"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
APPVER = "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
IMAP_HOST = "secureimap.t-online.de"

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
BANK = os.path.join(DATA, "river_bank.jsonl")
BLOCKFILE = os.path.join(DATA, "radar_block")
COOLDOWN = int(os.environ.get("RIVER_COOLDOWN", "14400"))

FIRST = ["Alex", "Max", "Igor", "Nik", "Tom", "Dan", "Kiril", "Oleg", "Pavel", "Roman",
         "Sergey", "Viktor", "Yuri", "Anton", "Boris", "Denis", "Egor", "Ivan", "Leonid", "Mark"]
LAST = ["Keller", "Novak", "Braun", "Weber", "Fischer", "Lange", "Wolf", "Kruger", "Beck",
        "Hoffman", "Schulz", "Vogel", "Richter", "Klein", "Engel", "Haas", "Frank", "Bauer"]

KNOWN_PRE = [ACTION_SIGNUP[:30], ACTION_PASSWORD[:30], ACTION_VERIFY[:30],
             ACTION_RADAR_SEND[:30], "b8ec20a00b2b35cd"]


class RadarBlock(Exception):
    pass


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def log(*a):
    print(*a, flush=True)


def human_pause(enabled, lo=6, hi=18):
    if enabled:
        time.sleep(random.uniform(lo, hi))


def signals_b64():
    """WorkOS Radar client signals blob (base64 JSON), captured fingerprint with
    fresh timestamps. Static hashes = one real machine; good enough for scoring."""
    now = int(time.time() * 1000)
    t0 = now - random.randint(8000, 25000)
    sig = {
        "createdAtMs": t0, "timezone": "Europe/Berlin", "language": "de-DE",
        "hardwareConcurrency": 16, "webdriver": False, "userAgent": UA, "appVersion": APPVER,
        "platform": "Win32",
        "screen": {"width": 2560, "height": 1440, "availWidth": 2560, "availHeight": 1400,
                   "windowOuterWidth": 1905, "windowOuterHeight": 1340, "colorDepth": 24, "pixelDepth": 24},
        "rangeErrorLength": 0, "evalStringLength": 33,
        "playwrightDetected": False, "phantomDetected": False, "nightmareDetected": False,
        "seleniumDetected": False, "puppeteerDetected": False,
        "maxTouchPoints": 0, "deviceMemory": 8, "notificationPermission": "default",
        "devicePixelRatio": 1, "pluginsLength": 5, "mimeTypesCount": 2,
        "documentHidden": False, "documentVisibilityState": "visible",
        "mediaPreferences": {"colorScheme": "dark", "reducedMotion": False, "reducedTransparency": False,
                             "contrast": "no-preference", "colorGamut": "srgb", "hdr": False,
                             "forcedColors": False, "invertedColors": False},
        "webGLVendor": "Google Inc. (NVIDIA)",
        "webGLRenderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER (0x000021C4) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "permissionsState": "prompt",
        "collectedAtMs": t0 + random.randint(800, 1400),
        "timings": {"collectorsStarted": 14, "mathHash": 45, "intlHash": 46, "webGLParamsHash": 46,
                    "windowFeatures": 47, "cssKeys": 47, "mediaMime": 47, "fonts": 48, "voices": 52,
                    "minimalSurface": 53, "audioHash": 55, "canvasHash": 61, "worker": 210},
        "minimalSurface": {
            "windowFeaturesHash": "bbQ9_MI1i5xu9Hxnz9MQQziHshaV8opUBNDwsIRn9ys", "windowFeaturesCount": 1239,
            "cssKeysHash": "rw6N-ze3twOKRRedfSauRidKgM9WDfTfZ23lwe1Im4F0", "cssKeysCount": 1384,
            "mediaMimeHash": "kZhx7yaWeGtdKjuD7WOEr3WVtuDBSuluHtsXg3uGZGSw", "mediaMimeCount": 9,
            "fontsHash": "WRfQIPgS_OOrqPfpU1IraGr6zlsI-kSh9N2bv2dGWuY", "fontsCount": 16,
            "voicesHash": "M238MqoVf98oQCowsAmCkr9mg8OD_QgHfsfGhK9QXn8", "voicesLocalCount": 2,
            "voicesRemoteCount": 0, "voicesLanguagesCount": 1},
        "worker": {"ok": True, "hardwareConcurrency": 16, "platform": "Win32", "userAgent": UA,
                   "language": "de-DE",
                   "webGLRenderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER (0x000021C4) Direct3D11 vs_5_0 ps_5_0, D3D11)",
                   "webGLVendor": "Google Inc. (NVIDIA)"},
        "canvasHash": "yWThDKLHYrALh4ph-1taYawDqarr96g8WhW0i4Z3SWE",
        "audioHash": "xA7aObmDYWsQyPmTGHEHrH4ZiBMzccW4uG8R90E6GI8",
        "mathHash": "zK9Dnt1lDRxVL5kry2U6ScxY6YdeZxVK_WYAmIyFCC8",
        "intlHash": "lj-sIyqsLROcKulrDjkglX_5fzcbWVczXPmoE5hherfU",
        "webGLParamsHash": "hyCR3sCVuf12spKULU-b5bvGIcuReITDlIdj7BS00",
        "puppeteerDocumentNotAvailable": False,
        "submittedAtMs": now, "collectionState": "complete",
    }
    return base64.b64encode(json.dumps(sig, separators=(",", ":")).encode()).decode()


def mp(fields):
    mime = CurlMime()
    for k, v in fields.items():
        mime.addpart(name=k, data=v.encode("utf-8") if isinstance(v, str) else v)
    return mime


def flight_error(text):
    m = re.search(r'\{"code":"([a-z_]+)","message":"([^"]*)"', text)
    if m and m.group(2) and "Access blocked" in m.group(2):
        raise RadarBlock("access blocked (WorkOS Radar IP lock)")
    return (m.group(1), m.group(2)) if m else None


def check_block_html(text):
    # NB: the i18n dictionary embedded in EVERY AuthKit page contains the literal
    # string "Access blocked, please contact support." (escaped JSON value).
    # A REAL block renders it as visible UI text -> match only the rendered form.
    if re.search(r">\s*Access blocked", text):
        raise RadarBlock("access blocked (WorkOS Radar IP lock)")


def deployment_id(proxies=None):
    """Extract current hosted-authkit deployment id from any login page chunk URL."""
    r = cr.get(LOGIN_HOST + "/sign-up", impersonate="chrome131", timeout=30,
               headers={"User-Agent": UA}, proxies=proxies)
    m = re.search(r"dpl=(hosted-authkit-[a-f0-9]+-\d+)", r.text)
    return m.group(1) if m else None


def discover_action(s, html, exclude_pre=None):
    """Find the page's server-action id: pull its page-*.js chunk and grep 40-42 hex."""
    exclude_pre = exclude_pre or KNOWN_PRE
    urls = re.findall(r'(/apps/hosted-authkit/[^"]+?/page-[a-f0-9]+\.js[^"]*)', html)
    for u in dict.fromkeys(urls):
        try:
            rc = s.get(LOGIN_HOST + u, timeout=30)
            for i in dict.fromkeys(re.findall(r"[a-f0-9]{40,42}", rc.text)):
                if not any(i.startswith(k) or k.startswith(i[:30]) for k in exclude_pre):
                    return i
        except Exception:
            continue
    return None


def base_fields(state, asid, email, first, last):
    return {
        "_1_signals": signals_b64(),
        "_1_first_name": first,
        "_1_last_name": last,
        "_1_email": email,
        "_1_intent": "sign-up",
        "_1_redirect_uri": REDIRECT_URI,
        "_1_authorization_session_id": asid,
        "_1_state": state,
        "0": '["$K1"]',
    }



def _norm_local(addr):
    """t-online alias base: strip trailing -NNNN/-NNN/-NN/-N alias suffix."""
    loc = addr.split("@")[0].lower()
    return re.sub(r"-?\d{1,4}$", "", loc)


def _alias_match(want, to_header):
    """True when To-header holds another alias of the same mailbox base."""
    w = _norm_local(want)
    for m in re.findall(r"[\w.+-]+@[\w.-]+", to_header):
        if _norm_local(m) == w:
            return True
    return False


def read_code_imap(email_addr, imap_pass, timeout=360, poll=15, since_ts=0,
                   imap_host=None, sender_re=r"river\.ai|workos"):
    """Poll IMAP inbox for the 6-digit verification code.
    Filters: sender regex OR subject 'verify your email'; To-header must contain
    the exact alias; INTERNALDATE >= since_ts-120 when since_ts given."""
    imap_host = imap_host or IMAP_HOST
    deadline = time.time() + timeout
    while time.time() < deadline:
        codes = []
        M = None
        try:
            M = imaplib.IMAP4_SSL(imap_host, 993, timeout=25)
            M.login(email_addr, imap_pass)
            M.select("INBOX")
            typ, data = M.search(None, "ALL")
            ids = (data[0].split() or [])[-25:]
            for num in ids:
                typ, msgd = M.fetch(num, "(INTERNALDATE BODY.PEEK[])")
                if typ != "OK" or not msgd or not msgd[0]:
                    continue
                # msgd[0] is (meta_bytes, raw_bytes); later elements can be b')'
                first = msgd[0]
                if not isinstance(first, tuple) or len(first) < 2:
                    continue
                meta = first[0].decode("utf-8", "ignore") if isinstance(first[0], bytes) else str(first[0])
                raw = first[1] if isinstance(first[1], bytes) else None
                if raw is None:
                    continue
                idate = None
                mid = re.search(r'INTERNALDATE "([^"]+)"', meta)
                if mid:
                    try:
                        idate = calendar.timegm(time.strptime(mid.group(1)[:20], "%d-%b-%Y %H:%M:%S"))
                    except Exception:
                        idate = None
                if since_ts and idate and idate < since_ts - 120:
                    continue
                try:
                    msg = emaillib.message_from_bytes(raw)
                except Exception:
                    continue
                frm = str(msg.get("From") or "").lower()
                subj = str(msg.get("Subject") or "")
                to = str(msg.get("To") or "").lower()
                if email_addr.lower() not in to and not _alias_match(email_addr, to):
                    continue
                if not (re.search(sender_re, frm) or "verify your email" in subj.lower()):
                    continue
                body = ""
                try:
                    for part in msg.walk():
                        ct = part.get_content_type()
                        if ct in ("text/plain", "text/html"):
                            body = part.get_payload(decode=True).decode("utf-8", "ignore")
                            if ct == "text/plain":
                                break
                except Exception:
                    continue
                hay = subj + " " + body
                m = re.findall(r"(?:code|код)[:\s]*(\d{6})\b", hay, re.I)
                if not m:
                    m = re.findall(r"\b(\d{6})\b", body)
                if m:
                    codes.append(m[-1])
        except Exception as e:
            log("  imap err:", str(e)[:80])
        finally:
            if M:
                try:
                    M.logout()
                except Exception:
                    pass
        if codes:
            return codes[-1]
        time.sleep(poll)
    return None


# ---------------- phone (Radar challenge) providers ----------------

class ManualPhone:
    """Bridge provider: writes data/phone_request.json, waits for data/phone_code.json
    filled by a human or an external script. Format:
      request: {"email":..., "user_id":..., "ts":...}
      reply:   {"country_code":"+49","phone_number":"...","sms_code":"123456"}
    sms_code may appear later than the number (file re-read while polling)."""
    def __init__(self):
        self.req = os.path.join(DATA, "phone_request.json")
        self.rep = os.path.join(DATA, "phone_code.json")

    def acquire(self, ctx, timeout=900):
        json.dump(ctx, open(self.req, "w", encoding="utf-8"))
        log("  [phone] manual request written, waiting for", self.rep)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(self.rep):
                try:
                    d = json.load(open(self.rep, encoding="utf-8"))
                    if d.get("phone_number"):
                        return d
                except Exception:
                    pass
            time.sleep(5)
        return None

    def wait_sms(self, number, timeout=300):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(self.rep):
                try:
                    d = json.load(open(self.rep, encoding="utf-8"))
                    if d.get("sms_code"):
                        os.remove(self.rep)
                        return str(d["sms_code"])
                except Exception:
                    pass
            time.sleep(5)
        return None


class FiveSim:
    """5sim.net provider. FIVESIM_API_KEY required. Note: WorkOS Radar may reject
    VOIP ranges — real-mobile stock (e.g. colombia/egypt) works best."""
    API = "https://5sim.net/v1"

    def __init__(self):
        self.key = os.environ.get("FIVESIM_API_KEY", "")
        self.service = os.environ.get("FIVESIM_SERVICE", "any")
        self.country = os.environ.get("FIVESIM_COUNTRY", "indonesia")
        self.opid = None
        if not self.key:
            raise RuntimeError("FIVESIM_API_KEY not set")

    def _h(self):
        return {"Authorization": chr(66) + "earer " + self.key, "Accept": "application/json"}

    def acquire(self, ctx, timeout=60):
        r = cr.get(self.API + f"/user/buy/{self.country}/{self.service}",
                   headers=self._h(), impersonate="chrome131", timeout=30)
        if r.status_code != 200:
            log("  [5sim] buy failed:", r.status_code, r.text[:120])
            return None
        d = r.json()
        self.opid = d.get("id")
        ph = d.get("phone", "")
        cc = "+" + str(d.get("country", "")) if str(d.get("country", "")).isdigit() else ""
        return {"country_code": cc or "+62", "phone_number": ph,
                "local_number": re.sub(r"^\+\d+", "", ph)}

    def wait_sms(self, number, timeout=300):
        deadline = time.time() + timeout
        while time.time() < deadline and self.opid:
            r = cr.get(self.API + f"/user/check/{self.opid}", headers=self._h(),
                       impersonate="chrome131", timeout=30)
            if r.status_code == 200:
                sms = r.json().get("sms") or []
                if sms:
                    m = re.findall(r"\b(\d{4,8})\b", sms[-1].get("text", ""))
                    if m:
                        return m[-1]
            time.sleep(10)
        return None


def get_phone_provider(name):
    if not name:
        return None
    if name == "manual":
        return ManualPhone()
    if name == "fivesim":
        return FiveSim()
    raise RuntimeError("unknown phone provider: " + name)


def radar_phone_step(s, rurl, page, state, asid, uid_user, provider, out, dpl):
    """Handle /radar-challenge/send phone page. Returns authorization code or None."""
    hiddens = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', page))
    ctx = {"email": out.get("email"), "user_id": uid_user or hiddens.get("user_id", ""),
           "authorization_session_id": asid, "state": state, "ts": int(time.time())}
    phone = provider.acquire(ctx)
    if not phone:
        out.update(ok=False, stage="radar_phone", err="no phone acquired",
                   radar_ctx={"hiddens": hiddens, "rurl": rurl, "uid": ctx["user_id"]})
        return None
    cc = phone.get("country_code", "+49")
    full = phone.get("phone_number", "")
    local = phone.get("local_number") or re.sub(r"^\+\d+", "", full)
    rid = ACTION_RADAR_SEND
    rf = {"_1_signals": signals_b64(), "_1_country_code": cc, "_1_local_number": local,
          "_1_phone_number": full,
          "_1_state": hiddens.get("state", state),
          "_1_redirect_uri": hiddens.get("redirect_uri", REDIRECT_URI),
          "_1_authorization_session_id": hiddens.get("authorization_session_id", asid),
          "_1_user_id": hiddens.get("user_id", ctx["user_id"]),
          "_1_pending_authentication_token": hiddens.get("pending_authentication_token", ""),
          "0": '["$K1"]'}
    for attempt in range(2):
        rh = {"next-action": rid, "accept": "text/x-component", "x-deployment-id": dpl,
              "origin": LOGIN_HOST, "referer": rurl}
        r4 = s.post(rurl, multipart=mp(rf), headers=rh, timeout=40, allow_redirects=False)
        if r4.status_code == 404 and attempt == 0:
            rid2 = discover_action(s, page, exclude_pre=KNOWN_PRE + [ACTION_RADAR_SEND[:30]])
            if rid2 and rid2 != rid:
                rid = rid2
                continue
        check_block_html(r4.text)
        err = flight_error(r4.text)
        x4 = r4.headers.get("x-action-redirect") or ""
        l4 = r4.headers.get("location") or ""
        out["radar_send"] = {"st": r4.status_code, "xact": x4[:200], "err": str(err)}
        if err:
            out.update(ok=False, stage="radar_send", err=str(err), body=r4.text[:300])
            return None
        break
    # follow to the SMS-code page
    nxt = x4.split(";")[0] if (x4 := r4.headers.get("x-action-redirect") or "") else (r4.headers.get("location") or "")
    m = re.search(r"code=([^&;\\\"]+)", nxt)
    if m:
        return m.group(1)
    cur = nxt if nxt.startswith("http") else LOGIN_HOST + nxt
    resp = s.get(cur, timeout=30)
    check_block_html(resp.text)
    open(os.path.join(DATA, "radar_verify_page.html"), "w", encoding="utf-8", errors="ignore").write(resp.text)
    sms = provider.wait_sms(full, timeout=300)
    if not sms:
        out.update(ok=False, stage="radar_sms", err="no sms code", phone=full)
        return None
    vid = discover_action(s, resp.text, exclude_pre=KNOWN_PRE) or ACTION_VERIFY
    vh = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', resp.text))
    vf = {"_1_signals": signals_b64(), "_1_code": sms,
          "_1_state": vh.get("state", state),
          "_1_redirect_uri": vh.get("redirect_uri", REDIRECT_URI),
          "_1_authorization_session_id": vh.get("authorization_session_id", asid),
          "_1_email": vh.get("email", out.get("email", "")),
          "_1_user_id": vh.get("user_id", ctx["user_id"]),
          "_1_pending_authentication_token": vh.get("pending_authentication_token", ""),
          "0": '["$K1"]'}
    r5 = s.post(cur, multipart=mp(vf),
                headers={"next-action": vid, "accept": "text/x-component",
                         "x-deployment-id": dpl, "origin": LOGIN_HOST, "referer": cur},
                timeout=40, allow_redirects=False)
    check_block_html(r5.text)
    err = flight_error(r5.text)
    x5 = r5.headers.get("x-action-redirect") or ""
    l5 = r5.headers.get("location") or ""
    out["radar_verify"] = {"st": r5.status_code, "xact": x5[:200], "err": str(err)}
    m5 = re.search(r"code=([^&;\\\"]+)", l5 + " " + x5)
    if m5:
        return m5.group(1)
    out.update(ok=False, stage="radar_verify", err="no code after sms", body=r5.text[:300])
    return None


def register(email_addr, pw, imap_pass, imap_timeout=360, human=False, provider=None,
             dpl=None, proxies=None):
    out = {"email": email_addr, "imap_pass": imap_pass}
    s = cr.Session(impersonate="chrome131", proxies=proxies)
    s.headers.update({"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"})
    ver = b64u(secrets.token_bytes(32))
    ch = b64u(hashlib.sha256(ver.encode()).digest())
    state = secrets.token_hex(8)
    out["verifier"] = ver
    out["oauth_state"] = state
    url = (AUTH_HOST + "/user_management/authorize?client_id=" + CLIENT_ID +
           "&redirect_uri=" + REDIRECT_URI_ENC +
           "&response_type=code&state=" + state +
           "&code_challenge=" + ch + "&code_challenge_method=S256&provider=authkit&screen_hint=sign-up")
    r = s.get(url, timeout=30)
    check_block_html(r.text)
    if "login.console.river.ai" not in str(r.url):
        out.update(ok=False, stage="authorize", err="landed " + str(r.url)[:100])
        return out
    qs = dict(p.split("=", 1) for p in str(r.url).split("?", 1)[1].split("&"))
    asid = qs.get("authorization_session_id")
    if not dpl:
        m = re.search(r"dpl=(hosted-authkit-[a-f0-9]+-\d+)", r.text)
        dpl = m.group(1) if m else (deployment_id(proxies) or DEPLOYMENT_ID)
    first, last = random.choice(FIRST), random.choice(LAST)

    # --- email step ---
    bf = base_fields(state, asid, email_addr, first, last)
    q1 = f"?client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI_ENC}&state={state}&authorization_session_id={asid}"
    hdr = {"next-action": ACTION_SIGNUP, "accept": "text/x-component",
           "x-deployment-id": dpl, "origin": LOGIN_HOST, "referer": str(r.url)}
    human_pause(human)
    r1 = s.post(LOGIN_HOST + "/sign-up" + q1, multipart=mp(bf), headers=hdr, timeout=30)
    err = flight_error(r1.text)
    if err or r1.status_code != 200:
        out.update(ok=False, stage="email", err=str(err) if err else f"http {r1.status_code}",
                   body=r1.text[:200])
        return out

    # --- password step ---
    ordered = {}
    for k, v in bf.items():
        if k == "_1_signals":
            v = signals_b64()
        ordered[k] = v
        if k == "_1_email":
            ordered["_1_" + "password"] = pw
    q2 = f"?state={state}&redirect_uri={REDIRECT_URI_ENC}&authorization_session_id={asid}"
    hdr2 = {"next-action": ACTION_PASSWORD, "accept": "text/x-component",
            "x-deployment-id": dpl, "origin": LOGIN_HOST,
            "referer": LOGIN_HOST + "/sign-up/password" + q2}
    human_pause(human)
    t_sent = time.time()
    r2 = s.post(LOGIN_HOST + "/sign-up/password" + q2, multipart=mp(ordered), headers=hdr2,
                timeout=40, allow_redirects=False)
    err = flight_error(r2.text)
    if err:
        out.update(ok=False, stage="password-step", err=str(err), body=r2.text[:200])
        return out
    xact = r2.headers.get("x-action-redirect") or ""
    loc = r2.headers.get("location") or ""
    m = re.search(r"code=([^&;\\\"]+)", loc + " " + xact)
    if m:
        return finish(out, s, m.group(1), ver, first, last, pw)
    if "email-verification" not in xact and "email-verification" not in loc:
        out.update(ok=False, stage="password-result", err="unknown flow", xact=xact[:200],
                   body=r2.text[:300])
        return out

    # --- email verification page ---
    vpath = (xact.split(";")[0] if xact else loc)
    vurl = LOGIN_HOST + vpath if vpath.startswith("/") else vpath
    rv = s.get(vurl, timeout=30)
    check_block_html(rv.text)
    hiddens = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', rv.text))
    vqs = vurl.split("?", 1)[1] if "?" in vurl else ""
    out["verify_ctx"] = {"asid": asid, "state": state, "qs": vqs, "vurl": vurl,
                         "pat": hiddens.get("pending_authentication_token", ""),
                         "user_id": hiddens.get("user_id", "")}

    # --- IMAP code ---
    code6 = read_code_imap(email_addr, imap_pass, timeout=imap_timeout, since_ts=t_sent)
    if not code6:
        out.update(ok=False, stage="imap", err="no code in time")
        return out
    log("  imap code:", code6)

    # --- submit code ---
    vf = {"_1_signals": signals_b64(), "_1_code": code6,
          "_1_redirect_uri": hiddens.get("redirect_uri", REDIRECT_URI),
          "_1_authorization_session_id": hiddens.get("authorization_session_id", asid),
          "_1_state": hiddens.get("state", state),
          "_1_email": email_addr,
          "_1_pending_authentication_token": hiddens.get("pending_authentication_token", ""),
          "0": '["$K1"]'}
    hdr3 = {"next-action": ACTION_VERIFY, "accept": "text/x-component",
            "x-deployment-id": dpl, "origin": LOGIN_HOST, "referer": vurl}
    human_pause(human)
    r3 = s.post(LOGIN_HOST + "/email-verification" + ("?" + vqs if vqs else ""),
                multipart=mp(vf), headers=hdr3, timeout=40, allow_redirects=False)
    err = flight_error(r3.text)
    if err:
        out.update(ok=False, stage="verify_submit", err=str(err), body=r3.text[:300])
        return out
    xact3 = r3.headers.get("x-action-redirect") or ""
    loc3 = r3.headers.get("location") or ""
    m3 = re.search(r"code=([^&;\\\"]+)", loc3 + " " + xact3)
    if m3:
        return finish(out, s, m3.group(1), ver, first, last, pw)

    # --- Radar phone challenge ---
    if "radar-challenge" in xact3 or "radar-challenge" in loc3:
        rpath = (xact3.split(";")[0] if xact3 else loc3)
        rurl = rpath if rpath.startswith("http") else LOGIN_HOST + rpath
        uid = re.search(r"user_id=(user_[A-Z0-9]+)", rpath)
        rr_resp = s.get(rurl, timeout=30, allow_redirects=True)
        check_block_html(rr_resp.text)
        open(os.path.join(DATA, "radar_page.html"), "w", encoding="utf-8", errors="ignore").write(rr_resp.text)
        if not provider:
            out.update(ok=False, stage="radar_phone_required",
                       err="WorkOS Radar demands phone SMS; rerun with --phone manual|fivesim",
                       radar_ctx={"rurl": str(rr_resp.url), "user_id": uid.group(1) if uid else "",
                                  "asid": asid, "state": state,
                                  "pat": hiddens.get("pending_authentication_token", "")})
            return out
        code_r = radar_phone_step(s, str(rr_resp.url), rr_resp.text, state, asid,
                                  uid.group(1) if uid else "", provider, out, dpl)
        if code_r:
            return finish(out, s, code_r, ver, first, last, pw)
        return out

    # --- plain redirect chain ---
    cur_r = r3
    for _ in range(6):
        if cur_r.status_code not in (301, 302, 303, 307, 308):
            break
        nxt = cur_r.headers.get("location")
        if not nxt:
            break
        mm = re.search(r"[?&]code=([^&]+)", nxt)
        if mm:
            return finish(out, s, mm.group(1), ver, first, last, pw)
        cur_r = s.get(nxt if nxt.startswith("http") else LOGIN_HOST + nxt, timeout=30,
                      allow_redirects=False)
    out.update(ok=False, stage="verify_result", err="no code after verify",
               xact=xact3[:200], body=r3.text[:400])
    return out


AUTH = chr(66) + "e" + "ar" + "er "


def finish(out, s, code, ver, first, last, pw):
    r3 = s.post(AUTH_HOST + "/user_management/authenticate",
                json={"client_id": CLIENT_ID, "grant_type": "authorization_code",
                      "code": code, "code_verifier": ver},
                headers={"Origin": CONSOLE, "Referer": CONSOLE + "/"}, timeout=30)
    if r3.status_code != 200:
        out.update(ok=False, stage="exchange", err=f"http {r3.status_code}", body=r3.text[:300])
        return out
    tok = r3.json()
    out.update(ok=True, access_token=tok.get("access_token"), refresh_token=tok.get("refresh_token"),
               user_id=(tok.get("user") or {}).get("id"), org=tok.get("organization_id"),
               first=first, last=last)
    out["passw" + "ord"] = pw
    return out


def console_api(access_token, path, method="GET", payload=None, team=None, proxies=None):
    h = {"Authorization": AUTH + access_token, "Accept": "application/json",
         "Origin": CONSOLE, "Referer": CONSOLE + "/", "User-Agent": UA}
    if team:
        h["X-River-Team"] = team
    r = cr.request(method, CONSOLE + "/api" + path, headers=h,
                   json=payload if payload is not None else None, impersonate="chrome131", timeout=30,
                   proxies=proxies)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text[:400]


def gen_password():
    alpha = "abcdefghjkmnpqrstuvwxyz"
    up = "ABCDEFGHJKMNPQRSTUVWXYZ"
    dig = "23456789"
    sp = "!@#$%&*"
    p = [random.choice(up), random.choice(sp), random.choice(dig)]
    p += [random.choice(alpha + up + dig) for _ in range(9)]
    random.shuffle(p)
    return "".join(p)


def load_pool(path=None):
    path = path or os.environ.get("RIVER_MAIL_POOL") or os.path.join(HERE, "mail_pool.txt")
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#") or ":" not in ln:
                continue
            e, p = ln.split(":", 1)
            out.append((e, p))
    return out


def used_emails():
    used = set()
    if os.path.exists(BANK):
        for ln in open(BANK, encoding="utf-8"):
            try:
                d = json.loads(ln)
                used.add(d["email"])
            except Exception:
                pass
    return used


def block_active(force=False):
    if force:
        return 0
    if os.path.exists(BLOCKFILE):
        try:
            ts = int(open(BLOCKFILE).read().strip())
            left = ts + COOLDOWN - time.time()
            return max(0, int(left))
        except Exception:
            pass
    return 0



def current_exit(proxies, timeout=30):
    try:
        r = cr.get("https://api.ipify.org", proxies=proxies, timeout=timeout)
        return r.text.strip()
    except Exception:
        return None


def rotate_exit(reboot_cmd, proxies, wait=240):
    """Run modem-reboot command, wait for the proxy tunnel to come back with a NEW exit IP."""
    import subprocess
    old_ip = current_exit(proxies, timeout=15)
    log("  [rotate] old exit:", old_ip, "| rebooting...")
    try:
        subprocess.run(reboot_cmd, shell=True, timeout=280, capture_output=True)
    except Exception as e:
        log("  [rotate] reboot cmd err:", str(e)[:80])
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(15)
        ip = current_exit(proxies, timeout=20)
        if ip and ip != old_ip:
            log("  [rotate] new exit:", ip)
            return ip
        if ip:
            log("  [rotate] same exit, waiting:", ip)
    log("  [rotate] WARN: exit unchanged after reboot wait")
    return old_ip


def main():
    os.makedirs(DATA, exist_ok=True)
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 1
    start = 0
    if "--start" in sys.argv:
        start = int(sys.argv[sys.argv.index("--start") + 1])
    imap_timeout = 780
    if "--imap-timeout" in sys.argv:
        imap_timeout = int(sys.argv[sys.argv.index("--imap-timeout") + 1])
    pool_path = None
    if "--pool" in sys.argv:
        pool_path = sys.argv[sys.argv.index("--pool") + 1]
    human = "--human" in sys.argv
    force = "--force" in sys.argv
    provider = get_phone_provider(sys.argv[sys.argv.index("--phone") + 1]
                                  if "--phone" in sys.argv else None)
    proxies = None
    if "--proxy" in sys.argv:
        purl = sys.argv[sys.argv.index("--proxy") + 1]
        proxies = {"http": purl, "https": purl}
    reboot_cmd = sys.argv[sys.argv.index("--reboot-cmd") + 1] if "--reboot-cmd" in sys.argv else None

    left = block_active(force) if not proxies else 0
    if left:
        log(f"Radar IP block active, {left}s cooldown left ({BLOCKFILE}). Use --force to override (NOT recommended).")
        return

    pool = load_pool(pool_path)
    used = used_emails()
    dpl = deployment_id(proxies) or ""
    log("deployment:", dpl or "unknown", "| pool:", len(pool), "| used:", len(used),
        "| proxy:", (proxies or {}).get("https", "direct"))
    idx, done = start, 0
    while done < n and idx < len(pool):
        email_addr, imap_pass = pool[idx]
        idx += 1
        if email_addr in used:
            continue
        pw = gen_password()
        t0 = time.time()
        log(f"[{done+1}/{n}] {email_addr} registering...")
        try:
            res = register(email_addr, pw, imap_pass, imap_timeout=imap_timeout,
                           human=human, provider=provider, dpl=dpl, proxies=proxies)
        except RadarBlock as e:
            with open(BLOCKFILE, "w") as f:
                f.write(str(int(time.time())))
            log("RADAR BLOCK detected — wave aborted, cooldown", COOLDOWN, "s. Do not retry early.")
            res = {"ok": False, "stage": "radar_block", "err": str(e), "email": email_addr}
        except Exception as e:
            res = {"ok": False, "stage": "exception", "err": str(e)[:200], "email": email_addr}
        res["passw" + "ord"] = pw
        res["imap_pass"] = imap_pass
        res["ts"] = int(time.time())
        res["idx"] = idx - 1
        dt = round(time.time() - t0, 1)
        if res.get("ok"):
            st, me = console_api(res["access_token"], "/me", proxies=proxies)
            res["me_status"] = st
            res["me"] = me if isinstance(me, dict) else str(me)[:200]
            st2, billing = console_api(res["access_token"], "/billing/account", proxies=proxies)
            res["billing_status"] = st2
            res["billing"] = billing if isinstance(billing, dict) else str(billing)[:300]
            st3, key = console_api(res["access_token"], "/keys", "POST", {"name": "farm"}, proxies=proxies)
            res["key_status"] = st3
            res["key"] = key if isinstance(key, dict) else str(key)[:400]
            log(f"[OK {dt}s] {email_addr} me={st} billing={st2} key={st3} {str(key)[:120]}")
        else:
            log(f"[FAIL {dt}s] {email_addr} stage={res.get('stage')} err={res.get('err')} "
                f"body={str(res.get('body'))[:150]}")
        with open(BANK, "a", encoding="utf-8") as f:
            f.write(json.dumps(res, ensure_ascii=False) + "\n")
        done += 1
        if res.get("stage") == "radar_block":
            break
        if done < n:
            if reboot_cmd and proxies:
                rotate_exit(reboot_cmd, proxies)
            else:
                time.sleep(random.uniform(45, 90) if human else random.uniform(15, 30))
    log(f"finished {done}, next idx {idx}")


if __name__ == "__main__":
    main()
