# River.ai autoreg — полный гайд (RU)

## Что это

HTTP-only авторег аккаунтов **River.ai** (`console.river.ai`). Без браузера в горячем
пути: `curl_cffi` с имперсонацией chrome131 реплеит весь флоу WorkOS AuthKit
(Next.js server actions), читает 6-значный код из почты по IMAP, обменивает PKCE-код
на access-токен и создаёт API-ключ `rv_*` через console API.

Финальный артефакт — строка в `data/river_bank.jsonl`:
email / пароль / imap-пароль / PKCE verifier / токены / API-ключ (или stage+err при фейле).

## Как работает флоу

```
1. auth.console.river.ai/user_management/authorize   — PKCE, screen_hint=sign-up
2. login.console.river.ai/sign-up                    — server action 408cf059… (email)
3. login.console.river.ai/sign-up/password           — server action 409a26bd… (пароль, юзер создан)
4. login.console.river.ai/email-verification         — код 6 цифр из IMAP (TTL 15 мин),
                                                       server action 400f86f0…
5. [WorkOS Radar] /radar-challenge/send              — ГЕЙТ: телефонный SMS-челлендж,
                                                       server action 4037b59e…
6. redirect ?code=…                                  — обмен кода + verifier на сессию
7. auth.console.river.ai/user_management/authenticate
8. console.river.ai/api/me → /api/billing/account → POST /api/keys  — ключ rv_*
```

Технические детали реплея:
- Server actions шлются как `multipart/form-data` с заголовками `next-action`,
  `accept: text/x-component`, `x-deployment-id` (берётся динамически — WorkOS
  передеплоивает и ротирует его).
- Имена полей обязаны нести flight-префикс `_1_` (`_1_email`, `_1_signals`, …)
  плюс `0=["$K1"]`. Любое лишнее поле → `invalid_params`.
- Если WorkOS сротировал action-id, `discover_action()` сам переизвлекает их
  из `page-*.js` чанка страницы при 404.

## Установка

```bash
pip install -r requirements.txt          # curl_cffi>=0.7
cp mail_pool.example.txt mail_pool.txt   # свой пул почт
```

Формат пула — по строке `email:imap_pass` на аккаунт. Дефолтный провайдер —
t-online.de, IMAP `secureimap.t-online.de:993`. Другой провайдер: поменять
`IMAP_HOST` в `river_reg.py` или передать `imap_host` в `read_code_imap`.
Альтернатива файлу — env `RIVER_MAIL_POOL`.

## Запуск

```bash
# одна рега, human-паузы, ожидание письма 13 минут
python river_reg.py 1 --human --imap-timeout 780

# волна из 5, начиная с индекса 100 в пуле
python river_reg.py 5 --start 100

# с обработкой телефонного челленджа
python river_reg.py 3 --phone manual      # код вручную в data/phone_code.json
FIVESIM_API_KEY=*** python river_reg.py 3 --phone fivesim

# бесплатные PL-номера через 2nd-no.com (вендорный пакет ./secondno)
python secondno_register.py 1             # playwright; акки → data/secondno/state.json
python river_reg.py 3 --phone secondno    # REUSE-FIRST резерв + OTP-поллинг

# дожать акки, застрявшие на phone-шаге
python complete_pending.py --phone fivesim

# пейсинг-волна обёрткой
./river_wave.sh
```

Поддерживаются прокси и ротация exit-IP (ZTE-модем): см. флаги в `river_reg.py --help`
и секцию proxy в драйвере.

## ГЛАВНЫЙ БЛОКЕР — WorkOS Radar

После ~7 signup-ов с одного IP Radar эскалирует до **глобального IP-лока**
«Access blocked, please contact support» — даже в реальном браузере.
**Каждая новая попытка продлевает лок (~3–4 часа).**

Драйвер детектит это по rendered-странице, пишет маркер `data/radar_block`
и прерывает волну. Правила:
- пейсинг ≤1 signup / 30–60 мин на IP;
- или ротация чистых residential-IP;
- после лока — стоять, не долбить.

Radar-сигнатуры: `_1_signals` — base64 JSON-фингерпринт; таймстемпыобновляются на каждый
вызов, хеши статичны (одна реальная машина). Переиспользование сигналов на многих
регах поднимает Radar-score → phone-челлендж → IP-блок.

## Питфолы (оплачены кровью)

1. **t-online alias-ловушка:** `base-0001`, `base-001`, `base-01`, `base-1` — ОДИН
   ящик. IMAP обязан фильтровать по точному заголовку `To:` + INTERNALDATE.
   Письма лагают до 7+ минут (`--imap-timeout 780`), код живёт 15 минут.
