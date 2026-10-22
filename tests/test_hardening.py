"""Regression tests for the security/stability review (one block per fix)."""
import http.client
import json
import logging
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

import pytest
import requests

import fixtures
from conftest import ACCESS_VALUE, CLIENT_SECRET_VALUE, REFRESH_VALUE, FakeCreds, FakeHealth, Resp, client_json
from health_sync import api as api_mod
from health_sync import auth, cli, config, parse, sync, util
from health_sync.api import ApiError, HealthApi

ROOT = Path(__file__).resolve().parents[1]
SECRETS = (ACCESS_VALUE, REFRESH_VALUE, CLIENT_SECRET_VALUE)


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def make_api(fake, creds=None, rec=None):
    return HealthApi(creds or FakeCreds(), session=fake, recorder=rec, sleep=lambda s: None)


def store_tokens(expires_in=3000):
    auth.TokenStore().save({"access_token": ACCESS_VALUE, "expires_at": time.time() + expires_in,
                            "refresh_token": REFRESH_VALUE, "scopes": config.SCOPES})


# ------------------------------------------------------------------ redaction
def test_redact_strips_tokens_headers_and_query_params():
    s = util.redact(f"GET https://h/v4/x?filter=a&pageToken=p1&access_token={ACCESS_VALUE} "
                    f"Authorization: Bearer {ACCESS_VALUE} refresh_token={REFRESH_VALUE} code=4/0AbCdEfGhIjKlMn "
                    f"{CLIENT_SECRET_VALUE} {{'Authorization': 'Bearer abc.def'}} \"refresh_token\": \"{REFRESH_VALUE}\"")
    for v in SECRETS + ("4/0AbCdEfGhIjKlMn", "abc.def", "p1"):
        assert v not in s
    assert "filter=a" in s  # harmless params stay readable


def test_logging_filter_redacts_args_and_tracebacks(caplog):
    lg = logging.getLogger("hsync.test")
    h = logging.Handler()
    seen = []
    h.emit = lambda rec: seen.append(logging.Formatter().format(rec))
    h.addFilter(util.RedactingFilter())
    lg.addHandler(h)
    try:
        lg.error("token %s", ACCESS_VALUE)
        try:
            raise RuntimeError(f"Bearer {ACCESS_VALUE}")
        except RuntimeError:
            lg.exception("boom")
    finally:
        lg.removeHandler(h)
    assert seen and not any(ACCESS_VALUE in x for x in seen)


def test_cli_installs_redacting_filter(env):
    cli.main(["status", "--offline"])
    assert any(isinstance(f, util.RedactingFilter) for h in logging.getLogger().handlers for f in h.filters)


# ------------------------------------------------------------------ atomic private writes
def test_atomic_write_private_even_with_loose_umask(tmp_path):
    old = os.umask(0o022)
    try:
        util.write_json_atomic(tmp_path / "t.json", {"a": 1}, mode=0o600)
        util.write_text_atomic(tmp_path / "u.txt", "x", mode=0o600)
    finally:
        os.umask(old)
    assert mode(tmp_path / "t.json") == 0o600 and mode(tmp_path / "u.txt") == 0o600


def test_atomic_write_failure_leaves_target_and_no_tmp(tmp_path):
    p = tmp_path / "d.json"
    util.write_json_atomic(p, {"ok": 1})
    with pytest.raises(ValueError):
        util.write_json_atomic(p, {"bad": float("nan")})  # allow_nan=False
    assert json.loads(p.read_text()) == {"ok": 1}
    assert [x.name for x in tmp_path.iterdir()] == ["d.json"]


def test_non_finite_numbers_become_null():
    assert util.num("NaN") is None and util.num("Infinity") is None and util.num(float("inf")) is None
    assert util.rnd(float("nan")) is None and util.num("12") == 12
    pts = [{"dailyRestingHeartRate": {"date": {"year": 2026, "month": 1, "day": 2}, "beatsPerMinute": "NaN"}}]
    assert parse.parse_resting_hr(pts) == {}


