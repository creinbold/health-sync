"""Writing meals, drinks and symptoms to Google Health (e.g. from a bot that keeps the user's food and symptom diary).

Data types (discovery v4 rev20261020, DataPoint union fields):
  nutrition-log  -> DataPoint.nutritionLog  (NutritionLog: interval, foodDisplayName, mealType, energy{kcal},
                    totalCarbohydrate{grams}, totalFat{grams}, nutrients[{nutrient, quantity{grams}}],
                    serving{amount, foodMeasurementUnit})                       scope nutrition.writeonly
  hydration-log  -> DataPoint.hydrationLog  (HydrationLog: interval, amountConsumed{milliliters, userProvidedUnit})
                                                                                scope nutrition.writeonly
  symptoms       -> DataPoint.symptoms      (Symptoms: sampleTime{physicalTime, utcOffset}, symptoms[enum])
                                                                                scope logged_symptoms.writeonly
Methods: users.dataTypes.dataPoints.create / list / batchDelete.

Verified live (2026-10-21):
- session types (nutrition-log, hydration-log) accept ONLY `<type>.interval.civil_start_time` filters
  (date or local date-time literal); `interval.start_time`/`end_time` -> INVALID_DATA_POINT_FILTER_DATA_TYPE_MEMBER.
- `symptoms` supports only create / update / batchDelete (no list, no get): duplicates are caught by the
  client-provided id (ALREADY_EXISTS) and the local ledger; list-logs shows symptoms from the audit log.
- food-measurement-unit needs googlehealth.nutrition.readonly (not granted) -> portion stays in the name;
  a serving.foodMeasurementUnit that is not a unit resource is silently dropped (only serving.amount kept).
- a client-provided data point `name` is IGNORED on create: the server assigns a numeric id and returns
  users/<healthUserId>/dataTypes/<type>/dataPoints/<id>. So no name is sent; idempotency = local ledger
  (dedupe key -> returned name, verified with GET where supported) + remote content match.
- batchDelete always answers {"done": true}, also for unknown names; deletion of listable types is
  verified with GET (404).

Safety model: validate everything locally first (a batch with one bad entry sends nothing), dry-run by
default, idempotent via a local dedupe key/ledger plus a remote content match,
and every write attempt is appended to a private audit log (no tokens, no headers).
"""
import hashlib
import json
import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config
from .util import ensure_private_dir, parse_rfc3339, read_json, redact

KINDS = {
    "meal": ("nutrition-log", "nutritionLog", config.NUTRITION_WRITE_SCOPE),
    "drink": ("hydration-log", "hydrationLog", config.NUTRITION_WRITE_SCOPE),
    "symptom": ("symptoms", "symptoms", config.SYMPTOMS_WRITE_SCOPE),
}
TYPE_TO_KIND = {v[0]: k for k, v in KINDS.items()}
MAX_FUTURE_S = 120          # clock skew tolerance
MAX_AGE_DAYS = 400          # older entries are rejected as implausible
MAX_BATCH = 100

MEAL_TYPES = ["BEFORE_BREAKFAST", "BREAKFAST", "BEFORE_LUNCH", "LUNCH", "BEFORE_DINNER", "DINNER",
              "AFTER_DINNER", "SNACK", "ANYTIME"]
# food portion units: canonical -> (max plausible portion, display name candidates in food-measurement-unit)
FOOD_UNITS = {
    "g": (3000, ["gram", "g"]), "kg": (3, ["kilogram", "kg"]), "ml": (3000, ["milliliter", "millilitre", "ml"]),
    "l": (3, ["liter", "litre", "l"]), "piece": (30, ["piece"]), "serving": (10, ["serving"]),
    "cup": (10, ["cup"]), "tbsp": (30, ["tablespoon", "tbsp"]), "tsp": (50, ["teaspoon", "tsp"]),
    "slice": (20, ["slice"]), "oz": (100, ["ounce", "oz"]),
}
FOOD_UNIT_ALIASES = {"gram": "g", "grams": "g", "kilogram": "kg", "milliliter": "ml", "millilitre": "ml",
                     "liter": "l", "litre": "l", "pieces": "piece", "pc": "piece", "stk": "piece", "servings": "serving",
                     "cups": "cup", "tablespoon": "tbsp", "teaspoon": "tsp", "slices": "slice", "ounce": "oz"}
