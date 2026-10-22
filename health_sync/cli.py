"""hsync CLI: auth | auth-complete | sync | backfill | status"""
import argparse
import logging
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import json

from . import auth, config, logs, sync
from .api import AccountNotLinked, ApiError, HealthApi
from .util import RedactingFilter, read_json, redact, write_text_atomic

log = logging.getLogger("hsync")
EXIT_OK, EXIT_ERR, EXIT_AUTH, EXIT_PARTIAL, EXIT_LOCKED = 0, 1, 2, 3, 4


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # usage errors are exit 1, never 2 (2 means "re-auth needed")
        self.print_usage(sys.stderr)
        self.exit(EXIT_ERR, f"{self.prog}: error: {message}\n")


def _mask(v):
    v = str(v or "")
    return "n/a" if not v else ("*" * max(0, len(v) - 4)) + v[-4:]


def _iso_date(s):
    """Strict YYYY-MM-DD (date.fromisoformat alone also accepts e.g. 20261019 / 2026-W43-1)."""
    try:
        if len(s) != 10:
            raise ValueError
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {s!r}") from None


def _fmt_ts(ts, tz):
    return datetime.fromtimestamp(ts, ZoneInfo(tz)).isoformat(timespec="seconds") if ts else None


def cmd_auth(a):
    src = auth.find_client_secret(a.client_secret)
    if not src or not src.exists():
        print("No client JSON found. Pass --client-secret /path/to/client_secret_*.json "
              f"(looked in {config.CLIENT_SECRET_FILE} and {config.DOWNLOADS_GLOB})", file=sys.stderr)
        return EXIT_ERR
    try:
        dst = auth.install_client_secret(src)
        client = auth.load_client(dst)
    except auth.AuthError as e:
        print(f"AUTH FAILED: {e}", file=sys.stderr)
        return EXIT_ERR
    print(f"client JSON installed at {dst} (type: {client['kind']}, mode 600)")
    if client["kind"] != "installed":
        print("WARNING: this is a 'web' client; the loopback redirect URI must be registered on it. "
              "A 'Desktop app' client is recommended.", file=sys.stderr)
    httpd, url, pending = auth.start_flow(client, port=a.port)
    url_file = config.APP_DIR / "auth_url.txt"
    write_text_atomic(url_file, url + "\n", mode=0o600)  # created 0600, no readable window
    print("\nOpen this URL in a browser on this machine and sign in with the Google account linked to Google Health:\n")
    print(url)
    print(f"\n(also saved to {url_file}; listening on {pending['redirect_uri']} for {a.timeout}s)", flush=True)
    params = auth.wait_for_callback(httpd, a.timeout)
    if not params:
        print("Timed out waiting for the redirect. If the browser landed on an error page at 127.0.0.1, "
              "copy its full URL and run: hsync auth-complete --redirect-url '<url>'", file=sys.stderr)
        return EXIT_AUTH
    return _complete(client, params, pending)


def cmd_auth_complete(a):
    pending = read_json(config.PENDING_AUTH_FILE)
    if not isinstance(pending, dict) or not pending.get("state") or not pending.get("code_verifier"):
        print("No pending authorization; run: hsync auth", file=sys.stderr)
        return EXIT_AUTH
    try:
        client = auth.load_client(config.CLIENT_SECRET_FILE)
    except auth.AuthError as e:
        print(f"AUTH FAILED: {e}", file=sys.stderr)
        return EXIT_AUTH
    return _complete(client, auth.parse_redirect_url(a.redirect_url), pending)