def test_read_json_tolerates_corrupt_and_binary(tmp_path):
    (tmp_path / "a.json").write_text("{trunc")
    (tmp_path / "b.json").write_bytes(b"\xff\xfe\x00")
    assert util.read_json(tmp_path / "a.json", 7) == 7 and util.read_json(tmp_path / "b.json") is None
    assert util.read_json(tmp_path, 1) == 1  # a directory


# ------------------------------------------------------------------ client secret / token store
def test_install_client_secret_never_world_readable(env):
    old = os.umask(0o022)
    try:
        dst = auth.install_client_secret(client_json(env))
    finally:
        os.umask(old)
    assert mode(dst) == 0o600 and mode(config.SECRETS_DIR) == 0o700


def test_token_uri_is_pinned_and_bad_client_json_is_auth_error(env):
    p = client_json(env)
    d = json.loads(p.read_text())
    d["installed"]["token_uri"] = "http://evil.example/token"
    p.write_text(json.dumps(d))
    assert auth.load_client(p)["token_uri"] == "https://oauth2.googleapis.com/token"
    p.write_text("{not json")
    with pytest.raises(auth.AuthError):
        auth.load_client(p)
    p.write_text("[]")
    with pytest.raises(auth.AuthError):
        auth.load_client(p)


def test_token_store_repairs_loose_permissions(env):
    store_tokens()
    os.chmod(config.TOKEN_FILE, 0o644)
    assert auth.TokenStore().load()["refresh_token"] == REFRESH_VALUE
    assert mode(config.TOKEN_FILE) == 0o600


def test_corrupt_token_file_means_reauth(env, capsys):
    auth.install_client_secret(client_json(env))
    config.TOKEN_FILE.write_text('["x"]')
    assert cli.main(["--out-dir", str(env / "daily"), "sync", "--days", "1"]) == cli.EXIT_AUTH


