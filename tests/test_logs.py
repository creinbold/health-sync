"""hsync log-meal / log-drink / log-symptom / list-logs / delete-log (all values synthetic)."""
import io
import json
import os
import stat
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import pytest

from conftest import ACCESS_VALUE, CLIENT_SECRET_VALUE, REFRESH_VALUE, FakeHealth, Resp, client_json
from health_sync import auth, cli, config, logs

NOW = datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc)
MEAL = {"time": "2026-10-21T12:30", "meal_type": "lunch", "food_name": "Test oats", "portion": 200, "unit": "g",
        "kcal": 300, "protein_g": 10, "carbs_g": 50, "fat_g": 5, "sugar_g": 8, "sodium_mg": 120,
        "client_id": "diary-test-1"}
DRINK = {"time": "2026-10-21T13:00", "amount": 2, "unit": "cup", "drink": "tea"}
SYMPTOM = {"time": "2026-10-21T09:15+02:00", "symptoms": ["dry skin", "eczema", "DRY_SKIN"], "note": "test arm"}


USER_NUM = "1234567890"  # synthetic healthUserId


class FakeWriter(FakeHealth):
    """Adds create / get / batchDelete / log-type list, emulating the LIVE behaviour seen 2026-10-21:
    client-provided names ignored (numeric server ids under users/<healthUserId>/...), session types only
    filterable by interval.civil_start_time, symptoms without list/get, batchDelete always done."""

    def __init__(self):
        super().__init__()
        self.store = {}       # name -> DataPoint
        self.writes = []      # (method, path, body)
        self.create_status = None
        self.next_id = 9000000000000000001
        self.create_without_name = False

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        assert headers["Authorization"].startswith("Bearer ")
        path = urlparse(url).path.split("/v4/", 1)[1]
        self.calls.append((method, path, params, json))
        if method == "POST" and path.endswith("/dataPoints"):
            self.writes.append((method, path, json))
            if self.create_status:
                return Resp(self.create_status, {"error": {"code": self.create_status, "status": "ALREADY_EXISTS",
                                                           "message": "exists"}})
            dt = path.split("/dataTypes/")[1].split("/")[0]
            name = f"users/{USER_NUM}/dataTypes/{dt}/dataPoints/{self.next_id}"  # json["name"] is ignored
            self.next_id += 1
            self.store[name] = dict({k: v for k, v in json.items() if k != "name"}, name=name,
                                    dataSource={"platform": "GOOGLE_WEB_API"})
            if self.create_without_name:
                return Resp(200, {"done": False, "name": "operations/x"})
            return Resp(200, {"done": True, "response": self.store[name]})
        if method == "POST" and path.endswith(":batchDelete"):
            self.writes.append((method, path, json))
            for n in json["names"]:
                self.store.pop(n, None)
            return Resp(200, {"done": True})
        if method == "GET" and "/dataTypes/symptoms/" in path:
            return Resp(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                        "message": "List is not supported for data type symptoms"}})
        if method == "GET" and path.endswith("/dataPoints") and params and "filter" in params:
            f = params["filter"]
            if ".interval.start_time" in f or ".interval.end_time" in f or "sample_time" in f:
                return Resp(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message":
                                            "Invalid data point filter: INVALID_DATA_POINT_FILTER_DATA_TYPE_MEMBER."}})
        if method == "GET" and "/dataPoints/" in path:
            if path in self.store:
                return Resp(200, self.store[path])
            return Resp(404, {"error": {"code": 404, "status": "NOT_FOUND", "message": "nope"}})
        if method == "GET" and path.endswith("/dataPoints"):
            dt = path.split("/dataTypes/")[1].split("/")[0]
            pts = [v for k, v in self.store.items() if f"/dataTypes/{dt}/" in k]
            return Resp(200, {"dataPoints": pts})
        if path.endswith("/identity"):
            return Resp(200, {"healthUserId": "1"})
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture
def setup(env, monkeypatch):
    monkeypatch.setattr(config, "API_BASE", "https://health.googleapis.com/v4")
    monkeypatch.setattr(logs, "datetime", _FrozenDT)
    auth.install_client_secret(client_json(env))
    auth.TokenStore().save({"access_token": ACCESS_VALUE, "expires_at": time.time() + 3000,
                            "refresh_token": REFRESH_VALUE, "scopes": config.AUTH_SCOPES})
    fake = FakeWriter()
    monkeypatch.setattr("requests.Session", lambda: fake)
    return fake