# drink units -> (ml per unit, VolumeUnit enum)
DRINK_UNITS = {
    "ml": (1.0, "MILLILITER"), "l": (1000.0, "LITER"), "cup": (236.5882365, "CUP_US"),
    "cup_imperial": (284.130625, "CUP_IMPERIAL"), "fl_oz": (29.5735295625, "FLUID_OUNCE_US"),
    "fl_oz_imperial": (28.4130625, "FLUID_OUNCE_IMPERIAL"), "pint": (473.176473, "PINT_US"),
    "pint_imperial": (568.26125, "PINT_IMPERIAL"),
}
DRINK_UNIT_ALIASES = {"milliliter": "ml", "millilitre": "ml", "liter": "l", "litre": "l", "cups": "cup",
                      "floz": "fl_oz", "fl oz": "fl_oz", "oz": "fl_oz"}
MAX_DRINK_ML = 3000
# optional macros: json field -> (max, how it is sent)
MACROS = {
    "carbs_g": (1000, ("totalCarbohydrate", None)), "fat_g": (500, ("totalFat", None)),
    "protein_g": (500, ("nutrient", "PROTEIN")), "fiber_g": (200, ("nutrient", "DIETARY_FIBER")),
    "sugar_g": (500, ("nutrient", "SUGAR")), "saturated_fat_g": (300, ("nutrient", "SATURATED_FAT")),
    "sodium_mg": (20000, ("nutrient_mg", "SODIUM")), "caffeine_mg": (1500, ("nutrient_mg", "CAFFEINE")),
}
MAX_KCAL = 5000
SYMPTOMS = ['CRAMPS', 'HEADACHE', 'TENDER_BREASTS', 'ACNE', 'SICK', 'BLOATED', 'HOT_FLASHES', 'PMS', 'COUGH',
            'FEVER', 'DIFFICULTY_BREATHING', 'BACK_PAIN', 'SHAKINESS', 'HUNGER', 'SWEATING', 'ANXIETY', 'THIRST',
            'FREQUENT_URINATION', 'BLURRED_VISION', 'OTHER', 'SEX_DRIVE_HIGH', 'SEX_DRIVE_MEDIUM', 'SEX_DRIVE_LOW',
            'HEART_PALPITATIONS', 'FAINTING', 'CHEST_PAIN', 'FATIGUE', 'CONFUSION', 'DIZZINESS', 'ABDOMINAL_PAIN',
            'BLADDER_LEAKS', 'BLEEDING_GUMS', 'BRAIN_FOG', 'BURNING_MOUTH', 'CONSTIPATION', 'CRAVINGS',
            'DECREASED_APPETITE', 'DIARRHEA', 'DRAWING_PAIN', 'DRY_EYES', 'DRY_HAIR', 'DRY_SKIN', 'EXHAUSTION',
            'FEEL_GOOD', 'FOOD_AVERSIONS', 'HAIR_LOSS', 'HEARTBURN', 'HYPERPIGMENTATION', 'INCREASED_APPETITE',
            'INCREASED_APPETITE_V2', 'INSOMNIA', 'JOINT_PAIN', 'LEG_CRAMPS', 'MILKY_NIPPLE_DISCHARGE', 'NAUSEA',
            'NIGHT_SWEATS', 'NORMAL_DIGESTION', 'NORMAL_STOOL', 'PERINEUM_PAIN', 'SLEEPINESS', 'STRETCH_MARKS',
            'SWELLING', 'VAGINAL_DRYNESS', 'VAGINAL_ITCHING', 'VOMITING']
CLIENT_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,100}")
POINT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
NAME_RE = re.compile(r"users/(?:me|[0-9]{1,30})/dataTypes/(nutrition-log|hydration-log|symptoms)/dataPoints/([A-Za-z0-9_-]{1,128})")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")



def audit_dir():
    return Path(os.environ.get("HSYNC_AUDIT_DIR") or (config.APP_DIR / "audit"))


def food_units_file():
    return Path(os.environ.get("HSYNC_FOOD_UNITS") or (config.APP_DIR / "food_units.json"))


class ValidationError(ValueError):
    pass