2. **curl_cffi multipart:** работает только `CurlMime().addpart(name=…, data=bytes)`;
   `files=dict` / `multipart=dict` → `'dict' object has no attribute '_form'`.
3. **Phone-шаг:** WorkOS Radar вероятно режет VOIP-диапазоны — нужен real-mobile сток
   (5sim и т.п. с депозитом). Бесплатный вариант `--phone secondno` (PL веб-номера
   2nd-no.com, turnstile решает sidecar :8877 бесплатно, REUSE-FIRST экономит капчу,
   3 номера/акк/3 дня) — вшит, но против VOIP-фильтра Radar это best-effort.
   Акки 2nd-no регать заранее: `python secondno_register.py N` (playwright).
4. **PKCE verifier** обязан персиститься (банк это делает) — без него
   `complete_pending.py` не обменяет код.
5. **Google OAuth — не обход:** плюс-алиасы не меняют identity, который возвращает
   Google; на каждый River-акк нужен отдельный реальный Gmail.
6. **Access-blocked детектор** должен смотреть только rendered-текст страницы:
   i18n-словари AuthKit содержат строку блока во всех языках → ложные срабатывания.

## Дашборд

`python dashboard.py [port]` (по умолчанию 8910) — однофайловый stdlib-сервер, тёмный UI.
Открывается на `http://127.0.0.1:8910`.

- **Карточки**: статистика банка (OK/pending/failed), countdown Radar-лока, размер пула
  2nd-no, сожжённые номера, mail pool, статус капчи (sidecar/2captcha/ready).
- **Действия**: рега 2nd-no акков, запуск river-волны (`--phone secondno`),
  complete pending, reserve-тест (живой номер 2nd-no), captcha-тест (живой solve turnstile).
- **Таблицы**: аккаунты river-банка (email/stage/статус/key/ошибка), пул 2nd-no.
- **Логи**: хвост reg2no.log / reserve_test.log / wave.log.
- Авто-обновление каждые 15с (переключается). Секреты маскируются на сервере.

## Капча (turnstile для 2nd-no резервов)

Вендорный `secondno/captcha.py` даёт `CaptchaRouter` — пробует бэкенды по порядку с ретраями:

1. **SidecarSolver** — локальный captcha-solver sidecar на `:8877` (БЕСПЛАТНО, ~14с/токен).
   Health: `GET http://127.0.0.1:8877/health`. Поднимается отдельно. Приоритетный бэкенд.
2. **TwoCaptchaTurnstile** — 2captcha.com фолбэк. Источник ключа (первый матч):
   - env `TWOCAPTCHA_API_KEY`
   - env `RUCAPTCHA_KEY`
   - `data/secondno/captcha_keys.json` (шаблон: `captcha_keys.example.json`)

   ВАЖНО: submit ТОЛЬКО через `in.php` (res.php → ERROR_EMPTY_METHOD);
   баланс: POST `res.php {action:getbalance}`, значение в поле `request`.

`get_solver()` возвращает роутер (sidecar → 2captcha фолбэк). Дашборд показывает
живой статус капчи (sidecar alive, баланс 2captcha) и имеет кнопку **🧩 Captcha-тест**,
которая решает реальный turnstile 2nd-no.com end-to-end.

## Файлы

```
river_reg.py         главный драйвер: signup → verify → radar → exchange → key
complete_pending.py  дожатие акков, застрявших на phone/SMS-шаге
secondno_register.py рега акков 2nd-no.com для --phone secondno
secondno/            вендорный клиент 2nd-no.com (2no.pl JSON-RPC + turnstile-солверы)
dashboard.py         веб-дашборд (stdlib, :8910, тёмный UI, карточки/таблицы/действия)
captcha_keys.example.json  шаблон конфига 2captcha-ключей (→ data/secondno/captcha_keys.json)
river_wave.sh        пейсинг-обёртка волны
mail_pool.example.txt
requirements.txt
```

## Статус (2026-10-20)

| Шаг | Статус |
|-----|--------|
| PKCE authorize | ✅ |
| Email-шаг | ✅ |
| Password-шаг (юзер создаётся) | ✅ |
| Email-верификация + IMAP | ✅ |
| Radar phone-челлендж | ⚠️ гейт, нужен SMS (`--phone`) |
| Token exchange + API-ключ | реализовано, в бою ещё не достигнуто (Radar) |

## Дисклеймер

Research/interop-тулинг для сервиса, который вы авторизованы тестировать.
WorkOS Radar существует не зря — пейсите прогоны, не долбите общие IP.
