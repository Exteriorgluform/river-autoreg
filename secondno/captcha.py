"""Turnstile solvers for 2nd-no.com reservations.

Two backends:
1. TwoCaptchaTurnstile — 2captcha.com, method=turnstile.
   IMPORTANT: submit ONLY via in.php (res.php returns ERROR_EMPTY_METHOD);
   balance check is POST res.php {action:getbalance}, balance lives in 'request' field.
   Key source: env TWOCAPTCHA_API_KEY, or data/captcha_keys.json {"keys":[{"key":...,"label":...}]}.
2. SidecarSolver — local captcha-solver sidecar (POST {SIDECAR_URL}/solve), free,
   requires the sidecar running with real_page support.

get_solver(): sidecar if reachable, else 2captcha if a key with balance exists.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request

HOME = os.environ.get("SECONDNO_HOME") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
KEYS_FILE = os.path.join(HOME, "captcha_keys.json")
SIDECAR_URL = os.environ.get("SIDECAR_URL", "http://127.0.0.1:8877")


def _load_2captcha_key():
    env_key = os.environ.get("TWOCAPTCHA_API_KEY", "").strip()
    keys = [{"key": env_key, "label": "env"}] if env_key else []
    try:
        d = json.load(open(KEYS_FILE, encoding="utf-8"))
        keys += d.get("keys") or ([d] if isinstance(d, dict) and d.get("key") else [])
    except Exception:
        pass
    for k in keys:
        try:
            body = urllib.parse.urlencode(
                {"key": k["key"], "action": "getbalance", "json": 1}).encode()
            req = urllib.request.Request("https://2captcha.com/res.php", data=body)
            r = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
            bal = float(r.get("request", -1))
            print(f"[2captcha] {k.get('label', '?')} balance {bal}", flush=True)
            if r.get("status") == 1 and bal > 0.05:
                return k["key"]
        except Exception as e:
            print("[2captcha] balance check err:", str(e)[:80], flush=True)
    return ""


class TwoCaptchaTurnstile:
    def __init__(self, key=None):
        self.key = key or _load_2captcha_key()
        if not self.key:
            raise RuntimeError("no 2captcha key with balance")

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
        print("[2captcha] task", task_id, flush=True)
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
        raise RuntimeError("2captcha timeout")


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
        r = httpx.post(self.url + "/solve", json={
            "type": "turnstile", "sitekey": sitekey, "url": pageurl,
            "real_page": True, "timeout_s": 180}, timeout=200)
        d = r.json()
        if not d.get("solved") or not d.get("token"):
            raise RuntimeError("sidecar turnstile fail: " + json.dumps(d)[:200])
        print("[sidecar] turnstile solved in", d.get("elapsed"), flush=True)
        return d["token"]


def get_solver():
    s = SidecarSolver()
    if s.alive():
        return s
    return TwoCaptchaTurnstile()
