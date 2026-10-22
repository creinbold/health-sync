# health-sync

Connect your own bot (for example a Grok Bot) to **Google Health** (Google Health API v4,
`health.googleapis.com`, the successor of the Fitbit Web API).

- **Read:** a daily job pulls steps, calories, sedentary time, resting heart rate, HRV, sleep,
  workouts, SpO2, skin temperature and breathing rate, and writes one JSON file per local day.
  Your bot reads those files to evaluate the user's data.
- **Write:** your bot can log meals, drinks and symptoms back to Google Health, with a dry-run
  by default and an explicit `--confirm` step.

It is meant for people who want a personal health assistant on top of their own Fitbit / Google
Health data, running on their own machine with their own Google Cloud OAuth client. There is no
hosted service: tokens and data stay on the machine that runs it.

Contents: [Prerequisites](#prerequisites) · [Quickstart](#quickstart) ·
[Configuration](#configuration) · [Connecting to a Grok Bot](#connecting-to-a-grok-bot) ·
[Writing back](#writing-back-meals-drinks-symptoms) · [Security notes](#security-notes) ·
[API behaviour notes](#api-behaviour-notes) · [Development](#development)

## Prerequisites

1. **A Google account whose data is in Google Health.** Open the Google Health app and sign in
   with that account (or migrate your Fitbit account to it) first. Otherwise `hsync auth` stops
   with "Google Health account NOT linked".
2. **A Google Cloud project** (<https://console.cloud.google.com/>). You don't need billing.
3. **Enable the Google Health API** in that project (APIs & Services → Library → "Google Health API").
4. **OAuth consent screen** (Google Auth Platform → Branding / Audience):
   - User type **External**.
   - **Publish the app ("In production")**. You don't need verification for personal use. In
     "Testing" mode, Google lets refresh tokens expire after 7 days, so the daily sync would
     need a new `./hsync auth` every week. (`./hsync status` warns when the token has an expiry.)
   - Because the app is unverified, Google shows a **"Google hasn't verified this app"** warning
     during consent. Click *Advanced → Go to … (unsafe)* to continue. This is expected for
     your own client.
   - Unverified apps are capped at **100 users**. That's fine for yourself and a few people.
     Each user should normally create their own project and client anyway.
5. **An OAuth client of type "Desktop app"** (Google Auth Platform → Clients → Create client).
   Download its JSON (`client_secret_<...>.apps.googleusercontent.com.json`). A "Web" client also
   works only if the loopback redirect URI is registered on it. Desktop is recommended.
6. **Python 3.13+** and **a virtual environment at `.venv/` in the repo.** The `./hsync` wrapper
   runs `.venv/bin/python` directly.

## Quickstart

```bash
git clone https://github.com/creinbold/health-sync.git
cd health-sync
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Optional: persistent local settings (gitignored), see "Configuration" below
cp hsync.local.env.example hsync.local.env && chmod 600 hsync.local.env
# e.g. set HSYNC_TZ (default: system timezone, else UTC) and HSYNC_OUT_DIR (default: ./daily)
```

Without any settings everything lives in the repo dir: secrets in `secrets/`, daily files in `daily/`
(both gitignored), and days follow the system timezone.

**1. Authorize (one time).** Pass the downloaded client JSON. It is copied to `secrets/client_secret.json`
(mode 600). If you omit `--client-secret`, `hsync` uses `secrets/client_secret.json` or looks for
`~/Downloads/client_secret_*.json`.

```bash
./hsync auth --client-secret ~/Downloads/client_secret_XXXX.apps.googleusercontent.com.json
```

This prints the consent URL (it's also saved to `auth_url.txt` in the app dir, mode 600) and listens on
`http://127.0.0.1:8765` (`--port`, `0` = random; `--timeout`, default 900 s). Open the URL in a browser
**on the same machine**, sign in, and approve all scopes. Tokens are stored in `secrets/token.json` (mode 600).

**2. If the redirect isn't captured** (for example, the browser runs on a different machine and lands on
an error page at `127.0.0.1`), copy that page's full URL and finish the flow:

```bash
./hsync auth-complete --redirect-url 'http://127.0.0.1:8765/?state=...&code=...'
```

A pending authorization expires after 12 h.

**3. Check:**

```bash
./hsync status            # client JSON, token, granted scopes, live token/identity check, last sync
./hsync status --offline  # same, without the live check
```

**4. Sync:**

```bash
./hsync sync              # today + the 2 previous days (default --days 3), files overwritten
./hsync sync --days 7     # today + the 6 previous days (1..3660)
```

**5. Backfill history (optional):**

```bash
./hsync backfill --from 2026-01-01                  # --to defaults to today
./hsync backfill --from 2026-01-01 --to 2026-06-30  # max 3660 days per run
```

`sync` and `backfill` also accept `--no-raw`, which skips keeping the raw API responses.
Global options go **before** the subcommand: `./hsync [-v] [--tz TZ] [--out-dir DIR] <command> ...`.

### Scopes requested

All scopes are requested at consent time (`./hsync auth`):

| Scope (`https://www.googleapis.com/auth/googlehealth.` + …) | Used for |
|---|---|
| `activity_and_fitness.readonly` | steps, calories, sedentary time, workouts (daily sync) |
| `health_metrics_and_measurements.readonly` | resting HR, HRV, SpO2, skin temperature, breathing rate (daily sync) |
| `sleep.readonly` | sleep (daily sync) |
| `profile.readonly` | identity / account-linked check (daily sync) |
| `nutrition.writeonly` | `log-meal`, `log-drink`, deleting those entries |
| `logged_symptoms.writeonly` | `log-symptom`, deleting symptoms |
| `logged_symptoms.readonly` | requested together with the symptom write scope |

The daily sync needs only the four read scopes. `./hsync status` shows separately whether the
log (write) scopes were granted. If the user unchecks some scopes on the consent screen, the matching
data groups fail (fields stay `null`, sync exits 3) or the `log-*` commands exit 2. To fix that, run `./hsync auth` again. A new
token replaces `secrets/token.json` only if the code exchange succeeds and it still contains every
read scope the current token has. Otherwise the new token is parked in `secrets/token.rejected.json`
and the old one stays.

## Configuration

All settings are `HSYNC_*` variables. They are taken from, in this order:

1. the **environment** (for example `HSYNC_TZ=Europe/London ./run_sync.sh`),
2. the **local config file** `hsync.local.env` next to the code (see below),
3. the **defaults** below, exactly as `health_sync/config.py`, `health_sync/logs.py` and `run_sync.sh`
   define them.

| Variable | Default | Meaning |
|---|---|---|
| `HSYNC_APP_DIR` | the repo dir (where `health_sync/` lives) | Base dir for secrets, state, raw responses, audit log, lock, `auth_url.txt`, `food_units.json` |
| `HSYNC_SECRETS_DIR` | `$HSYNC_APP_DIR/secrets` | `client_secret.json`, `token.json`, `pending_auth.json` |
| `HSYNC_STATE_FILE` | `$HSYNC_APP_DIR/state.json` | last sync / last error |
| `HSYNC_RAW_DIR` | `$HSYNC_APP_DIR/raw` | raw API responses per run |
| `HSYNC_RAW_KEEP_DAYS` | `14` | prune raw run dirs older than this (min 1) |
| `HSYNC_OUT_DIR` | `$HSYNC_APP_DIR/daily` | output dir for `YYYY-MM-DD.json` |
| `HSYNC_TZ` | system timezone (`$TZ`, `/etc/localtime`, `/etc/timezone`), else `UTC` | IANA timezone that defines a "day", and the timezone of times without an offset in `log-*` input |
| `HSYNC_API_BASE` | `https://health.googleapis.com/v4` | API base URL (tests only; leave unset) |
| `HSYNC_AUDIT_DIR` | `$HSYNC_APP_DIR/audit` | write/delete audit log + idempotency ledger |
| `HSYNC_FOOD_UNITS` | `$HSYNC_APP_DIR/food_units.json` | cached food measurement units |
| `HSYNC_SUMMARY_CSV` | `daily_summary.csv` in the parent of `HSYNC_OUT_DIR` | CSV for the optional updater (below) |
| `HSYNC_DAYS` | `3` | `run_sync.sh` only: days per daily run |
| `HSYNC_UPDATE_CSV` | `0` | `run_sync.sh` only: `1` also runs `update_daily_summary.py` |
| `HSYNC_RUN_TIMEOUT` | `1800` | `run_sync.sh` only: hard timeout for one sync (seconds) |
| `HSYNC_LOCAL_ENV` | `hsync.local.env` next to the code | path of the local config file; empty = don't load one |

The lock file is always `$HSYNC_APP_DIR/.sync.lock`, and `run_sync.sh` always logs to `sync.log`
next to itself. Per call, `./hsync` also takes `--tz` and `--out-dir` (global options, before the
subcommand). `log-*` interprets times without an offset in `HSYNC_TZ`, though, not `--tz`. `status`
counts files in `HSYNC_OUT_DIR`.

### Local config file

To make settings stick for every entry point (`./hsync`, `./run_sync.sh`, `update_daily_summary.py`,
`python -m health_sync`), put them in `hsync.local.env` in the repo dir. Start from
`hsync.local.env.example`. The file is gitignored and should be mode 600:

```bash
cp hsync.local.env.example hsync.local.env && chmod 600 hsync.local.env
```

```ini
HSYNC_TZ=America/New_York
HSYNC_OUT_DIR=/home/me/health/daily
HSYNC_UPDATE_CSV=1
```

The format is one `HSYNC_NAME=value` per line, with `#` comments, an optional `export ` prefix and
optional matching quotes. Only `HSYNC_*` keys are accepted, and nothing is expanded or executed
(`$(...)` stays literal text). Variables already set in the environment win. A group- or
world-writable file is ignored with a warning.

### Optional CSV updater

With `HSYNC_UPDATE_CSV=1`, `run_sync.sh` also runs `update_daily_summary.py`, which keeps a
one-row-per-day CSV (`HSYNC_SUMMARY_CSV`) filled from the complete days in `HSYNC_OUT_DIR`. Options:
`--csv`, `--daily-dir`, `--dry-run`.

- If the CSV doesn't exist, it is created with `date` plus the columns `steps_best, kcal_total,
  kcal_active, sedentary_period_min, rhr, hrv_ms, sleep_asleep_min, sleep_in_bed_min,
  sleep_total_asleep_min, bedtime, waketime`.
- If the CSV already exists (for example one you maintain with another tool), it keeps its columns.
  Only these columns are filled, and only in empty cells or cells the updater filled itself before
  (tracked in `daily_summary.api_cells.json`).
- Missing dates are appended, and one rolling backup `daily_summary.csv.bak-api` is kept.
- A failure only logs `WARN: daily_summary.csv update failed`. It never changes the sync's exit code.

## Connecting to a Grok Bot

### 1. Give your bot the daily files

Point your bot at `HSYNC_OUT_DIR` (default `daily/` in the repo dir). There is one file per local day,
`YYYY-MM-DD.json`, and each is fully rebuilt on every run that covers that day. Schema (`schema_version` 1):

```jsonc
{
  "date": "2026-10-20",
  "steps": 6000,                    // int, all sources reconciled
  "calories_total": 2200.5,         // kcal
  "calories_active": 600.4,         // kcal (active energy burned)
  "sedentary_minutes": 500,
  "resting_heart_rate": 60.0,       // bpm
  "hrv": {"rmssd_ms": 30.0, "deep_sleep_rmssd_ms": 35.5, "entropy": 2.5, "non_rem_heart_rate_bpm": 55},
  "sleep": {                        // main sleep of the night that ENDS on this date (wake day)
    "duration_min": 420, "time_in_bed_min": 460,
    "bedtime": "2026-10-19T23:10-04:00", "wake_time": "2026-10-20T06:50-04:00",
    "stages": {"deep": 60, "light": 250, "rem": 90, "awake": 40},   // null without stage data
    "sleep_type": "...", "minutes_to_fall_asleep": 10,
    "main_sleep": true, "nap": false, "manually_edited": false, "processed": true,
    "sessions": 1, "naps": 0, "total_asleep_min_all_sessions": 420
  },
  "workouts": [                     // [] when none were recorded
    {"type": "...", "name": "Walking", "start": "...", "end": "...", "duration_min": 30.0,
     "elapsed_min": 32.0, "calories": 150.0, "avg_hr": 100, "steps": 3000, "distance_km": 2.5,
     "active_zone_minutes": 10, "auto_detected": true}
  ],
  "spo2": {"avg_pct": 96.0, "lower_bound_pct": 94.0, "upper_bound_pct": 98.0, "std_dev_pct": 1.0},
  "skin_temperature": {"deviation_c": -0.2, "nightly_c": 33.8, "baseline_c": 34.0, "relative_stddev_30d_c": 0.3},
  "breathing_rate": 14.0,           // breaths per minute
  "meta": {
    "source": "Google Health API v4 (health.googleapis.com)", "user": "users/me",
    "sync_time": "...", "timezone": "America/New_York",
    "day_complete": true,           // false for today: values are still growing
    "endpoints": {"steps": "dailyRollUp steps", "...": "..."},
    "errors": null,                 // {group: message} if a data group failed in this run
    "stale_groups": null,           // groups whose previous values were kept because of an error
    "schema_version": 1, "generator": "health-sync"
  }
}
```

(The values above are illustrative.) Any field may be `null` when the device recorded nothing or a
scope is missing. The bot should ignore `day_complete: false` days for daily totals and treat groups
in `meta.stale_groups` as possibly outdated.

### 2. Schedule the daily sync with a Grok Bot routine

`run_sync.sh` does not schedule itself. Create a daily routine in your Grok Bot (or use cron or a
systemd timer) that runs, for example:

```bash
cd /path/to/health-sync && ./run_sync.sh; echo "exit=$?"
```

`run_sync.sh` runs `hsync sync --days 3` (re-fetching the last 3 days so late-synced data fills in),
then the optional CSV updater. Its settings come from `hsync.local.env` (or the routine's environment). All output goes to `sync.log` (rotated at ~5 MB), so have the routine
read the tail of `sync.log` when the exit code isn't 0. A lock prevents overlapping runs. Exit codes
(the same for `./hsync sync|backfill`):

| Code | Meaning | What your bot should do |
|---|---|---|
| 0 | OK | read the new files |
| 1 | error (network/API down, timeout, invalid input or usage) | retry later; nothing was broken |
| 2 | re-auth needed or Google Health account not linked | ask the user to run `./hsync auth` |
| 3 | partial: some data groups failed; their previous values were kept and are listed in `meta.stale_groups` | use the data and retry later |
| 4 | another run holds the lock | skip this run |

## Writing back: meals, drinks, symptoms

**Rule for your bot: show the user every entry (the dry-run output) and run `--confirm` only after the
user has approved exactly those entries.** Without `--confirm` nothing is sent. The dry-run prints the
exact HTTP requests that would be made.

```bash
./hsync log-meal    --file meal.json            # dry-run (default)
./hsync log-meal    --file meal.json --confirm  # actually write
./hsync log-drink   < drink.json                # JSON on stdin also works (--file - or -f -)
./hsync log-symptom --file symptom.json --confirm
./hsync list-logs   --date 2026-10-21 [--type all|meal|drink|symptom] [--json]
./hsync delete-log  --id users/me/dataTypes/nutrition-log/dataPoints/<n> [--id ...]   # dry-run
./hsync delete-log  --id <ids as printed by log-*/list-logs> --confirm
```

Input is one JSON object or an array of up to 100. If one entry is invalid, the whole batch is
rejected and nothing is sent. Unknown fields are rejected. Times are ISO 8601. A time without an
offset is local time in `HSYNC_TZ`. The tool rejects future times, entries older than 400 days, and
naive times that are ambiguous or don't exist at a DST change (for example `2026-10-25T02:30` in
Europe/Paris; add `+02:00` or `+01:00`). Exit codes: 0 ok / dry-run / duplicate, 1 invalid input or
API error, 2 auth problem or missing scope.

**Meal** (`nutrition-log`):

```json
{"time": "2026-10-21T12:30", "meal_type": "lunch", "food_name": "Oatmeal with berries",
 "portion": 250, "unit": "g", "kcal": 320, "protein_g": 10, "carbs_g": 55, "fat_g": 6,
 "fiber_g": 7, "sugar_g": 12, "saturated_fat_g": 1, "sodium_mg": 40, "caffeine_mg": 0,
 "duration_min": 20, "client_id": "diary-2026-10-21-lunch-1", "note": "local only"}
```

- Required: `time`, `meal_type` (breakfast, lunch, dinner, snack, before_breakfast, before_lunch,
  before_dinner, after_dinner, anytime), `food_name`, `portion`, `unit` (g, kg, ml, l, piece, serving,
  cup, tbsp, tsp, slice, oz). Optional: `kcal` (≤ 5000), the macros above, `duration_min` or
  `end_time`, `client_id`, `note` (local audit log only).
- The tool sends an anonymous food: foodDisplayName, mealType, energy.kcal, totalCarbohydrate.grams,
  totalFat.grams, and nutrients[] (PROTEIN, DIETARY_FIBER, SUGAR, SATURATED_FAT, SODIUM, CAFFEINE; in grams).
- Plausibility checks: per-unit portion limits (for example ≤ 3000 g), macros may not exceed the kcal
  (4/4/9 rule + 30 %), sugar ≤ carbs, saturated fat ≤ fat, and macros may not weigh more than a gram portion.

**Drink** (`hydration-log`, sent as `amountConsumed` in ml):

```json
{"time": "2026-10-21T15:00", "amount": 0.5, "unit": "l", "drink": "water", "client_id": "diary-2026-10-21-drink-1"}
```

Units: ml, l, cup (US), cup_imperial, fl_oz, fl_oz_imperial, pint, pint_imperial. The maximum is 3000 ml per
entry. `drink` and `note` stay local (audit log only), because the API's hydration log has no name field.

**Symptom** (`symptoms`, a sample time plus Google's fixed symptom list):

```json
{"time": "2026-10-21T21:00", "symptoms": ["HEADACHE", "FATIGUE"], "note": "short night",
 "client_id": "diary-2026-10-21-symptom-1"}
```

Values: ABDOMINAL_PAIN, ACNE, ANXIETY, BACK_PAIN, BLADDER_LEAKS, BLEEDING_GUMS, BLOATED, BLURRED_VISION,
BRAIN_FOG, BURNING_MOUTH, CHEST_PAIN, CONFUSION, CONSTIPATION, COUGH, CRAMPS, CRAVINGS,
DECREASED_APPETITE, DIARRHEA, DIFFICULTY_BREATHING, DIZZINESS, DRAWING_PAIN, DRY_EYES, DRY_HAIR,
DRY_SKIN, EXHAUSTION, FAINTING, FATIGUE, FEEL_GOOD, FEVER, FOOD_AVERSIONS, FREQUENT_URINATION,
HAIR_LOSS, HEADACHE, HEART_PALPITATIONS, HEARTBURN, HOT_FLASHES, HUNGER, HYPERPIGMENTATION,
INCREASED_APPETITE, INSOMNIA, JOINT_PAIN, LEG_CRAMPS, NAUSEA, NIGHT_SWEATS, NORMAL_DIGESTION,
NORMAL_STOOL, OTHER, SHAKINESS, SICK, SLEEPINESS, SWEATING, SWELLING, THIRST, VOMITING, plus a few
reproductive-health values (full list in `health_sync/logs.py`). A few plain-English aliases are also
accepted (for example "tired" → FATIGUE, "bloating" → BLOATED, "dry skin" → DRY_SKIN; "eczema",
"rash" and "itching" → OTHER).

### Idempotency: always pass a stable `client_id`

Every entry gets a dedupe key: the sha256 of `client_id` if you give one, otherwise of the entry
content. The API ignores client-chosen data point ids and assigns numeric ones, so the key and the
returned resource name are kept in a local ledger (`audit/writes.jsonl`). Before the tool creates
an entry, it checks two things:

1. Is the key already in the ledger? For meals and drinks, it verifies this with a GET and
   re-creates the entry if it was deleted in the app meanwhile.
2. Does Google Health already have the same content at the same start minute?

A repeat prints `duplicate` and writes nothing. For symptoms, the ledger is the only duplicate
guard, so your bot should use the diary entry's own id as `client_id`.

### list-logs and delete-log

- `create` returns the resource name `users/<healthUserId>/dataTypes/<type>/dataPoints/<n>`, and
  `log-*` prints it. Pass exactly that name to `delete-log --id` (`users/me/...` works too). `--id`
  is repeatable.
- `list-logs --date` shows meals and drinks from Google Health (entries written by this tool are
  marked `[health-sync]`) and symptoms from the local audit log.
- Meal and drink deletions are verified. Symptom deletions can't be checked, because the API answers
  `batchDelete` with `{"done": true}` even for unknown names.

### Known Google Health API limits (observed October 2026)

- **The portion goes into the food name.** For example, the API receives "Oatmeal with berries (250 g)".
  Real serving units would need the food-measurement-unit list, which requires the
  `googlehealth.nutrition.readonly` scope. This tool doesn't request that scope, so
  `./hsync food-units --refresh` exits 2. A free-text unit is silently dropped by the API.
- **The symptom list has no eczema or itch value, and no severity or notes field.** For skin
  complaints, use DRY_SKIN and/or OTHER. The `note` stays in the local audit log only.
- **Symptoms can't be read back.** The API can create and batch-delete symptoms, but it can't list
  or get them.
- **Meals can't be edited.** Anonymous nutrition logs are immutable. To correct a meal, `delete-log`
  it and log it again with a new `client_id`.

## Security notes

- **Secrets stay local.** The client JSON and tokens live only in `secrets/` (dir 700, files 600). They
  are written atomically (mkstemp with mode 0600 from creation, fsync, rename). If the permissions on
  the token or client JSON have been loosened, they are reset to 0600 when loaded. `secrets/`, tokens,
  `state.json`, `raw/`, `audit/`, `daily/`, CSVs, `hsync.local.env`, logs and all non-doc JSON are in `.gitignore`. Never commit them,
  and never commit your daily files: they are health data.
- **Tokens are redacted from logs.** Every log line, printed error and `state.json` `last_error` passes
  through credential redaction (Bearer/Authorization, access and refresh tokens, client secret, auth
  codes, token-like query parameters). urllib3 debug output is suppressed even with `-v`. The
  `audit/writes.jsonl` log contains no tokens or headers.
- **The token endpoint is pinned.** `token_uri` is always Google's `https://oauth2.googleapis.com/token`,
  whatever the client JSON says.
- OAuth uses PKCE S256, and `state` is compared in constant time (callbacks with the wrong state are
  ignored and the flow keeps waiting). A pending auth expires after 12 h. The loopback listener refuses
  non-loopback binds and has a 10 s socket timeout.
- HTTP: TLS verification is always on, with (10 s connect, 60 s read) timeouts. Retries use capped
  backoff (honouring Retry-After up to 60 s) on 429, 5xx and network errors. Pagination has a loop guard.
- If the token endpoint is down, the run exits 1 (retry later; the old token is untouched). If the
  refresh token is rejected, it exits 2. Usage errors exit 1, never 2. The lock uses flock(2), so a
  crashed run never leaves a stale lock.
- `run_sync.sh` uses `set -euo pipefail` and `umask 077`, validates its env input, and has a hard timeout.
- Raw API responses (`raw/<timestamp>/`, 700/600) contain health data. They are pruned after
  `HSYNC_RAW_KEEP_DAYS`. Use `--no-raw` if you don't want them.
- Non-finite numbers become `null`. Corrupt day files are rebuilt instead of carried over.

## API behaviour notes

Observed in October 2026. `docs/discovery_v4_rev20261020.json` is the API discovery document the
field names were taken from.

- Rollup windows come back as [day 00:00, next day 00:00), so the end is exclusive.
- Sleep uses reference names (type/stages/summary/metadata). Intervals carry no civil times, so the
  wake date is derived from endTime + endUtcOffset. False booleans are omitted (nap/mainSleep).
- API sleep returns algorithm v2 sessions only. A Google Takeout export also contains v1 duplicates, so
  a "longest session per day" taken from Takeout can differ from the API by up to ~50 min.
- Daily data types came back without duplicate points per day. If several sources exist, the Fitbit
  platform source is preferred.
- `steps` is the reconciled all-sources rollup (a per-minute merge of tracker and phone). It can exceed
  any single Takeout source on days with little wear time.
- Takeout changed its active-calorie definition in autumn 2026 (since then it's roughly 2x the API's
  active-energy-burned), so don't mix the two series.
- The rollup range is limited (14 days for total-calories), so requests are chunked into 14-day windows.

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/ruff check . && .venv/bin/bandit -q -r health_sync update_daily_summary.py && .venv/bin/pip-audit -r requirements.txt
```

The tests use synthetic fixtures only and redirect every path to a temp dir.

## License

MIT, see [LICENSE](LICENSE).