# ------------------------------------------------------------------ OAuth flow
def test_loopback_ignores_wrong_state_then_accepts_right_one(env):
    c = auth.load_client(auth.install_client_secret(client_json(env)))
    httpd, url, pending = auth.start_flow(c, port=0)
    port = httpd.server_address[1]
    assert httpd.server_address[0] == "127.0.0.1"
    codes = []

    def browser():
        time.sleep(0.2)
        for q in ("state=evil&code=4/attacker", f"state={pending['state']}&code=4/good"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/?" + q)
            codes.append(conn.getresponse().status)
            conn.close()
    t = threading.Thread(target=browser, daemon=True)
    t.start()
    params = auth.wait_for_callback(httpd, timeout=10)
    t.join(5)
    assert codes == [400, 200] and params["code"] == "4/good"


def test_loopback_handler_has_socket_timeout_and_must_be_loopback(env):
    assert auth._Handler.timeout and auth._Handler.timeout <= 30
    c = auth.load_client(client_json(env))
    with pytest.raises(ValueError):
        auth.start_flow(c, port=0, host="0.0.0.0")  # nosec B104 - asserting it is refused


def test_pkce_is_s256_and_verifier_length_valid():
    import base64
    import hashlib
    v, ch = auth._pkce()
    assert 43 <= len(v) <= 128
    assert ch == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


def test_expired_pending_auth_rejected(env):
    c = auth.load_client(client_json(env))
    pending = {"state": "s", "code_verifier": "v", "redirect_uri": "r", "created": time.time() - auth.PENDING_MAX_AGE_S - 60}
    with pytest.raises(auth.AuthError, match="expired"):
        auth.finish(c, {"state": "s", "code": "x"}, pending, session=FakeHealth())


def test_refresh_transient_errors_retry_then_token_refresh_error(env, monkeypatch):
    monkeypatch.setattr(auth, "_sleep", lambda s: None)
    c = auth.load_client(client_json(env))
    calls = []

    class S:
        def post(self, url, data=None, timeout=None, allow_redirects=True):
            calls.append((timeout, allow_redirects))
            if len(calls) == 1:
                raise requests.ConnectionError(f"boom {url}?refresh_token={REFRESH_VALUE}")
            return Resp(503, {"error": "backend_error"})
    with pytest.raises(auth.TokenRefreshError) as ei:
        auth.refresh(c, {"refresh_token": REFRESH_VALUE}, session=S())
    assert len(calls) == auth.TOKEN_RETRIES + 1 and all(t and a is False for t, a in calls)
    assert REFRESH_VALUE not in str(ei.value)


def test_refresh_non_json_200_is_clean_error(env, monkeypatch):
    c = auth.load_client(client_json(env))

    class R(Resp):
        def json(self):
            raise ValueError("no json")

    class S:
        def post(self, *a, **kw):
            return R(200, {})
    with pytest.raises(auth.TokenRefreshError):
        auth.refresh(c, {"refresh_token": REFRESH_VALUE}, session=S())


def test_cli_sync_refresh_revoked_exits_2_without_leaking(env, capsys, monkeypatch):
    auth.install_client_secret(client_json(env))
    store_tokens(expires_in=-10)  # force a refresh
    fake = FakeHealth(fixtures.data())
    fake.refresh_status = 400
    monkeypatch.setattr("requests.post", fake.post)
    monkeypatch.setattr("requests.Session", lambda: fake)
    rc = cli.main(["--out-dir", str(env / "daily"), "sync", "--days", "2"])
    out = "".join(capsys.readouterr())
    assert rc == cli.EXIT_AUTH and "AUTH REQUIRED" in out
    assert not (env / "daily").exists() or not list((env / "daily").iterdir())
    blob = out + config.STATE_FILE.read_text()
    assert not any(s in blob for s in SECRETS)


def test_cli_sync_token_endpoint_down_exits_1(env, capsys, monkeypatch):
    monkeypatch.setattr(auth, "_sleep", lambda s: None)
    auth.install_client_secret(client_json(env))
    store_tokens(expires_in=-10)
    fake = FakeHealth(fixtures.data())
    fake.refresh_status = 503
    monkeypatch.setattr("requests.post", fake.post)
    monkeypatch.setattr("requests.Session", lambda: fake)
    assert cli.main(["--out-dir", str(env / "daily"), "sync", "--days", "2"]) == cli.EXIT_ERR
    assert fake.token_calls == auth.TOKEN_RETRIES + 1  # fatal once, not retried per data group


# ------------------------------------------------------------------ CLI exit codes / input validation
def test_lock_held_exits_4_and_lock_word_in_error_does_not(env, capsys, monkeypatch):
    auth.install_client_secret(client_json(env))
    store_tokens()
    with sync.Lock():
        assert cli.main(["--out-dir", str(env / "daily"), "sync", "--days", "1"]) == cli.EXIT_LOCKED
    fake = FakeHealth(fixtures.data())

    def req(method, url, **kw):
        return Resp(400, {"error": {"status": "FAILED_PRECONDITION", "message": "request blocked by lock policy"}})
    fake.request = req
    monkeypatch.setattr("requests.Session", lambda: fake)
    assert cli.main(["--out-dir", str(env / "daily"), "sync", "--days", "1"]) == cli.EXIT_ERR


@pytest.mark.parametrize("argv", [["sync", "--days", "x"], ["sync", "--days", "0"], ["nope"],
                                  ["backfill", "--from", "../../etc"], ["backfill", "--from", "20261019"],
                                  ["backfill", "--from", "2026-10-19", "--to", "2026-02-30"],
                                  ["backfill", "--from", "1990-01-01", "--to", "2026-01-01"]])
def test_bad_arguments_exit_1_not_2(env, argv, capsys):
    with pytest.raises(SystemExit) as ei:
        sys.exit(cli.main(argv))
    assert ei.value.code == cli.EXIT_ERR


def test_api_error_echoing_bearer_is_redacted_everywhere(env, capsys, monkeypatch):
    auth.install_client_secret(client_json(env))
    store_tokens()
    fake = FakeHealth(fixtures.data())

    def req(method, url, **kw):
        return Resp(400, {"error": {"status": "INVALID_ARGUMENT", "message": f"bad header Bearer {ACCESS_VALUE}"}})
    fake.request = req
    monkeypatch.setattr("requests.Session", lambda: fake)
    assert cli.main(["--out-dir", str(env / "daily"), "sync", "--days", "1"]) == cli.EXIT_ERR
    blob = "".join(capsys.readouterr()) + config.STATE_FILE.read_text()
    assert ACCESS_VALUE not in blob and "[REDACTED]" in blob


# ------------------------------------------------------------------ API transport
def test_retry_after_is_capped_and_http_date_falls_back():
    waits = []
    a = HealthApi(FakeCreds(), session=FakeHealth(), sleep=waits.append)
    a._backoff(0, "86400")
    a._backoff(0, "Wed, 21 Oct 2026 07:28:00 GMT")
    a._backoff(0, "-5")
    assert waits[0] == api_mod.MAX_BACKOFF_S and 2 <= waits[1] <= 3 and waits[2] == 0


def test_requests_use_connect_and_read_timeouts(env):
    fake = FakeHealth(fixtures.data())
    seen = []
    orig = fake.request

    def req(method, url, timeout=None, **kw):
        seen.append(timeout)
        return orig(method, url, timeout=timeout, **kw)
    fake.request = req
    make_api(fake).identity()
    assert seen == [(config.HTTP_CONNECT_TIMEOUT, config.HTTP_TIMEOUT)]


def test_non_json_response_is_a_group_error_not_a_crash(env):
    fake = FakeHealth(fixtures.data())
    orig = fake.request

    class R(Resp):
        def json(self):
            raise ValueError("html")

    def req(method, url, **kw):
        if "/sleep/" in url:
            return R(200, {"x": 1})
        return orig(method, url, **kw)
    fake.request = req
    s = sync.run(make_api(fake), date(2026, 10, 20), date(2026, 10, 21), tz="Europe/Paris")
    assert list(s["errors"]) == ["sleep"]


def test_repeated_page_token_stops_instead_of_looping(env):
    fake = FakeHealth(fixtures.data())

    def req(method, url, params=None, **kw):
        return Resp(200, {"dataPoints": [], "nextPageToken": "same"})
    fake.request = req
    with pytest.raises(ApiError, match="pagination"):
        list(make_api(fake).list_points("exercise"))


def test_network_error_message_has_no_url_or_params(env):
    class S:
        def request(self, method, url, params=None, **kw):
            raise requests.ConnectionError(f"Max retries exceeded with url: {url}?pageToken=SECRETPAGE")
    a = HealthApi(FakeCreds(), session=S(), sleep=lambda s: None, max_retries=1)
    with pytest.raises(ApiError) as ei:
        a.identity()
    assert "SECRETPAGE" not in str(ei.value) and "ConnectionError" in str(ei.value)


def test_plain_http_api_base_refused():
    with pytest.raises(ValueError):
        HealthApi(FakeCreds(), session=FakeHealth(), base="http://health.googleapis.com/v4")


# ------------------------------------------------------------------ partial failure / previous values
@pytest.mark.parametrize("prev", [["not", "a", "dict"], {"date": "2026-10-19", "hrv": {"rmssd_ms": 99}}])
def test_foreign_or_corrupt_previous_is_not_carried_over(prev):
    doc = sync.build_day("2026-10-20", {}, {"hrv": "boom"}, prev, "2026-10-21", "Europe/Paris", "x")
    assert doc["hrv"] is None and doc["meta"]["stale_groups"] is None


def test_corrupt_existing_day_file_is_rebuilt(env):
    config.OUT_DIR.mkdir(parents=True)
    (config.OUT_DIR / "2026-10-20.json").write_text("{garbage")
    sync.run(make_api(FakeHealth(fixtures.data())), date(2026, 10, 20), date(2026, 10, 21), tz="Europe/Paris")
    assert json.loads((config.OUT_DIR / "2026-10-20.json").read_text())["steps"] == 6000


def test_stale_tmp_files_are_cleaned(env):
    config.OUT_DIR.mkdir(parents=True)
    old, new = config.OUT_DIR / ".tmp-old.json", config.OUT_DIR / ".tmp-new.json"
    old.write_text("x")
    new.write_text("x")
    os.utime(old, (time.time() - 7200,) * 2)
    sync.run(make_api(FakeHealth(fixtures.data())), date(2026, 10, 20), date(2026, 10, 21), tz="Europe/Paris")
    assert not old.exists() and new.exists()


# ------------------------------------------------------------------ lock
def test_lock_survives_killed_holder_and_records_pid(env):
    code = ("import sys,time; sys.path.insert(0, %r); from health_sync import sync; from pathlib import Path\n"
            "with sync.Lock(Path(%r)):\n    print('held', flush=True); time.sleep(60)") % (str(ROOT), str(config.LOCK_FILE))
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "held"
        assert config.LOCK_FILE.read_text().strip() == str(p.pid)
        with pytest.raises(sync.LockBusy, match=str(p.pid)):
            with sync.Lock():
                pass
        p.send_signal(signal.SIGKILL)
        p.wait(5)
        with sync.Lock() as lk:  # stale lock file from a killed run: acquired immediately
            assert lk.fd is not None and mode(config.LOCK_FILE) == 0o600
    finally:
        p.kill()
        p.stdout.close()


def test_lock_refuses_symlink(env):
    target = env / "victim.txt"
    target.write_text("keep me")
    config.LOCK_FILE.symlink_to(target)
    with pytest.raises(RuntimeError, match="symlink"):
        with sync.Lock():
            pass
    assert target.read_text() == "keep me"


# ------------------------------------------------------------------ raw recorder
def test_raw_run_dirs_never_collide(env):
    a, b = sync.RawRecorder(root=config.RAW_DIR), sync.RawRecorder(root=config.RAW_DIR)
    a("get", "identity", {}, {})
    b("get", "identity", {}, {})
    assert a.run_dir != b.run_dir and mode(a.run_dir) == 0o700


# ------------------------------------------------------------------ DST (2026-10-25 has 25 hours in Paris)
def test_dst_end_day_sleep_and_rollups():
    pts = [{"sleep": {"interval": {"startTime": "2026-10-24T20:30:00Z", "startUtcOffset": "7200s",
                                   "endTime": "2026-10-25T06:00:00Z", "endUtcOffset": "3600s"},
                      "metadata": {"mainSleep": True}, "summary": {"minutesAsleep": "500"}}}]
    s = parse.parse_sleep(pts, "Europe/Paris")["2026-10-25"]
    assert s["bedtime"] == "2026-10-24T22:30+02:00" and s["wake_time"] == "2026-10-25T07:00+01:00"
    assert s["time_in_bed_min"] == 570  # 9.5 real hours, not 8.5 wall-clock hours
    ro = [fixtures.rollup("steps", "2026-10-25", {"countSum": "9000"}),
          fixtures.rollup("steps", "2026-10-26", {"countSum": "100"})]
    assert parse.parse_daily_rollup("steps", ro) == {"2026-10-25": 9000, "2026-10-26": 100}


def test_dst_end_day_exercise_in_repeated_hour():
    pts = [{"exercise": {"interval": {"startTime": "2026-10-25T00:30:00Z", "startUtcOffset": "7200s",
                                      "endTime": "2026-10-25T01:30:00Z", "endUtcOffset": "3600s"},
                         "exerciseType": "RUNNING", "activeDuration": "3600s"}}]
    w = parse.parse_exercise(pts, "Europe/Paris")["2026-10-25"][0]
    assert w["start"] == "2026-10-25T02:30+02:00" and w["end"] == "2026-10-25T02:30+01:00" and w["elapsed_min"] == 60.0


def test_dst_range_sync_writes_one_file_per_civil_day(env):
    data = {"steps": [fixtures.rollup("steps", d, {"countSum": "1000"}) for d in ("2026-10-24", "2026-10-25", "2026-10-26")]}
    sync.run(make_api(FakeHealth(data)), date(2026, 10, 24), date(2026, 10, 27), tz="Europe/Paris")
    assert sorted(p.name for p in config.OUT_DIR.iterdir()) == ["2026-10-24.json", "2026-10-25.json", "2026-10-26.json"]
    assert json.loads((config.OUT_DIR / "2026-10-25.json").read_text())["steps"] == 1000


# ------------------------------------------------------------------ daily_summary.csv
HEADER = "date,steps_best,kcal_total,bedtime"


def _csv_run(tmp, *extra):
    return subprocess.run([sys.executable, str(ROOT / "update_daily_summary.py"), "--csv", str(tmp / "s.csv"),
                           "--daily-dir", str(tmp / "daily"), *extra], capture_output=True, text=True, cwd="/")


def test_csv_update_skips_corrupt_and_mismatched_files_and_is_private(tmp_path):
    (tmp_path / "s.csv").write_text(HEADER + "\n2026-10-01,,,\n")
    d = tmp_path / "daily"
    d.mkdir()
    (d / "2026-10-01.json").write_text(json.dumps({"date": "2026-10-01", "steps": 5000, "meta": {"day_complete": True}}))
    (d / "2026-10-02.json").write_text("{corrupt")
    (d / "2026-10-03.json").write_text(json.dumps({"date": "2026-10-09", "steps": 1, "meta": {"day_complete": True}}))
    (d / "2026-10-04.json").write_text(json.dumps({"date": "2026-10-04", "steps": {"x": 1}, "meta": {"day_complete": True}}))
    r = _csv_run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "skipping" in r.stderr and "skipped files: 2" in r.stdout
    text = (tmp_path / "s.csv").read_text().splitlines()
    assert text[0] == HEADER and text[1] == "2026-10-01,5000.0,," and text[2] == "2026-10-04,,,"
    assert mode(tmp_path / "s.api_cells.json") == 0o600 and mode(tmp_path / "s.csv.bak-api") == 0o600
    assert mode(tmp_path / "s.csv") == 0o644
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".tmp")]


def test_csv_without_date_column_untouched(tmp_path):
    (tmp_path / "s.csv").write_text("a,b\n1,2\n")
    (tmp_path / "daily").mkdir()
    assert _csv_run(tmp_path).returncode == 1 and (tmp_path / "s.csv").read_text() == "a,b\n1,2\n"


# ------------------------------------------------------------------ shell scripts
def test_shell_scripts_strict_mode_and_syntax():
    for name in ("run_sync.sh", "hsync"):
        text = (ROOT / name).read_text()
        assert "set -euo pipefail" in text
        assert subprocess.run(["bash", "-n", str(ROOT / name)]).returncode == 0


@pytest.mark.parametrize("val", ["1; touch /tmp/hsync_pwned", "$(id)", "-3", "3 --no-raw"])
def test_run_sync_rejects_non_integer_days_before_doing_anything(val):
    r = subprocess.run(["bash", str(ROOT / "run_sync.sh")], env={**os.environ, "HSYNC_DAYS": val},
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 1 and "HSYNC_DAYS" in r.stderr
    assert not os.path.exists("/tmp/hsync_pwned")
