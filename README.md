# river-autoreg

Fast HTTP-only autoreg for **River.ai** (`console.river.ai`, WorkOS AuthKit) — no browser
in the hot path. `curl_cffi` (chrome131 impersonation) replays the AuthKit Next.js
server-action flow end to end, reads the email code over IMAP, exchanges the PKCE code
for an access token and pulls a `rv_*` API key from the console API.

## Status (2026-10-20)

| Step | Status |
|------|--------|
| PKCE authorize | ✅ |
| Email step (`/sign-up` server action) | ✅ |
| Password step (`/sign-up/password` server action) | ✅ (user created) |
| Email verification (`/email-verification` + IMAP code) | ✅ |
| **WorkOS Radar phone challenge** (`/radar-challenge/send`) | ⚠️ gate — needs SMS (`--phone`) |
| Token exchange + console API key | implemented, not yet reached in the wild |

**The wall:** after ~7 signups from one IP, WorkOS Radar escalates to a *global IP-level*
`Access blocked, please contact support` (even in a real browser) and **every further
attempt extends the lock (~3–4 h)**. The driver detects this, writes `data/radar_block`
and aborts the wave. Respect the cooldown; pace at ≤1 signup / 30–60 min per IP, or
rotate clean residential IPs.

## Flow

```
auth.console.river.ai/user_management/authorize   (PKCE, screen_hint=sign-up)
  -> login.console.river.ai/sign-up               POST next-action 408cf059...
  -> login.console.river.ai/sign-up/password      POST next-action 409a26bd...
  -> login.console.river.ai/email-verification    IMAP 6-digit code (TTL 15 min)
                                                  POST next-action 400f86f0...
  -> [Radar] /radar-challenge/send                phone SMS, next-action 4037b59e...
  -> ?code=... redirect
  -> auth.console.river.ai/user_management/authenticate  (code + verifier)
  -> console.river.ai/api/me, /api/billing/account, POST /api/keys
```

Next.js server actions are replayed as `multipart/form-data` with headers
`next-action`, `accept: text/x-component`, `x-deployment-id` (fetched dynamically —
WorkOS redeploys rotate it). Field names carry the `_1_` flight-prefix
(`_1_email`, `_1_signals`, ...) plus `0=["$K1"]`; extra/unknown fields cause
`invalid_params`. If a deployment rotates the action ids, `discover_action()`
re-extracts them from the page's `page-*.js` chunk on 404.

## Setup

```bash
pip install -r requirements.txt          # curl_cffi
cp mail_pool.example.txt mail_pool.txt   # or point RIVER_MAIL_POOL at your pool
```

Mail pool format — one `email:imap_password` per line (default provider: t-online.de
IMAP `secureimap.t-online.de:993`; other providers: edit `IMAP_HOST` / pass
`imap_host` to `read_code_imap`).

## Usage

```bash
# single registration, human-like pauses, IMAP wait 13 min
python river_reg.py 1 --human --imap-timeout 780

# wave of 5 starting at pool index 100
python river_reg.py 5 --start 100

# with phone challenge handling
python river_reg.py 3 --phone manual      # you fill data/phone_code.json
FIVESIM_API_KEY=*** python river_reg.py 3 --phone fivesim

# free PL numbers via 2nd-no.com (vendored ./secondno package)
python secondno_register.py 1             # playwright; accounts -> data/secondno/state.json
python river_reg.py 3 --phone secondno    # REUSE-FIRST reservation + OTP poll

# finish accounts stuck at the phone step later
python complete_pending.py --phone fivesim

# dashboard (stdlib, dark UI, http://127.0.0.1:8910)
python dashboard.py            # or: python dashboard.py 8910
```

## Captcha (turnstile for 2nd-no reservations)

The vendored `secondno/captcha.py` provides a `CaptchaRouter` that tries backends
in order with retries:

1. **SidecarSolver** — local captcha-solver sidecar at `:8877` (FREE, ~14s/token).
   Health: `GET http://127.0.0.1:8877/health`. Start it separately (see your
   captcha-solver sidecar setup). Preferred backend.
2. **TwoCaptchaTurnstile** — 2captcha.com fallback. Key source (first match wins):
   - env `TWOCAPTCHA_API_KEY`
   - env `RUCAPTCHA_KEY`
   - `data/secondno/captcha_keys.json` (copy from `captcha_keys.example.json`)

   IMPORTANT: submit ONLY via `in.php` (res.php returns ERROR_EMPTY_METHOD);
   balance check is POST `res.php {action:getbalance}`, balance in `request` field.

`get_solver()` returns the router (sidecar → 2captcha fallback). The dashboard
shows live captcha status (sidecar alive, 2captcha balance) and has a
**🧩 Captcha-тест** button that solves a real 2nd-no turnstile end-to-end.

## Dashboard

`python dashboard.py [port]` (default 8910) — single-file stdlib server, dark UI.

- **Cards**: bank stats (OK/pending/failed), Radar block countdown, 2nd-no pool size,
  used numbers, mail pool, captcha status (sidecar/2captcha/ready).
- **Actions**: register 2nd-no accounts, launch river wave (`--phone secondno`),
  complete pending, reserve-test (live 2nd-no number), captcha-test (live turnstile solve).
- **Tables**: river bank accounts (email/stage/status/key/err), 2nd-no account pool.
- **Logs**: tail of reg2no.log / reserve_test.log / wave.log.
- Auto-refresh every 15s (toggleable). Secrets are masked server-side.

Results append to `data/river_bank.jsonl` (email, password, imap_pass, PKCE verifier,
tokens, key material, stage/err on failure). `data/` is gitignored.

## Pitfalls (all paid for in blood)

- **t-online alias trap:** `base-0001`, `base-001`, `base-01`, `base-1` are the SAME
  mailbox. IMAP must filter by exact `To:` header; letters lag up to 7+ minutes
  (`--imap-timeout 780`), and codes expire 15 min after send.
- **curl_cffi multipart:** only `CurlMime().addpart(name=..., data=bytes)` works;
  `files=dict` / `multipart=dict` raise `'dict' object has no attribute '_form'`.
- **Radar signals:** `_1_signals` is a base64 JSON fingerprint blob; timestamps are
  refreshed per call, hashes are static (one real machine). Reuse across many
  signups raises the Radar score → phone challenge → IP block.
- **WorkOS Radar phone step** likely rejects VOIP ranges — use real-mobile stock.
  Free option `--phone secondno` (2nd-no.com PL web numbers, sidecar-solved turnstile,
  REUSE-FIRST) is wired in, but treat it as best-effort against Radar's VOIP filter.
- **PKCE verifier** must be persisted (bank does it) — `complete_pending.py` cannot
  exchange the code without it.
- Google OAuth signup is *not* a bypass: plus-aliases don't change the identity
  Google returns, so each River account needs a distinct real Gmail.

## Files

```
river_reg.py         main driver (signup -> verify -> radar -> exchange -> key)
complete_pending.py  resume accounts stuck at the phone/SMS step
secondno_register.py register 2nd-no.com accounts for --phone secondno
secondno/            vendored 2nd-no.com client (2no.pl JSON-RPC + turnstile solvers)
dashboard.py         web dashboard (stdlib, :8910, dark UI, cards/tables/actions)
captcha_keys.example.json  2captcha key config template (-> data/secondno/captcha_keys.json)
river_wave.sh        paced wave wrapper
mail_pool.example.txt
requirements.txt
```

## Disclaimer

Research/interop tooling for a service you are authorized to test. WorkOS Radar
exists for a reason — pace your runs, don't hammer shared IPs.