class _FrozenDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


def run(monkeypatch, capsys, argv, data):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))
    rc = cli.main(argv)
    out = capsys.readouterr()
    return rc, out.out, out.err


def plan_of(out):
    return json.loads(out[out.index("["):])


# ------------------------------------------------------------------ dry run / confirm / exactness
def test_dry_run_sends_nothing_and_shows_exact_body(setup, monkeypatch, capsys):
    rc, out, err = run(monkeypatch, capsys, ["log-meal"], MEAL)
    assert rc == 0 and "DRY RUN" in out and setup.calls == [] and not logs.audit_path().exists()
    body = plan_of(out)[0]["body"]
    nl = body["nutritionLog"]
    assert list(body) == ["nutritionLog"]  # no client-provided name (the API ignores it)
    assert nl["interval"] == {"startTime": "2026-10-21T10:30:00Z", "startUtcOffset": "7200s",
                              "endTime": "2026-10-21T10:30:01Z", "endUtcOffset": "7200s"}
    assert nl["mealType"] == "LUNCH" and nl["foodDisplayName"] == "Test oats (200 g)"
    assert nl["energy"] == {"kcal": 300} and nl["totalCarbohydrate"] == {"grams": 50} and nl["totalFat"] == {"grams": 5}
    assert {"nutrient": "SODIUM", "quantity": {"grams": 0.12}} in nl["nutrients"]
    assert {"nutrient": "PROTEIN", "quantity": {"grams": 10}} in nl["nutrients"]


def test_confirm_sends_exactly_the_dry_run_body_and_audits(setup, monkeypatch, capsys):
    _, out, _ = run(monkeypatch, capsys, ["log-meal"], MEAL)
    planned = plan_of(out)
    rc, out, err = run(monkeypatch, capsys, ["log-meal", "--confirm"], MEAL)
    assert rc == 0 and out.startswith("created")
    assert [(m, p, b) for m, p, b in setup.writes] == [("POST", "users/me/dataTypes/nutrition-log/dataPoints",
                                                         planned[0]["body"])]
    st = os.stat(logs.audit_path())
    assert stat.S_IMODE(st.st_mode) == 0o600 and stat.S_IMODE(os.stat(logs.audit_dir()).st_mode) == 0o700
    text = logs.audit_path().read_text()
    rec = json.loads(text.splitlines()[-1])
    assert rec["action"] == "create" and rec["status"] == "created" and rec["client_id"] == "diary-test-1"
    for s in (ACCESS_VALUE, REFRESH_VALUE, CLIENT_SECRET_VALUE, "Bearer", "Authorization"):
        assert s not in text and s not in out + err


def test_repeat_is_idempotent_via_ledger(setup, monkeypatch, capsys):
    run(monkeypatch, capsys, ["log-meal", "--confirm"], MEAL)
    rc, out, _ = run(monkeypatch, capsys, ["log-meal", "--confirm"], MEAL)
    assert rc == 0 and out.startswith("duplicate") and len(setup.writes) == 1


def test_same_client_id_with_changed_text_is_still_a_duplicate(setup, monkeypatch, capsys):
    run(monkeypatch, capsys, ["log-meal", "--confirm"], MEAL)
    run(monkeypatch, capsys, ["log-meal", "--confirm"], dict(MEAL, food_name="Test oats edited"))
    assert len(setup.writes) == 1


def test_without_client_id_content_hash_dedupes(setup, monkeypatch, capsys):
    m = {k: v for k, v in MEAL.items() if k != "client_id"}
    run(monkeypatch, capsys, ["log-meal", "--confirm"], m)
    logs.audit_path().unlink()  # even without the local ledger: deterministic id + remote match
    rc, out, _ = run(monkeypatch, capsys, ["log-meal", "--confirm"], m)
    assert out.startswith("duplicate") and len(setup.writes) == 1


def test_remote_entry_from_elsewhere_is_detected(setup, monkeypatch, capsys):
    setup.store[f"users/{USER_NUM}/dataTypes/hydration-log/dataPoints/app-1"] = {
        "name": f"users/{USER_NUM}/dataTypes/hydration-log/dataPoints/app-1",
        "hydrationLog": {"interval": {"startTime": "2026-10-21T11:00:00Z"}, "amountConsumed": {"milliliters": 473.2}}}
    rc, out, _ = run(monkeypatch, capsys, ["log-drink", "--confirm"], DRINK)
    assert rc == 0 and "duplicate" in out and "app-1" in out and setup.writes == []


