"""Turnstile solvers for 2nd-no.com reservations (and any Cloudflare turnstile).

Backends (tried in order by CaptchaRouter):
1. SidecarSolver — local captcha-solver sidecar (POST {SIDECAR_URL}/solve).
   FREE, ~14s/token, requires sidecar running with real_page support.
   Health: GET {SIDECAR_URL}/health → 200.
2. TwoCaptchaTurnstile — 2captcha.com, method=turnstile.
   IMPORTANT: submit ONLY via in.php (res.php returns ERROR_EMPTY_METHOD);
   balance check is POST res.php {action:getbalance}, balance lives in 'request'.
   Key source (first match wins):
     env TWOCAPTCHA_API_KEY
     env RUCAPTCHA_KEY
     data/secondno/captcha_keys.json  {"keys":[{"key":"...","label":"..."}]}
   See captcha_keys.example.json for the template.

CaptchaRouter: tries sidecar first; on failure/timeout falls back to 2captcha.
Retries each backend up to `retries` times before giving up.

get_solver(): returns a CaptchaRouter (sidecar+2captcha fallback chain).
get_sidecar(): returns SidecarSolver only (no fallback).
get_2captcha(): returns TwoCaptchaTurnstile only (no fallback).
test_solve(sitekey, pageurl): quick smoke test — returns {"ok":bool,"backend":str,"elapsed":float}.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request

HOME = os.environ.get("SECONDNO_HOME") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "secondno")
KEYS_FILE = os.path.join(HOME, "captcha_keys.json")
SIDECAR_URL = os.environ.get("SIDECAR_URL", "http://127.0.0.1:8877")

# 2nd-no.com turnstile sitekey (used by test_solve default)
DEFAULT_SITEKEY = "0x4AAAAAAAh6YYTPTzEcN3Ep"
DEFAULT_PAGEURL = "https://2nd-no.com/"


def _load_2captcha_key():
    """Find first 2captcha key with balance > $0.05. Checks env then keys file."""
    candidates = []
    for env_var in ("TWOCAPTCHA_API_KEY", "RUCAPTCHA_KEY"):
        v = os.environ.get(env_var, "").strip()
        if v:
            candidates.append({"key": v, "label": f"env:{env_var}"})
    try:
        d = json.load(open(KEYS_FILE, encoding="utf-8"))
        if isinstance(d, dict):
            candidates += d.get("keys") or ([d] if d.get("key") else [])
        elif isinstance(d, list):
            candidates += [k if isinstance(k, dict) else {"key": str(k)} for k in d]
    except Exception:
        pass
    for k in candidates:
        key = k.get("key", "").strip()
        if not key:
            continue
        try:
            body = urllib.parse.urlencode(
                {"key": key, "action": "getbalance", "json": 1}).encode()
            req = urllib.request.Request("https://2captcha.com/res.php", data=body)
            r = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
            bal = float(r.get("request", -1))
            label = k.get("label", "?")
            print(f"[2captcha] {label} balance ${bal:.3f}", flush=True)
            if r.get("status") == 1 and bal > 0.05:
                return key
        except Exception as e:
            print("[2captcha] balance check err:", str(e)[:80], flush=True)
    return ""


class TwoCaptchaTurnstile:
    """2captcha.com turnstile solver. Submit via in.php, poll via res.php."""

    def __init__(self, key=None):
        self.key = key or _load_2captcha_key()
        if not self.key:
            raise RuntimeError(
                "no 2captcha key with balance — set TWOCAPTCHA_API_KEY or RUCAPTCHA_KEY env, "
                f"or add key to {KEYS_FILE} (see captcha_keys.example.json)")

    def _post(self, url, data, timeout=40):
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def turnstile(self, sitekey, pageurl, action=None, proxy_url=None):
        d = self._post("https://2captcha.com/in.php", {
            "key": self.key, "method": "turnstile", "sitekey": sitekey,
            "pageurl": pageurl, "json": 1})
        if d.get("status") != 1:
            raise RuntimeError(f"2captcha submit: {d}")
        task_id = d["request"]
        print(f"[2captcha] task {task_id}", flush=True)
        deadline = time.time() + 240
        while time.time() < deadline:
            time.sleep(8)
            r = self._post("https://2captcha.com/res.php", {
                "key": self.key, "action": "get", "id": task_id, "json": 1})
            if r.get("status") == 1:
                tok = r["request"]
                if isinstance(tok, dict):
                    tok = tok.get("token") or tok.get("code")
                return tok
            err = str(r.get("request", ""))
            if "NOT_READY" not in err:
                raise RuntimeError(f"2captcha get: {r}")
        raise RuntimeError("2captcha timeout (240s)")

    def balance(self):
        try:
            r = self._post("https://2captcha.com/res.php",
                           {"key": self.key, "action": "getbalance", "json": 1})
            return float(r.get("request", -1)) if r.get("status") == 1 else -1
        except Exception:
            return -1


class SidecarSolver:
    """Local captcha-solver sidecar (POST /solve). Free, ~14s/turnstile token."""

    def __init__(self, url=None):
        self.url = (url or SIDECAR_URL).rstrip("/")

    def alive(self):
        import httpx
        try:
            return httpx.get(self.url + "/health", timeout=5).status_code == 200
        except Exception:
            try:
                return httpx.get(self.url + "/", timeout=5).status_code < 500
            except Exception:
                return False

    def turnstile(self, sitekey, pageurl, action=None, proxy_url=None):
        import httpx
        t0 = time.time()
        r = httpx.post(self.url + "/solve", json={
            "type": "turnstile", "sitekey": sitekey, "url": pageurl,
            "real_page": True, "timeout_s": 180}, timeout=200)
        d = r.json()
        if not d.get("solved") or not d.get("token"):
            raise RuntimeError("sidecar turnstile fail: " + json.dumps(d)[:200])
        elapsed = round(time.time() - t0, 1)
        print(f"[sidecar] turnstile solved in {elapsed}s", flush=True)
        return d["token"]


class CaptchaRouter:
    """Tries backends in order with retries. Falls back on failure/timeout.

    backends: list of solver instances (SidecarSolver, TwoCaptchaTurnstile, ...).
    retries: attempts per backend before moving to next.
    """

    def __init__(self, backends=None, retries=2):
        self.backends = backends or []
        self.retries = retries
        self.last_backend = None
        self.last_error = None

    def turnstile(self, sitekey, pageurl, action=None, proxy_url=None):
        errors = []
        for b in self.backends:
            name = type(b).__name__
            for attempt in range(1, self.retries + 1):
                try:
                    tok = b.turnstile(sitekey, pageurl, action=action, proxy_url=proxy_url)
                    if tok:
                        self.last_backend = name
                        self.last_error = None
                        return tok
                    errors.append(f"{name} attempt {attempt}: empty token")
                except Exception as e:
                    msg = f"{name} attempt {attempt}: {str(e)[:120]}"
                    errors.append(msg)
                    print(f"[captcha-router] {msg}", flush=True)
                    if attempt < self.retries:
                        time.sleep(3)
        self.last_backend = None
        self.last_error = "; ".join(errors[-4:])
        raise RuntimeError(f"all captcha backends failed: {self.last_error}")

    def status(self):
        """Return dict with backend health info (no secrets)."""
        out = []
        for b in self.backends:
            name = type(b).__name__
            info = {"backend": name, "alive": False}
            if isinstance(b, SidecarSolver):
                info["alive"] = b.alive()
                info["url"] = b.url
            elif isinstance(b, TwoCaptchaTurnstile):
                info["alive"] = True  # key loaded = alive
                info["balance"] = round(b.balance(), 3)
            out.append(info)
        return out


def _build_backends():
    """Build ordered backend list: sidecar (if alive) → 2captcha (if key)."""
    backends = []
    s = SidecarSolver()
    if s.alive():
        backends.append(s)
        print("[captcha] sidecar alive at", s.url, flush=True)
    try:
        backends.append(TwoCaptchaTurnstile())
        print("[captcha] 2captcha key loaded", flush=True)
    except RuntimeError as e:
        print("[captcha] 2captcha unavailable:", str(e)[:80], flush=True)
    return backends


def get_solver():
    """Returns CaptchaRouter with sidecar+2captcha fallback chain."""
    backends = _build_backends()
    if not backends:
        raise RuntimeError(
            "no captcha backend available — start sidecar (:8877) or set TWOCAPTCHA_API_KEY")
    return CaptchaRouter(backends=backends, retries=2)


def get_sidecar():
    """Returns SidecarSolver only (no fallback). Raises if not alive."""
    s = SidecarSolver()
    if not s.alive():
        raise RuntimeError(f"sidecar not alive at {s.url}")
    return s


def get_2captcha():
    """Returns TwoCaptchaTurnstile only (no fallback). Raises if no key."""
    return TwoCaptchaTurnstile()


def test_solve(sitekey=None, pageurl=None):
    """Quick smoke test — solve a turnstile and return result dict (no secrets)."""
    sitekey = sitekey or DEFAULT_SITEKEY
    pageurl = pageurl or DEFAULT_PAGEURL
    t0 = time.time()
    try:
        router = get_solver()
        tok = router.turnstile(sitekey, pageurl)
        return {
            "ok": True,
            "backend": router.last_backend,
            "elapsed": round(time.time() - t0, 1),
            "token_len": len(tok) if tok else 0,
            "token_preview": (tok[:8] + "..." + tok[-4:]) if tok and len(tok) > 16 else "***",
        }
    except Exception as e:
        return {
            "ok": False,
            "elapsed": round(time.time() - t0, 1),
            "err": str(e)[:200],
        }


def captcha_status():
    """Return captcha subsystem status for dashboard (no secrets)."""
    sidecar = SidecarSolver()
    sc_alive = sidecar.alive()
    has_2captcha = False
    bal_2captcha = -1
    try:
        tc = TwoCaptchaTurnstile()
        has_2captcha = True
        bal_2captcha = round(tc.balance(), 3)
    except Exception:
        pass
    return {
        "sidecar_alive": sc_alive,
        "sidecar_url": sidecar.url,
        "has_2captcha_key": has_2captcha,
        "balance_2captcha": bal_2captcha,
        "keys_file": KEYS_FILE,
        "keys_file_exists": os.path.isfile(KEYS_FILE),
        "ready": sc_alive or has_2captcha,
    }