def _complete(client, params, pending):
    try:
        tok = auth.finish(client, params, pending)
    except (auth.AuthError, auth.TokenRefreshError) as e:
        print(f"AUTH FAILED: {e}", file=sys.stderr)
        return EXIT_AUTH
    granted = set(tok.get("scopes") or [])
    missing = [s for s in config.AUTH_SCOPES if s not in granted]
    print(f"tokens stored in {config.TOKEN_FILE} (mode 600); refresh token: present")
    print("granted scopes:", ", ".join(sorted(s.rsplit('.', 2)[-2] for s in granted if 'googlehealth' in s)) or "none")
    if missing:
        print("WARNING: not granted (those fields will stay null):", ", ".join(missing), file=sys.stderr)
    try:
        ident = HealthApi(auth.Credentials(client=client)).identity()
        print(f"Google Health account linked: yes (healthUserId {_mask(ident.get('healthUserId'))}, "
              f"legacy Fitbit id {_mask(ident.get('legacyUserId'))})")
    except AccountNotLinked:
        print("Google Health account NOT linked: open the Google Health app, sign in with this Google "
              "account / migrate the Fitbit account, then run: hsync status", file=sys.stderr)
        return EXIT_AUTH
    except (ApiError, auth.TokenRefreshError) as e:
        print(f"identity check failed: {redact(e)}", file=sys.stderr)
        return EXIT_ERR
    return EXIT_OK


def _sync_range(start, end_excl, a):
    tz = a.tz
    rec = sync.RawRecorder() if not a.no_raw else None
    try:
        with sync.Lock():
            api = HealthApi(auth.Credentials(), recorder=rec)
            s = sync.run(api, start, end_excl, out_dir=a.out_dir, tz=tz, recorder=rec)
    except sync.LockBusy as e:
        print(str(e), file=sys.stderr)
        return EXIT_LOCKED
    except (auth.AuthError, AccountNotLinked) as e:
        msg = redact(e)
        sync.update_state(last_error=f"auth: {msg}"[:800])
        print(f"AUTH REQUIRED: {msg}", file=sys.stderr)
        return EXIT_AUTH
    except Exception as e:  # anything else: clean exit 1 with a redacted one-line reason, no traceback
        msg = redact(f"{type(e).__name__}: {e}")
        log.debug("sync failed", exc_info=True)
        try:
            sync.update_state(last_error=msg[:800])
        except Exception:  # state.json unwritable: the printed reason below still reaches sync.log
            log.debug("could not record last_error in state.json")
        print(f"SYNC FAILED: {msg}", file=sys.stderr)
        return EXIT_ERR
    print(f"synced {s['range'][0]}..{s['range'][1]}: {s['files']} files -> {a.out_dir}"
          + (f"; failed groups: {', '.join(s['errors'])}" if s["errors"] else ""))
    return EXIT_PARTIAL if s["errors"] else EXIT_OK


def cmd_sync(a):
    if not 1 <= a.days <= config.MAX_BACKFILL_DAYS:
        print(f"--days must be between 1 and {config.MAX_BACKFILL_DAYS}", file=sys.stderr)
        return EXIT_ERR
    today = sync.today_local(a.tz)
    return _sync_range(today - timedelta(days=a.days - 1), today + timedelta(days=1), a)


def cmd_backfill(a):
    start = a.from_
    end = a.to or sync.today_local(a.tz)
    if start > end:
        print("--from must be <= --to", file=sys.stderr)
        return EXIT_ERR
    if (end - start).days + 1 > config.MAX_BACKFILL_DAYS:
        print(f"range too long (max {config.MAX_BACKFILL_DAYS} days)", file=sys.stderr)
        return EXIT_ERR
    return _sync_range(start, end + timedelta(days=1), a)