# ------------------------------------------------------------------ input
def load_entries(text):
    """JSON object or array of objects -> list of dicts."""
    try:
        data = json.loads(text, parse_constant=lambda c: (_ for _ in ()).throw(ValidationError(f"{c} not allowed")))
    except json.JSONDecodeError as e:
        raise ValidationError(f"input is not valid JSON ({e.msg} at line {e.lineno} col {e.colno})") from None
    items = data if isinstance(data, list) else [data]
    if not items:
        raise ValidationError("no entries in input")
    if len(items) > MAX_BATCH:
        raise ValidationError(f"too many entries ({len(items)} > {MAX_BATCH})")
    if not all(isinstance(x, dict) for x in items):
        raise ValidationError("every entry must be a JSON object")
    return items


def _num(entry, key, lo, hi, required=False, allow_zero=True):
    v = entry.get(key)
    if v is None:
        if required:
            raise ValidationError(f"'{key}' is required")
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValidationError(f"'{key}' must be a number")
    if v < lo or v > hi or (not allow_zero and v == 0):
        raise ValidationError(f"'{key}'={v} is outside the plausible range {lo}..{hi}")
    return v


def _text(entry, key, maxlen, required=False):
    v = entry.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise ValidationError(f"'{key}' is required")
        return None
    if not isinstance(v, str):
        raise ValidationError(f"'{key}' must be a string")
    v = " ".join(v.split())
    if len(v) > maxlen or _CTRL.search(v):
        raise ValidationError(f"'{key}' is too long or contains control characters")
    return v