def test_already_exists_409_is_duplicate(setup, monkeypatch, capsys):
    setup.create_status = 409
    rc, out, _ = run(monkeypatch, capsys, ["log-symptom", "--confirm"], SYMPTOM)
    assert rc == 0 and out.startswith("duplicate")


def test_deleted_in_app_gets_recreated(setup, monkeypatch, capsys):
    run(monkeypatch, capsys, ["log-meal", "--confirm"], MEAL)
    setup.store.clear()  # user removed it in the app
    rc, out, _ = run(monkeypatch, capsys, ["log-meal", "--confirm"], MEAL)
    assert out.startswith("created") and len(setup.writes) == 2


def test_api_error_is_exit_1_and_audited(setup, monkeypatch, capsys):
    setup.create_status = 400
    rc, out, err = run(monkeypatch, capsys, ["log-meal", "--confirm"], MEAL)
    assert rc == 1 and "WRITE FAILED" in err
    assert json.loads(logs.audit_path().read_text().splitlines()[-1])["status"] == "error"


def test_missing_write_scope_exits_2_and_sends_nothing(setup, monkeypatch, capsys):
    auth.TokenStore().save({"access_token": ACCESS_VALUE, "expires_at": time.time() + 3000,
                            "refresh_token": REFRESH_VALUE, "scopes": config.READ_SCOPES})
    rc, _, err = run(monkeypatch, capsys, ["log-meal", "--confirm"], MEAL)
    assert rc == 2 and "nutrition.writeonly" in err and setup.calls == []


# ------------------------------------------------------------------ payloads
def test_drink_units_converted_to_ml():
    d = logs.build_drink(DRINK, now=NOW)
    assert d["body"]["hydrationLog"]["amountConsumed"] == {"milliliters": 473.2, "userProvidedUnit": "CUP_US"}
    assert d["data_type"] == "hydration-log" and d["scope"] == config.NUTRITION_WRITE_SCOPE


def test_symptom_payload_aliases_and_offset():
    s = logs.build_symptom(SYMPTOM, now=NOW)
    assert s["body"]["symptoms"] == {"sampleTime": {"physicalTime": "2026-10-21T07:15:00Z", "utcOffset": "7200s"},
                                     "symptoms": ["DRY_SKIN", "OTHER"]}
    assert s["data_type"] == "symptoms" and s["scope"] == config.SYMPTOMS_WRITE_SCOPE
    assert "note" not in json.dumps(s["body"])  # notes stay local (audit only)


def test_unit_cache_maps_serving(env, tmp_path, monkeypatch):
    f = tmp_path / "units.json"
    f.write_text(json.dumps({"units": [{"name": "users/me/dataTypes/food-measurement-unit/dataPoints/147",
                                        "displayName": "gram"}]}))
    m = logs.build_meal(MEAL, now=NOW, unit_map=logs.load_unit_map(f))
    nl = m["body"]["nutritionLog"]
    assert nl["foodDisplayName"] == "Test oats" and nl["serving"] == {
        "amount": 200, "foodMeasurementUnit": "users/me/dataTypes/food-measurement-unit/dataPoints/147"}