def cmd_status(a):
    tz = a.tz
    print(f"client JSON : {'present' if config.CLIENT_SECRET_FILE.exists() else 'MISSING'} ({config.CLIENT_SECRET_FILE})")
    tok = auth.TokenStore().load()
    rc = EXIT_OK
    if not tok:
        print("token       : MISSING (run: hsync auth)")
        rc = EXIT_AUTH
    else:
        print(f"token       : refresh token {'present' if tok.get('refresh_token') else 'MISSING'}; "
              f"access token expires {_fmt_ts(tok.get('expires_at'), tz)}; "
              f"obtained {_fmt_ts(tok.get('refresh_token_obtained_at'), tz)}")
        if tok.get("refresh_token_expires_at"):
            print(f"              refresh token expires {_fmt_ts(tok['refresh_token_expires_at'], tz)} "
                  "(an expiry here means the app is still in 'Testing' mode)")
        missing = [s.rsplit('.', 2)[-2] for s in config.SCOPES if s not in (tok.get("scopes") or [])]
        print("scopes      : " + ("all granted" if not missing else "MISSING " + ", ".join(missing)))
        wmissing = [s.rsplit('.', 2)[-2] + "." + s.rsplit('.', 1)[-1] for s in config.WRITE_SCOPES
                    if s not in (tok.get("scopes") or [])]
        print("log scopes  : " + ("all granted (log-meal/drink/symptom enabled)" if not wmissing
                                  else "MISSING " + ", ".join(wmissing) + " (re-consent: hsync auth)"))
        if not a.offline:
            try:
                creds = auth.Credentials()
                creds.access_token(force_refresh=time.time() >= tok.get("expires_at", 0))
                HealthApi(creds).identity()
                print("live check  : OK (token valid, Google Health account linked)")
            except AccountNotLinked:
                print("live check  : token OK but Google Health account NOT linked")
                rc = EXIT_AUTH
            except auth.AuthError as e:
                print(f"live check  : AUTH FAILED - {redact(e)}")
                rc = EXIT_AUTH
            except Exception as e:
                print(f"live check  : ERROR - {redact(f'{type(e).__name__}: {e}')}")
                rc = EXIT_ERR
    st = read_json(config.STATE_FILE, {}) or {}
    if not isinstance(st, dict):
        st = {}
    print(f"last sync   : {st.get('last_sync') or 'never'} range={st.get('last_sync_range')} "
          f"errors={list((st.get('last_sync_errors') or {}).keys()) or 'none'}")
    print(f"last full OK: {st.get('last_full_success') or 'never'}; last attempt: {st.get('last_attempt') or 'never'}")
    if st.get("last_error"):
        print(f"last error  : {redact(st['last_error'])[:300]}")
    files = sorted(p.name for p in config.OUT_DIR.glob("????-??-??.json")) if config.OUT_DIR.exists() else []
    print(f"daily files : {len(files)} in {config.OUT_DIR}" + (f" ({files[0][:10]} .. {files[-1][:10]})" if files else ""))
    return rc


# ------------------------------------------------------------------ meals / drinks / symptoms
def _read_input(a):
    if a.file and a.file != "-":
        try:
            with open(a.file, encoding="utf-8") as f:
                return f.read(1_000_001)
        except OSError as e:
            raise logs.ValidationError(f"cannot read {a.file}: {e.strerror}") from None
    return sys.stdin.read(1_000_001)


def _need_scopes(scopes):
    tok = auth.TokenStore().load()
    if not tok:
        raise auth.AuthError("not authorized yet; run: hsync auth")
    have = tok.get("scopes")
    missing = [s for s in scopes if have is not None and s not in have]
    if missing:
        raise auth.AuthError("token lacks " + ", ".join(m.rsplit(".", 2)[-2] + "." + m.rsplit(".", 1)[-1] for m in missing)
                             + "; re-consent with: hsync auth (the daily sync keeps working meanwhile)")