def parse_time(value, tz, now=None, key="time"):
    """ISO 8601 with offset, or naive local time in `tz`. Rejects future, implausibly old,
    non-existent (spring-forward gap) and ambiguous (fall-back hour) naive times."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"'{key}' is required (ISO 8601, e.g. 2026-10-21T12:30 or 2026-10-21T12:30+02:00)")
    s = value.strip()
    try:
        if re.search(r"(Z|[+-]\d\d:?\d\d)$", s, re.I):
            dt = parse_rfc3339(s)
        else:
            naive = datetime.fromisoformat(s)
            if naive.tzinfo is not None:
                raise ValueError
            z = ZoneInfo(tz)
            a, b = naive.replace(tzinfo=z, fold=0), naive.replace(tzinfo=z, fold=1)
            if a.astimezone(timezone.utc).astimezone(z).replace(tzinfo=None) != naive:
                raise ValidationError(f"'{key}'={s} does not exist in {tz} (DST gap)")
            if a.utcoffset() != b.utcoffset():
                raise ValidationError(f"'{key}'={s} is ambiguous in {tz} (DST change); add an offset like +02:00 or +01:00")
            dt = a
    except ValidationError:
        raise
    except (TypeError, ValueError):
        raise ValidationError(f"'{key}'={s!r} is not an ISO 8601 date-time") from None
    if "T" not in s and " " not in s:
        raise ValidationError(f"'{key}' needs a time of day, not just a date")
    now = now or datetime.now(timezone.utc)
    if dt > now + timedelta(seconds=MAX_FUTURE_S):
        raise ValidationError(f"'{key}'={s} is in the future")
    if dt < now - timedelta(days=MAX_AGE_DAYS):
        raise ValidationError(f"'{key}'={s} is more than {MAX_AGE_DAYS} days ago")
    return dt


def _z(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _offset(dt):
    return f"{int(dt.utcoffset().total_seconds())}s"


def _interval(start, end):
    return {"startTime": _z(start), "startUtcOffset": _offset(start), "endTime": _z(end), "endUtcOffset": _offset(end)}


def _end(entry, start, now, max_min):
    """End of the interval: end_time, or time + duration_min, else time + 1 s (point-like log,
    as in Google's hydration example). Deterministic, so dry-run == what --confirm sends."""
    if entry.get("end_time") is not None and entry.get("duration_min") is not None:
        raise ValidationError("give either 'end_time' or 'duration_min', not both")
    if entry.get("end_time") is not None:
        end = parse_time(entry["end_time"], config.TIMEZONE, now, key="end_time")
    elif entry.get("duration_min") is not None:
        end = start + timedelta(minutes=_num(entry, "duration_min", 0, max_min, allow_zero=False))
        end = end.astimezone(start.tzinfo)
        if end > now + timedelta(seconds=MAX_FUTURE_S):
            raise ValidationError("'time' + 'duration_min' ends in the future")
    else:
        end = start + timedelta(seconds=1)
    if end <= start:
        raise ValidationError("'end_time' must be after 'time'")
    if (end - start) > timedelta(minutes=max_min):
        raise ValidationError(f"interval longer than {max_min} minutes")
    return end


# ------------------------------------------------------------------ food measurement units (cached, read-only)
def load_unit_map(path=None):
    """{canonical unit: food-measurement-unit resource name} from the local cache (see `hsync food-units`)."""
    m = read_json(path or food_units_file(), {}) or {}
    units = m.get("units") if isinstance(m, dict) else None
    out = {}
    for u in units if isinstance(units, list) else []:
        if not isinstance(u, dict) or not isinstance(u.get("name"), str):
            continue
        dn = str(u.get("displayName") or "").strip().lower()
        for canon, (_, cands) in FOOD_UNITS.items():
            if dn in cands and canon not in out:
                out[canon] = u["name"]
    return out


def refresh_unit_cache(api, path=None):
    units = []
    for dp in api.list_points("food-measurement-unit", page_size=1000):
        fmu = dp.get("foodMeasurementUnit") or {}
        if dp.get("name"):
            units.append({"name": dp["name"], "displayName": fmu.get("displayName"),
                          "pluralDisplayName": fmu.get("pluralDisplayName")})
    from .util import write_json_atomic
    write_json_atomic(path or food_units_file(), {"fetched": datetime.now(timezone.utc).isoformat(), "units": units},
                      mode=0o600)
    return units


# ------------------------------------------------------------------ builders
def _canon_unit(u, table, aliases):
    if not isinstance(u, str):
        raise ValidationError("'unit' is required")
    k = u.strip().lower()
    k = aliases.get(k, k)
    if k not in table:
        raise ValidationError(f"unit {u!r} not supported; use one of: {', '.join(table)}")
    return k


def build_meal(e, now=None, unit_map=None):
    now = now or datetime.now(timezone.utc)
    start = parse_time(e.get("time"), config.TIMEZONE, now)
    mt = e.get("meal_type")
    mt_u = mt.strip().upper().replace(" ", "_").replace("-", "_") if isinstance(mt, str) else None
    if mt_u not in MEAL_TYPES:
        raise ValidationError(f"'meal_type' must be one of: {', '.join(m.lower() for m in MEAL_TYPES)}")
    name = _text(e, "food_name", 200, required=True)
    unit = _canon_unit(e.get("unit"), FOOD_UNITS, FOOD_UNIT_ALIASES)
    portion = _num(e, "portion", 0, FOOD_UNITS[unit][0], required=True, allow_zero=False)
    kcal = _num(e, "kcal", 0, MAX_KCAL)
    macros = {k: _num(e, k, 0, hi) for k, (hi, _) in MACROS.items()}
    if macros["sugar_g"] is not None and macros["carbs_g"] is not None and macros["sugar_g"] > macros["carbs_g"] + 0.5:
        raise ValidationError("'sugar_g' cannot exceed 'carbs_g'")
    if macros["saturated_fat_g"] is not None and macros["fat_g"] is not None and macros["saturated_fat_g"] > macros["fat_g"] + 0.5:
        raise ValidationError("'saturated_fat_g' cannot exceed 'fat_g'")
    if kcal is not None:
        macro_kcal = 4 * (macros["carbs_g"] or 0) + 4 * (macros["protein_g"] or 0) + 9 * (macros["fat_g"] or 0)
        if macro_kcal > kcal * 1.3 + 30:
            raise ValidationError(f"macros add up to ~{macro_kcal:.0f} kcal but 'kcal' is {kcal}")
    if unit in ("g", "kg") and (macros["carbs_g"] or 0) + (macros["protein_g"] or 0) + (macros["fat_g"] or 0) \
            > portion * (1000 if unit == "kg" else 1) + 0.5:
        raise ValidationError("macros weigh more than the portion")
    _known(e, {"time", "end_time", "duration_min", "meal_type", "food_name", "portion", "unit", "kcal", "client_id",
               "note", *MACROS})
    end = _end(e, start, now, 240)
    resource = (unit_map or {}).get(unit)
    log = {"interval": _interval(start, end),
           "foodDisplayName": name if resource else f"{name} ({_fmtnum(portion)} {unit})",
           "mealType": mt_u}
    if resource:
        log["serving"] = {"amount": portion, "foodMeasurementUnit": resource}
    if kcal is not None:
        log["energy"] = {"kcal": kcal}
    nutrients = []
    for k, (_, (kind, field)) in MACROS.items():
        v = macros[k]
        if v is None:
            continue
        if kind == "totalCarbohydrate" or kind == "totalFat":
            log[kind] = {"grams": v}
        elif kind == "nutrient":
            nutrients.append({"nutrient": field, "quantity": {"grams": v}})
        else:  # mg -> g (all NutrientQuantity values are grams)
            nutrients.append({"nutrient": field, "quantity": {"grams": round(v / 1000, 6)}})
    if nutrients:
        log["nutrients"] = nutrients
    return _finish("meal", e, log, start)


def build_drink(e, now=None, unit_map=None):
    now = now or datetime.now(timezone.utc)
    start = parse_time(e.get("time"), config.TIMEZONE, now)
    unit = _canon_unit(e.get("unit"), DRINK_UNITS, DRINK_UNIT_ALIASES)
    per, enum = DRINK_UNITS[unit]
    amount = _num(e, "amount", 0, MAX_DRINK_ML / per, required=True, allow_zero=False)
    ml = round(amount * per, 1)
    if ml < 1:
        raise ValidationError("drink amount below 1 ml")
    _text(e, "drink", 100)  # local only (HydrationLog has no name field)
    _known(e, {"time", "end_time", "duration_min", "amount", "unit", "drink", "client_id", "note"})
    end = _end(e, start, now, 240)
    log = {"interval": _interval(start, end), "amountConsumed": {"milliliters": ml, "userProvidedUnit": enum}}
    return _finish("drink", e, log, start)


SYMPTOM_ALIASES = {"tired": "FATIGUE", "itchy skin": "OTHER", "itching": "OTHER", "eczema": "OTHER",
                   "rash": "OTHER", "dry skin": "DRY_SKIN", "bloating": "BLOATED", "stomach ache": "ABDOMINAL_PAIN"}


def build_symptom(e, now=None, unit_map=None):
    now = now or datetime.now(timezone.utc)
    t = parse_time(e.get("time"), config.TIMEZONE, now)
    raw = e.get("symptoms")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw or len(raw) > 20:
        raise ValidationError("'symptoms' must be a list of 1..20 symptom names")
    out = []
    for s in raw:
        if not isinstance(s, str):
            raise ValidationError("symptom names must be strings")
        k = s.strip().lower()
        v = SYMPTOM_ALIASES.get(k) or k.upper().replace(" ", "_").replace("-", "_")
        if v not in SYMPTOMS:
            raise ValidationError(f"unknown symptom {s!r} (Google Health enum; e.g. DRY_SKIN, OTHER, FATIGUE)")
        if v not in out:
            out.append(v)
    _text(e, "note", 500)
    _known(e, {"time", "symptoms", "client_id", "note"})
    log = {"sampleTime": {"physicalTime": _z(t), "utcOffset": _offset(t)}, "symptoms": out}
    return _finish("symptom", e, log, t)


BUILDERS = {"meal": build_meal, "drink": build_drink, "symptom": build_symptom}


def _known(e, allowed):
    extra = sorted(set(e) - set(allowed))
    if extra:
        raise ValidationError(f"unknown field(s): {', '.join(extra)}")


def _fmtnum(v):
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def _finish(kind, e, payload, when):
    data_type, field, scope = KINDS[kind]
    cid = e.get("client_id")
    if cid is not None and (not isinstance(cid, str) or not CLIENT_ID_RE.fullmatch(cid)):
        raise ValidationError("'client_id' must be 1..100 chars of A-Z a-z 0-9 . _ : -")
    note = _text(e, "note", 500)
    if cid:
        material = f"{kind}|client_id|{cid}"
    else:
        material = f"{kind}|" + json.dumps(payload, sort_keys=True, separators=(",", ":"))
    key = hashlib.sha256(material.encode()).hexdigest()
    body = {field: payload}  # no client-provided name: the API ignores it (verified live)
    return {"kind": kind, "data_type": data_type, "scope": scope, "dedupe_key": key, "name": None,
            "body": body, "when": when, "note": note, "client_id": cid}


# ------------------------------------------------------------------ audit log / ledger
def audit_path():
    return audit_dir() / "writes.jsonl"


def audit(record):
    ensure_private_dir(audit_dir())
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record}
    line = redact(json.dumps(rec, ensure_ascii=False, separators=(",", ":"), default=str)) + "\n"
    fd = os.open(audit_path(), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, line.encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def ledger():
    """dedupe_key -> last created record; names deleted later are dropped."""
    out, deleted = {}, set()
    try:
        with open(audit_path(), encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return {}
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("action") == "create" and r.get("status") in ("created", "duplicate") and r.get("dedupe_key"):
            prev = out.get(r["dedupe_key"])
            if r["status"] == "created" or prev is None or prev.get("name") in deleted:
                out[r["dedupe_key"]] = r  # a later "duplicate" never hides the original "created" record
        elif r.get("action") == "delete" and r.get("status") == "deleted":
            deleted.update(r.get("names") or [])
    return {k: v for k, v in out.items() if v.get("name") not in deleted}


# ------------------------------------------------------------------ remote duplicate check
LISTABLE = {"nutrition-log", "hydration-log"}  # symptoms: no list/get on the API


def _civil(dt):
    """Local wall-clock literal for civil_* filters (the entry's own UTC offset = what the API stores)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def remote_filter(data_type, when):
    """Window of +-1 min around the entry's start, in civil time (only filter the API accepts)."""
    f = data_type.replace("-", "_")
    a, b = _civil(when - timedelta(minutes=1)), _civil(when + timedelta(minutes=1))
    return f'{f}.interval.civil_start_time >= "{a}" AND {f}.interval.civil_start_time < "{b}"'


def _remote_match(api, entry):
    """Look for an existing point at the same time with the same content (covers entries written
    before the client id existed, or if the server assigned its own id)."""
    dt, field = entry["data_type"], KINDS[entry["kind"]][1]
    if dt not in LISTABLE:
        return None
    flt = remote_filter(dt, entry["when"])
    want = entry["body"][field]
    for dp in api.list_points(dt, flt, page_size=100):
        got = dp.get(field) or {}
        if _same(entry["kind"], want, got):
            return dp.get("name") or "(unnamed)"
    return None


def _same(kind, want, got):
    if kind == "symptom":
        return (sorted(got.get("symptoms") or []) == sorted(want["symptoms"])
                and (got.get("sampleTime") or {}).get("physicalTime", "")[:19] == want["sampleTime"]["physicalTime"][:19])
    s_w, s_g = want["interval"]["startTime"][:19], (got.get("interval") or {}).get("startTime", "")[:19]
    if s_w != s_g:
        return False
    if kind == "drink":
        return abs(float((got.get("amountConsumed") or {}).get("milliliters") or -1) - want["amountConsumed"]["milliliters"]) < 0.6
    return (got.get("foodDisplayName") == want["foodDisplayName"] and got.get("mealType") == want["mealType"])


# ------------------------------------------------------------------ write / delete
def write(api, entry):
    """Create one validated entry. Returns (status, name): created | duplicate."""
    led = ledger().get(entry["dedupe_key"])
    base = {"action": "create", "kind": entry["kind"], "data_type": entry["data_type"],
            "dedupe_key": entry["dedupe_key"], "client_id": entry["client_id"], "note": entry["note"],
            "request": entry["body"]}
    from .api import ApiError
    try:
        return _write(api, entry, led, base)
    except ApiError as e:
        if not getattr(e, "_audited", False):
            audit({**base, "status": "error", "http_status": e.status, "error": str(e)[:300]})
        raise
    except Exception as e:
        if not getattr(e, "_audited", False):
            audit({**base, "status": "error", "error": f"{type(e).__name__}: {e}"[:300]})
        raise


def _write(api, entry, led, base):
    from .api import ApiError
    if led and led.get("name") and entry["data_type"] not in LISTABLE:
        audit({**base, "status": "duplicate", "name": led["name"], "reason": "local ledger"})
        return "duplicate", led["name"]
    if led and led.get("name"):
        try:
            api.get_point(led["name"])
            audit({**base, "status": "duplicate", "name": led["name"], "reason": "local ledger"})
            return "duplicate", led["name"]
        except ApiError as e:
            if e.status != 404:
                raise
            # deleted in the app meanwhile: fall through and create it again
    existing = _remote_match(api, entry)
    if existing:
        audit({**base, "status": "duplicate", "name": existing, "reason": "already in Google Health"})
        return "duplicate", existing
    try:
        op = api.create_point(entry["data_type"], entry["body"])
    except ApiError as e:
        if e.status == 409:  # ALREADY_EXISTS (defensive; ids are server-assigned)
            audit({**base, "status": "duplicate", "name": None, "reason": "ALREADY_EXISTS"})
            return "duplicate", None
        raise
    name = (op.get("response") or {}).get("name") if isinstance(op, dict) else None
    if not name:
        audit({**base, "status": "error", "error": "create returned no data point name", "operation": op.get("name")})
        raise RuntimeError("create returned no data point name (operation not done?)")
    audit({**base, "status": "created", "name": name, "operation_done": op.get("done"),
           "operation": op.get("name")})
    return "created", name


def parse_name(s):
    """Full resource name or '<data-type>/<id>' -> (data_type, full name). Strict: no path tricks."""
    if not isinstance(s, str):
        raise ValidationError("bad id")
    s = s.strip()
    m = NAME_RE.fullmatch(s)
    if m:
        return m.group(1), s  # as returned by the API (users/me/... or users/<healthUserId>/...)
    parts = s.split("/")
    if len(parts) == 2 and parts[0] in TYPE_TO_KIND and POINT_ID_RE.fullmatch(parts[1]):
        return parts[0], f"users/{config.USER}/dataTypes/{parts[0]}/dataPoints/{parts[1]}"
    raise ValidationError(f"not a log id: {s!r} (use users/me/dataTypes/<nutrition-log|hydration-log|symptoms>/"
                          "dataPoints/<id> or <data-type>/<id>; see hsync list-logs)")


def delete(api, data_type, names):
    base = {"action": "delete", "data_type": data_type, "names": names}
    try:
        op = api.batch_delete(data_type, names)
    except Exception as e:
        audit({**base, "status": "error", "error": f"{type(e).__name__}: {e}"[:300]})
        raise
    audit({**base, "status": "deleted", "operation_done": op.get("done") if isinstance(op, dict) else None})
    return op


# ------------------------------------------------------------------ list
def day_filter(data_type, day):
    a, b = day.isoformat(), (day + timedelta(days=1)).isoformat()
    f = data_type.replace("-", "_")
    return f'{f}.interval.civil_start_time >= "{a}" AND {f}.interval.civil_start_time < "{b}"'


def _own_names():
    return {r.get("name") for r in ledger().values() if r.get("name")}


def summarize(data_type, dp, tz, own=None):
    kind = TYPE_TO_KIND[data_type]
    p = dp.get(KINDS[kind][1]) or {}
    if kind == "symptom":
        t = parse_rfc3339((p.get("sampleTime") or {}).get("physicalTime"))
        what = ", ".join(p.get("symptoms") or [])
    else:
        t = parse_rfc3339((p.get("interval") or {}).get("startTime"))
        if kind == "meal":
            bits = [p.get("foodDisplayName") or "?", (p.get("mealType") or "").lower()]
            if (p.get("energy") or {}).get("kcal") is not None:
                bits.append(f"{p['energy']['kcal']} kcal")
            what = " | ".join(b for b in bits if b)
        else:
            what = f"{(p.get('amountConsumed') or {}).get('milliliters')} ml"
    name = dp.get("name") or ""
    return {"kind": kind, "time": t.astimezone(ZoneInfo(tz)).isoformat(timespec="minutes") if t else None,
            "what": what, "name": name, "by_health_sync": name in (own if own is not None else _own_names()),
            "source": (dp.get("dataSource") or {}).get("platform")}


def _ledger_rows(data_type, day, tz):
    """Entries health-sync created for a type the API can't list (symptoms), from the audit log."""
    rows = []
    for r in ledger().values():
        if r.get("data_type") != data_type or r.get("status") != "created":
            continue
        dp = dict(r.get("request") or {}, name=r.get("name"))
        row = summarize(data_type, dp, tz, own={dp["name"]})
        if row["time"] and row["time"][:10] == day.isoformat():
            row["source"] = "local audit log (API cannot list this type)"
            rows.append(row)
    return rows


def list_day(api, day, kinds, tz):
    rows, errors = [], {}
    for kind in kinds:
        dt = KINDS[kind][0]
        if dt not in LISTABLE:
            rows.extend(_ledger_rows(dt, day, tz))
            continue
        try:
            own = _own_names()
            for dp in api.list_points(dt, day_filter(dt, day), page_size=1000):
                rows.append(summarize(dt, dp, tz, own))
        except Exception as e:  # e.g. missing scope for one type: still show the others
            errors[kind] = redact(str(e))[:200]
    rows.sort(key=lambda r: (r["time"] or "", r["kind"]))
    return rows, errors


def validate_day(s):
    try:
        if len(s) != 10:
            raise ValueError
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        raise ValidationError(f"not a YYYY-MM-DD date: {s!r}") from None
