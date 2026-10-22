import json
from datetime import date

import pytest

from conftest import FakeCreds, FakeHealth
import fixtures
from health_sync import config, sync
from health_sync.api import AccountNotLinked, HealthApi
from conftest import Resp


def make_api(fake, creds=None, rec=None):
    return HealthApi(creds or FakeCreds(), session=fake, recorder=rec, sleep=lambda s: None)


def test_full_sync_writes_expected_docs(env):
    fake = FakeHealth(fixtures.data())
    rec = sync.RawRecorder(root=config.RAW_DIR)
    s = sync.run(make_api(fake, rec=rec), date(2026, 10, 19), date(2026, 10, 22), tz="Europe/Paris", recorder=rec)
    assert s["files"] == 3 and s["errors"] is None
    d = json.loads((config.OUT_DIR / "2026-10-20.json").read_text())
    assert list(d) == sync.FIELD_ORDER
    assert d["steps"] == 6000 and d["calories_total"] == 2200.5 and d["calories_active"] == 600.4
    assert d["sedentary_minutes"] == 500 and d["resting_heart_rate"] == 60
    assert d["hrv"]["rmssd_ms"] == 30.0 and d["spo2"]["avg_pct"] == 97.0 and d["breathing_rate"] == 15.0
    assert d["skin_temperature"]["deviation_c"] == -0.25
    assert d["sleep"]["duration_min"] == 420 and d["sleep"]["stages"]["rem"] == 120
    assert d["workouts"][0]["type"] == "WALKING"
    assert d["meta"]["timezone"] == "Europe/Paris" and d["meta"]["errors"] is None and d["meta"]["sync_time"]
    # missing values are null, never invented
    d19 = json.loads((config.OUT_DIR / "2026-10-19.json").read_text())
    assert d19["sleep"] is None and d19["hrv"] is None and d19["spo2"] is None and d19["workouts"] == []
    d21 = json.loads((config.OUT_DIR / "2026-10-21.json").read_text())
    assert d21["calories_total"] is None and d21["steps"] == 1000
    # raw responses recorded privately
    assert rec.run_dir and len(list(rec.run_dir.iterdir())) == len(fake.calls)
    assert oct(rec.run_dir.stat().st_mode)[-3:] == "700"
    st = json.loads(config.STATE_FILE.read_text())
    assert st["last_full_success"] and st["last_sync_range"] == ["2026-10-19", "2026-10-21"]


def test_requests_follow_documented_endpoints_and_filters(env):
    fake = FakeHealth(fixtures.data())
    sync.run(make_api(fake), date(2026, 10, 20), date(2026, 10, 22), tz="Europe/Paris")
    seen = {(m, u.split("/v4/")[1]): (p, b) for m, u, p, b in fake.calls}
    p, b = seen[("POST", "users/me/dataTypes/steps/dataPoints:dailyRollUp")]
    assert b["range"] == {"start": {"date": {"year": 2026, "month": 10, "day": 20}},
                          "end": {"date": {"year": 2026, "month": 10, "day": 22}}} and b["windowSizeDays"] == 1
    p, _ = seen[("GET", "users/me/dataTypes/daily-resting-heart-rate/dataPoints")]
    assert p["filter"] == 'daily_resting_heart_rate.date >= "2026-10-20" AND daily_resting_heart_rate.date < "2026-10-22"'
    p, _ = seen[("GET", "users/me/dataTypes/sleep/dataPoints")]
    assert p["filter"].startswith('sleep.interval.civil_end_time >= "2026-10-20"') and p["pageSize"] == 25
    p, _ = seen[("GET", "users/me/dataTypes/exercise/dataPoints")]
    assert 'exercise.interval.civil_start_time < "2026-10-22"' in p["filter"]
    # only days in range are written
    assert sorted(x.name for x in config.OUT_DIR.iterdir()) == ["2026-10-20.json", "2026-10-21.json"]


def test_backfill_chunks_total_calories_to_14_days(env):
    fake = FakeHealth({})
    sync.run(make_api(fake), date(2026, 8, 1), date(2026, 9, 15), tz="Europe/Paris")  # FakeHealth asserts <= 14d
    n = [c for c in fake.calls if "total-calories" in c[1]]
    assert len(n) == 4
    assert len(list(config.OUT_DIR.iterdir())) == 45