def _print_json(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def cmd_log(a):
    kind = a.kind
    try:
        text = _read_input(a)
        if len(text) > 1_000_000:
            raise logs.ValidationError("input larger than 1 MB")
        raw = logs.load_entries(text)
        unit_map = logs.load_unit_map()
        entries = []
        for i, e in enumerate(raw):
            try:
                entries.append(logs.BUILDERS[kind](e, unit_map=unit_map))
            except logs.ValidationError as ve:
                raise logs.ValidationError(f"entry {i + 1}: {ve}") from None
        keys = [x["dedupe_key"] for x in entries]
        if len(set(keys)) != len(keys):
            raise logs.ValidationError("the input contains the same entry twice")
    except logs.ValidationError as e:
        print(f"INVALID: {e}  (nothing sent)", file=sys.stderr)
        return EXIT_ERR
    plan = [{"method": "POST", "url": f"{config.API_BASE}/users/{config.USER}/dataTypes/{x['data_type']}/dataPoints",
             "body": x["body"]} for x in entries]
    if not a.confirm:
        print(f"DRY RUN - nothing sent. {len(plan)} {kind} entr{'y' if len(plan) == 1 else 'ies'} would be written "
              "exactly as follows (re-run with --confirm to write):")
        _print_json(plan)
        return EXIT_OK
    try:
        _need_scopes({x["scope"] for x in entries})
        api = HealthApi(auth.Credentials())
        results = []
        for x in entries:
            status, name = logs.write(api, x)
            results.append({"status": status, "name": name, "client_id": x["client_id"]})
            print(f"{status:9s} {name}")
    except (auth.AuthError, AccountNotLinked) as e:
        print(f"AUTH REQUIRED: {redact(e)}", file=sys.stderr)
        return EXIT_AUTH
    except Exception as e:
        print(f"WRITE FAILED: {redact(f'{type(e).__name__}: {e}')}", file=sys.stderr)
        return EXIT_ERR
    return EXIT_OK


def cmd_list_logs(a):
    try:
        day = logs.validate_day(a.date)
    except logs.ValidationError as e:
        print(str(e), file=sys.stderr)
        return EXIT_ERR
    kinds = list(logs.KINDS) if a.type == "all" else [a.type]
    try:
        rows, errors = logs.list_day(HealthApi(auth.Credentials()), day, kinds, a.tz)
    except (auth.AuthError, AccountNotLinked) as e:
        print(f"AUTH REQUIRED: {redact(e)}", file=sys.stderr)
        return EXIT_AUTH
    except Exception as e:
        print(f"LIST FAILED: {redact(f'{type(e).__name__}: {e}')}", file=sys.stderr)
        return EXIT_ERR
    if a.json:
        _print_json({"date": day.isoformat(), "entries": rows, "errors": errors or None})
    else:
        for r in rows:
            print(f"{r['time'] or '?':22s} {r['kind']:8s} {r['what']}\n{'':31s}id: {r['name']}"
                  + ("  [health-sync]" if r["by_health_sync"] else ""))
        print(f"{len(rows)} entr{'y' if len(rows) == 1 else 'ies'} on {day.isoformat()}")
        for k, v in errors.items():
            print(f"WARN: {k}: {v}", file=sys.stderr)
    return EXIT_ERR if errors and not rows else EXIT_OK


def cmd_delete_log(a):
    try:
        by_type = {}
        for i in a.id:
            dt, name = logs.parse_name(i)
            by_type.setdefault(dt, [])
            if name not in by_type[dt]:
                by_type[dt].append(name)
    except logs.ValidationError as e:
        print(str(e), file=sys.stderr)
        return EXIT_ERR
    plan = [{"method": "POST", "url": f"{config.API_BASE}/users/{config.USER}/dataTypes/{dt}/dataPoints:batchDelete",
             "body": {"names": names}} for dt, names in by_type.items()]
    if not a.confirm:
        print("DRY RUN - nothing deleted. Would send (re-run with --confirm to delete):")
        _print_json(plan)
        return EXIT_OK
    try:
        _need_scopes({logs.KINDS[logs.TYPE_TO_KIND[dt]][2] for dt in by_type})
        api = HealthApi(auth.Credentials())
        for dt, names in by_type.items():
            logs.delete(api, dt, names)
            for n in names:
                print(f"deleted   {n}")
    except (auth.AuthError, AccountNotLinked) as e:
        print(f"AUTH REQUIRED: {redact(e)}", file=sys.stderr)
        return EXIT_AUTH
    except Exception as e:
        print(f"DELETE FAILED: {redact(f'{type(e).__name__}: {e}')}", file=sys.stderr)
        return EXIT_ERR
    return EXIT_OK


def cmd_food_units(a):
    try:
        if a.refresh:
            units = logs.refresh_unit_cache(HealthApi(auth.Credentials()))
            print(f"cached {len(units)} food measurement units -> {logs.food_units_file()}")
        m = logs.load_unit_map()
    except (auth.AuthError, AccountNotLinked) as e:
        print(f"AUTH REQUIRED: {redact(e)}", file=sys.stderr)
        return EXIT_AUTH
    except ApiError as e:
        if e.status == 403:
            print("food-measurement-unit needs the googlehealth.nutrition.readonly scope, which is not requested; "
                  "portions are written into the food name instead.", file=sys.stderr)
            return EXIT_AUTH
        print(f"FAILED: {redact(e)}", file=sys.stderr)
        return EXIT_ERR
    except Exception as e:
        print(f"FAILED: {redact(f'{type(e).__name__}: {e}')}", file=sys.stderr)
        return EXIT_ERR
    for u in logs.FOOD_UNITS:
        print(f"{u:8s} -> {m.get(u) or '(not mapped: portion goes into the food name)'}")
    return EXIT_OK


def main(argv=None):
    p = _Parser(prog="hsync", description="Google Health API -> daily JSON files, plus meal/drink/symptom logging")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--tz", default=config.TIMEZONE)
    p.add_argument("--out-dir", default=str(config.OUT_DIR))
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=_Parser)
    s = sub.add_parser("auth", help="one-time OAuth consent via loopback redirect")
    s.add_argument("--client-secret", help="path to downloaded client_secret_*.json (default: secrets/ or ~/Downloads)")
    s.add_argument("--port", type=int, default=config.DEFAULT_PORT, help="loopback port (0 = random)")
    s.add_argument("--timeout", type=int, default=900, help="seconds to wait for the redirect")
    s.set_defaults(fn=cmd_auth)
    s = sub.add_parser("auth-complete", help="finish auth by pasting the 127.0.0.1 redirect URL")
    s.add_argument("--redirect-url", required=True)
    s.set_defaults(fn=cmd_auth_complete)
    for name, fn in (("sync", cmd_sync), ("backfill", cmd_backfill)):
        s = sub.add_parser(name)
        if name == "sync":
            s.add_argument("--days", type=int, default=3, help="today and the N-1 days before (default 3)")
        else:
            s.add_argument("--from", dest="from_", required=True, type=_iso_date, help="YYYY-MM-DD")
            s.add_argument("--to", type=_iso_date, help="YYYY-MM-DD (default: today)")
        s.add_argument("--no-raw", action="store_true", help="don't keep raw API responses")
        s.set_defaults(fn=fn)
    for kind in logs.KINDS:
        s = sub.add_parser(f"log-{kind}", help=f"write {kind} entr(y/ies) from JSON (dry-run unless --confirm)")
        s.add_argument("--file", "-f", help="JSON file (object or array); default/'-': stdin")
        s.add_argument("--confirm", action="store_true", help="actually write (default: dry-run)")
        s.set_defaults(fn=cmd_log, kind=kind)
    s = sub.add_parser("list-logs", help="list meals/drinks/symptoms of a day")
    s.add_argument("--date", required=True, help="YYYY-MM-DD (civil date)")
    s.add_argument("--type", choices=["all", *logs.KINDS], default="all")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_list_logs)
    s = sub.add_parser("delete-log", help="delete logged entries by id (dry-run unless --confirm)")
    s.add_argument("--id", action="append", required=True, help="resource name from list-logs (repeatable)")
    s.add_argument("--confirm", action="store_true")
    s.set_defaults(fn=cmd_delete_log)
    s = sub.add_parser("food-units", help="show/refresh the cached food measurement units")
    s.add_argument("--refresh", action="store_true", help="fetch the list from the API (read-only)")
    s.set_defaults(fn=cmd_food_units)
    s = sub.add_parser("status", help="token valid? last sync?")
    s.add_argument("--offline", action="store_true", help="skip the live token/identity check")
    s.set_defaults(fn=cmd_status)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for h in logging.getLogger().handlers:  # every log line passes through credential redaction
        if not any(isinstance(f, RedactingFilter) for f in h.filters):
            h.addFilter(RedactingFilter())
    # urllib3 debug lines contain full request URLs; keep them out even with -v
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return a.fn(a)