def test_duration_and_winter_offset():
    m = logs.build_meal(dict(MEAL, time="2026-10-20T19:00", duration_min=20), now=datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert m["body"]["nutritionLog"]["interval"]["endTime"] == "2026-10-20T17:20:00Z"
    w = logs.build_drink(dict(DRINK, time="2026-10-26T08:00"), now=datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert w["body"]["hydrationLog"]["interval"]["startUtcOffset"] == "3600s"


# ------------------------------------------------------------------ validation
BAD_MEALS = [
    ({"time": "2026-10-21T21:30"}, "future"),                         # NOW is 20:00 local
    ({"time": "2025-01-01T12:00"}, "days ago"),
    ({"time": "2026-10-21"}, "time of day"),
    ({"time": "yesterday noon"}, "ISO 8601"),
    ({"time": "2026-10-25T02:30"}, "ambiguous"),
    ({"time": "2026-03-29T02:30"}, "does not exist"),
    ({"unit": "bucket"}, "not supported"),
    ({"portion": 0}, "plausible"),
    ({"portion": 5000}, "plausible"),
    ({"portion": "200"}, "number"),
    ({"kcal": 9000}, "plausible"),
    ({"kcal": 100}, "macros add up"),
    ({"sugar_g": 60}, "sugar_g"),
    ({"protein_g": 300, "carbs_g": 0, "fat_g": 0, "kcal": 1300, "sugar_g": 0}, "weigh more"),
    ({"meal_type": "brunch"}, "meal_type"),
    ({"food_name": ""}, "required"),
    ({"food_name": "a\x00b"}, "control"),
    ({"client_id": "../../etc"}, "client_id"),
    ({"extra": 1}, "unknown field"),
    ({"duration_min": 500}, "plausible"),
    ({"end_time": "2026-10-21T12:00"}, "after"),
]


@pytest.mark.parametrize("patch,msg", BAD_MEALS)
def test_meal_validation(patch, msg):
    with pytest.raises(logs.ValidationError, match=msg):
        logs.build_meal(dict(MEAL, **patch), now=NOW)


@pytest.mark.parametrize("entry,msg", [
    (dict(DRINK, amount=20, unit="l"), "plausible"), (dict(DRINK, unit="barrel"), "not supported"),
    (dict(DRINK, amount=-1), "plausible"), (dict(DRINK, amount=True), "number")])
def test_drink_validation(entry, msg):
    with pytest.raises(logs.ValidationError, match=msg):
        logs.build_drink(entry, now=NOW)


@pytest.mark.parametrize("entry,msg", [
    (dict(SYMPTOM, symptoms=["werewolf"]), "unknown symptom"), (dict(SYMPTOM, symptoms=[]), "1..20"),
    (dict(SYMPTOM, symptoms=["OTHER"] * 21), "1..20")])
def test_symptom_validation(entry, msg):
    with pytest.raises(logs.ValidationError, match=msg):
        logs.build_symptom(entry, now=NOW)


@pytest.mark.parametrize("text,msg", [("{", "not valid JSON"), ("[]", "no entries"), ("[1]", "JSON object"),
                                      ('{"portion": NaN}', "not allowed")])
def test_input_parsing(text, msg):
    with pytest.raises(logs.ValidationError, match=msg):
        logs.load_entries(text)


def test_batch_with_one_bad_entry_sends_nothing(setup, monkeypatch, capsys):
    rc, _, err = run(monkeypatch, capsys, ["log-meal", "--confirm"], [MEAL, dict(MEAL, client_id="x2", unit="bucket")])
    assert rc == 1 and "entry 2" in err and setup.calls == []


def test_same_entry_twice_in_batch_rejected(setup, monkeypatch, capsys):
    rc, _, err = run(monkeypatch, capsys, ["log-meal"], [MEAL, MEAL])
    assert rc == 1 and "twice" in err


def test_file_input(setup, monkeypatch, capsys, tmp_path):
    f = tmp_path / "m.json"
    f.write_text(json.dumps([DRINK]))
    assert cli.main(["log-drink", "--file", str(f)]) == 0
    assert cli.main(["log-drink", "--file", str(tmp_path / "missing.json")]) == 1


# ------------------------------------------------------------------ list / delete
def test_list_logs_filters_and_marks(setup, monkeypatch, capsys):
    run(monkeypatch, capsys, ["log-meal", "--confirm"], MEAL)
    rc, out, _ = run(monkeypatch, capsys, ["list-logs", "--date", "2026-10-21", "--json"], {})
    data = json.loads(out)
    assert rc == 0 and data["entries"][0]["kind"] == "meal" and data["entries"][0]["by_health_sync"]
    assert data["entries"][0]["time"] == "2026-10-21T12:30+02:00"
    flts = {p: prm["filter"] for m, p, prm, b in setup.calls if m == "GET" and prm and "filter" in prm}
    assert flts["users/me/dataTypes/nutrition-log/dataPoints"].startswith('nutrition_log.interval.civil_start_time >= "2026-10-21"')
    assert "users/me/dataTypes/symptoms/dataPoints" not in flts  # symptoms can't be listed
    assert cli.main(["list-logs", "--date", "21.10.2026"]) == 1


def test_delete_dry_run_then_confirm(setup, monkeypatch, capsys):
    _, out, _ = run(monkeypatch, capsys, ["log-meal", "--confirm"], MEAL)
    name = out.split()[1]
    rc, out, _ = run(monkeypatch, capsys, ["delete-log", "--id", name], {})
    assert rc == 0 and "DRY RUN" in out and len(setup.writes) == 1
    assert plan_of(out)[0]["body"] == {"names": [name]}
    rc, out, _ = run(monkeypatch, capsys, ["delete-log", "--id", name, "--confirm"], {})
    assert rc == 0 and setup.writes[-1] == ("POST", "users/me/dataTypes/nutrition-log/dataPoints:batchDelete",
                                            {"names": [name]})
    assert json.loads(logs.audit_path().read_text().splitlines()[-1])["status"] == "deleted"
    assert logs.ledger() == {}  # deleted entries no longer block a re-log
    rc, out, _ = run(monkeypatch, capsys, ["log-meal", "--confirm"], MEAL)
    assert out.startswith("created")


@pytest.mark.parametrize("bad", ["../../secrets/token", "users/me/dataTypes/steps/dataPoints/x",
                                 "users/me/dataTypes/nutrition-log/dataPoints/a/../../b", "nutrition-log/a?b=c",
                                 "users/me/dataTypes/symptoms/dataPoints/x y", "users/../dataTypes/symptoms/dataPoints/x"])
def test_delete_rejects_bad_ids(setup, bad, capsys):
    assert cli.main(["delete-log", "--id", bad, "--confirm"]) == 1 and setup.calls == []


def test_short_id_form():
    assert logs.parse_name("symptoms/hs-abc") == ("symptoms", "users/me/dataTypes/symptoms/dataPoints/hs-abc")


# ------------------------------------------------------------------ re-consent safety
def test_reconsent_requests_all_scopes(env):
    import urllib.parse
    c = auth.load_client(client_json(env))
    url = auth.build_auth_url(c, "http://127.0.0.1:8765", "st", "ch")
    sc = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["scope"][0].split()
    assert set(config.READ_SCOPES) <= set(sc) and set(config.WRITE_SCOPES) <= set(sc)
    assert {"https://www.googleapis.com/auth/googlehealth.nutrition.writeonly",
            "https://www.googleapis.com/auth/googlehealth.logged_symptoms.writeonly",
            "https://www.googleapis.com/auth/googlehealth.logged_symptoms.readonly"} <= set(sc)


def _pending():
    return {"state": "s", "code_verifier": "v", "redirect_uri": "http://127.0.0.1:8765", "created": time.time()}


def test_failed_exchange_keeps_old_token(env):
    c = auth.load_client(client_json(env))
    old = {"access_token": "old", "expires_at": 1, "refresh_token": "old-refresh", "scopes": config.READ_SCOPES}
    auth.TokenStore().save(old)

    class S:
        def post(self, *a, **kw):
            return Resp(400, {"error": "invalid_grant"})
    with pytest.raises(auth.AuthError):
        auth.finish(c, {"state": "s", "code": "x"}, _pending(), session=S())
    assert auth.TokenStore().load() == old


def test_reconsent_missing_read_scope_keeps_old_token(env):
    c = auth.load_client(client_json(env))
    old = {"access_token": "old", "expires_at": 1, "refresh_token": "old-refresh", "scopes": config.READ_SCOPES}
    auth.TokenStore().save(old)

    class S:
        def post(self, *a, **kw):
            return Resp(200, {"access_token": "new", "expires_in": 3600, "refresh_token": "new-refresh",
                              "scope": " ".join(config.WRITE_SCOPES + config.READ_SCOPES[1:])})
    with pytest.raises(auth.AuthError, match="kept the existing token"):
        auth.finish(c, {"state": "s", "code": "x"}, _pending(), session=S())
    assert auth.TokenStore().load() == old
    assert stat.S_IMODE(os.stat(config.SECRETS_DIR / "token.rejected.json").st_mode) == 0o600


def test_reconsent_full_grant_replaces_token(env):
    c = auth.load_client(client_json(env))
    auth.TokenStore().save({"access_token": "old", "expires_at": 1, "refresh_token": "old-refresh",
                            "scopes": config.READ_SCOPES})
    fake = FakeHealth()
    tok = auth.finish(c, {"state": "s", "code": "x"}, _pending(), session=fake)
    assert auth.TokenStore().load()["refresh_token"] == REFRESH_VALUE == tok["refresh_token"]
    assert set(config.WRITE_SCOPES) <= set(tok["scopes"])


# ------------------------------------------------------------------ live-API regressions (2026-10-21)
def test_root_cause_duplicate_check_uses_civil_start_time_filter(setup, monkeypatch, capsys):
    rc, out, err = run(monkeypatch, capsys, ["log-drink", "--confirm"], DRINK)
    assert rc == 0 and out.startswith("created"), err
    f = [prm["filter"] for m, p, prm, b in setup.calls if m == "GET" and prm and "filter" in prm][0]
    assert f == ('hydration_log.interval.civil_start_time >= "2026-10-21T12:59:00" AND '
                 'hydration_log.interval.civil_start_time < "2026-10-21T13:01:00"')


def test_remote_filter_uses_entry_local_wall_clock():
    e = logs.build_drink(dict(DRINK, time="2026-10-21T08:00-04:00"), now=NOW)
    assert logs.remote_filter("hydration-log", e["when"]).startswith(
        'hydration_log.interval.civil_start_time >= "2026-10-21T07:59:00"')


def test_server_assigned_name_is_recorded_and_used_for_dedupe(setup, monkeypatch, capsys):
    _, out, _ = run(monkeypatch, capsys, ["log-meal", "--confirm"], MEAL)
    assert out.split()[1] == f"users/{USER_NUM}/dataTypes/nutrition-log/dataPoints/9000000000000000001"
    rc, out, _ = run(monkeypatch, capsys, ["log-meal", "--confirm"], MEAL)
    assert out.split()[:2] == ["duplicate", f"users/{USER_NUM}/dataTypes/nutrition-log/dataPoints/9000000000000000001"]
    assert len(setup.writes) == 1


def test_symptom_without_list_or_get(setup, monkeypatch, capsys):
    rc, out, err = run(monkeypatch, capsys, ["log-symptom", "--confirm"], SYMPTOM)
    assert rc == 0 and out.startswith("created"), err
    n_calls = len(setup.calls)
    rc, out, _ = run(monkeypatch, capsys, ["log-symptom", "--confirm"], SYMPTOM)
    assert rc == 0 and out.startswith("duplicate") and len(setup.calls) == n_calls  # ledger only, no API
    rc, out, _ = run(monkeypatch, capsys, ["list-logs", "--date", "2026-10-21", "--type", "symptom", "--json"], {})
    rows = json.loads(out)["entries"]
    assert rc == 0 and rows[0]["what"] == "DRY_SKIN, OTHER" and rows[0]["by_health_sync"]
    assert rows[0]["time"] == "2026-10-21T09:15+02:00" and "audit log" in rows[0]["source"]


def test_later_duplicate_record_does_not_hide_created_one(setup, monkeypatch, capsys):
    for _ in range(3):
        run(monkeypatch, capsys, ["log-symptom", "--confirm"], SYMPTOM)
    (rec,) = logs.ledger().values()
    assert rec["status"] == "created"


def test_failure_before_create_is_audited(setup, monkeypatch, capsys):
    orig = setup.request

    def req(method, url, params=None, **kw):
        if method == "GET" and params and "filter" in params:
            return Resp(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "bad filter"}})
        return orig(method, url, params=params, **kw)
    setup.request = req
    rc, _, err = run(monkeypatch, capsys, ["log-drink", "--confirm"], DRINK)
    assert rc == 1 and setup.writes == []
    rec = json.loads(logs.audit_path().read_text().splitlines()[-1])
    assert rec["status"] == "error" and rec["http_status"] == 400


def test_create_without_returned_name_is_an_error(setup, monkeypatch, capsys):
    setup.create_without_name = True
    rc, _, err = run(monkeypatch, capsys, ["log-drink", "--confirm"], DRINK)
    assert rc == 1 and "no data point name" in err


def test_delete_accepts_numeric_user_names_and_rejects_others():
    n = f"users/{USER_NUM}/dataTypes/hydration-log/dataPoints/6400000000000000000"
    assert logs.parse_name(n) == ("hydration-log", n)
    for bad in ("users/12ab/dataTypes/hydration-log/dataPoints/1", f"users/{'9' * 31}/dataTypes/symptoms/dataPoints/1"):
        with pytest.raises(logs.ValidationError):
            logs.parse_name(bad)


def test_food_units_without_readonly_scope_exits_2(setup, monkeypatch, capsys):
    orig = setup.request

    def req(method, url, **kw):
        if "food-measurement-unit" in url:
            return Resp(403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "scope missing"}})
        return orig(method, url, **kw)
    setup.request = req
    assert cli.main(["food-units", "--refresh"]) == 2
    assert "nutrition.readonly" in capsys.readouterr().err