def test_pagination_followed(env):
    data = fixtures.data()
    data["exercise"] = data["exercise"] * 30  # 30 sessions
    fake = FakeHealth(data)
    fake.page_size_split["exercise"] = 25
    sync.run(make_api(fake), date(2026, 10, 20), date(2026, 10, 21), tz="Europe/Paris")
    d = json.loads((config.OUT_DIR / "2026-10-20.json").read_text())
    assert len(d["workouts"]) == 30
    assert any(c[2] and c[2].get("pageToken") == "25" for c in fake.calls)


def test_retry_on_429_and_refresh_on_401(env):
    fake = FakeHealth(fixtures.data())
    fake.fail["steps"] = [429, 503]
    fake.fail["sleep"] = [401]
    creds = FakeCreds()
    sync.run(make_api(fake, creds), date(2026, 10, 20), date(2026, 10, 21), tz="Europe/Paris")
    d = json.loads((config.OUT_DIR / "2026-10-20.json").read_text())
    assert d["steps"] == 6000 and d["sleep"]["duration_min"] == 420 and creds.refreshes == 1


def test_failed_group_keeps_previous_value_and_flags_it(env):
    sync.run(make_api(FakeHealth(fixtures.data())), date(2026, 10, 20), date(2026, 10, 21), tz="Europe/Paris")
    fake = FakeHealth(fixtures.data())
    fake.fail["daily-heart-rate-variability"] = [403]  # e.g. scope withdrawn
    fake.data["steps"][1]["steps"]["countSum"] = "6500"  # late-synced data
    s = sync.run(make_api(fake), date(2026, 10, 20), date(2026, 10, 21), tz="Europe/Paris")
    d = json.loads((config.OUT_DIR / "2026-10-20.json").read_text())
    assert d["steps"] == 6500  # overwritten with fresh data
    assert d["hrv"]["rmssd_ms"] == 30.0  # kept
    assert d["meta"]["stale_groups"] == ["hrv"] and "hrv" in d["meta"]["errors"]
    assert "hrv" in s["errors"]


def test_failed_group_without_previous_is_null(env):
    fake = FakeHealth(fixtures.data())
    fake.fail["exercise"] = [400]
    sync.run(make_api(fake), date(2026, 10, 20), date(2026, 10, 21), tz="Europe/Paris")
    d = json.loads((config.OUT_DIR / "2026-10-20.json").read_text())
    assert d["workouts"] is None


def test_all_groups_failing_writes_nothing(env):
    fake = FakeHealth(fixtures.data())
    for g, (_, dt, _) in sync.GROUPS.items():
        fake.fail[dt] = [400]
    with pytest.raises(RuntimeError):
        sync.run(make_api(fake), date(2026, 10, 20), date(2026, 10, 21), tz="Europe/Paris")
    assert not config.OUT_DIR.exists() or not list(config.OUT_DIR.iterdir())


def test_account_not_linked_aborts(env):
    fake = FakeHealth(fixtures.data())

    def req(method, url, **kw):
        return Resp(400, {"error": {"code": 400, "message": "The account is not linked to Google Health.",
                                    "status": "FAILED_PRECONDITION", "details": [
                                        {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "ACCOUNT_NOT_LINKED",
                                         "domain": "health.googleapis.com",
                                         "metadata": {"redirect_uri": "https://fitbit.google.com/auth/signup"}}]}})
    fake.request = req
    with pytest.raises(AccountNotLinked):
        sync.run(make_api(fake), date(2026, 10, 20), date(2026, 10, 21), tz="Europe/Paris")


def test_idempotent(env):
    for _ in range(2):
        sync.run(make_api(FakeHealth(fixtures.data())), date(2026, 10, 19), date(2026, 10, 22), tz="Europe/Paris")
    docs = [json.loads(p.read_text()) for p in sorted(config.OUT_DIR.iterdir())]
    assert [d["date"] for d in docs] == fixtures.DAYS
    assert not [p for p in config.OUT_DIR.iterdir() if p.name.startswith(".tmp")]


def test_day_complete_flag(env):
    doc = sync.build_day("2026-10-21", {}, {}, None, "2026-10-21", "Europe/Paris", "x")
    assert doc["meta"]["day_complete"] is False and doc["steps"] is None and doc["workouts"] == []
    assert sync.build_day("2026-10-20", {}, {}, None, "2026-10-21", "Europe/Paris", "x")["meta"]["day_complete"]


def test_lock_prevents_overlap(env):
    with sync.Lock():
        with pytest.raises(RuntimeError):
            with sync.Lock():
                pass
