"""Pure mapping from documented Google Health API v4 response shapes to per-day values.

Shapes follow the v4 discovery document (revision 20261020, kept in docs/):
  DailyRollupDataPoint{civilStartTime{date}, steps{countSum}, totalCalories{kcalSum},
                       activeEnergyBurned{kcalSum}, sedentaryPeriod{durationSum}}
  DataPoint{dailyRestingHeartRate{date, beatsPerMinute}, dailyHeartRateVariability{date,
            averageHeartRateVariabilityMilliseconds, ...}, dailyOxygenSaturation{date, averagePercentage, ...},
            dailyRespiratoryRate{date, breathsPerMinute}, dailySleepTemperatureDerivations{date,
            nightlyTemperatureCelsius, baselineTemperatureCelsius, ...}, sleep{interval, summary, stages, metadata},
            exercise{interval, exerciseType, displayName, activeDuration, metricsSummary{...}}}
Anything unexpected degrades to None (and a warning), never to a guessed value.
"""
import logging
from collections import defaultdict

from datetime import timezone

from .util import gdate_to_iso, local_iso_minutes, num, offset_seconds, parse_duration_s, parse_rfc3339, rnd, to_local

log = logging.getLogger("hsync.parse")


def camel(data_type):
    """'daily-resting-heart-rate' -> 'dailyRestingHeartRate' (DataPoint union field name)."""
    head, *rest = data_type.split("-")
    return head + "".join(p[:1].upper() + p[1:] for p in rest)


def _payload(dp, data_type):
    key = camel(data_type)
    if isinstance(dp.get(key), dict):
        return dp[key]
    # tolerate a differently-cased/renamed union field: the single dict besides dataSource/name
    cands = [v for k, v in dp.items() if isinstance(v, dict) and k not in ("dataSource",)]
    if len(cands) == 1:
        log.warning("%s: expected field %r, using %r", data_type, key, [k for k in dp if dp[k] is cands[0]][0])
        return cands[0]
    return None


def _platform_rank(dp):
    p = ((dp.get("dataSource") or {}).get("platform") or "")
    return 0 if p == "FITBIT" else 1


# ------------------------------------------------------------------ daily rollups
ROLLUP_FIELDS = {
    # data type: (DailyRollupDataPoint field, value field, converter)
    "steps": ("steps", "countSum", lambda v: None if num(v) is None else int(num(v))),
    "total-calories": ("totalCalories", "kcalSum", lambda v: rnd(num(v), 1)),
    "active-energy-burned": ("activeEnergyBurned", "kcalSum", lambda v: rnd(num(v), 1)),
    "sedentary-period": ("sedentaryPeriod", "durationSum",
                         lambda v: None if parse_duration_s(v) is None else int(round(parse_duration_s(v) / 60))),
}


def parse_daily_rollup(data_type, rollup_points):
    """-> {date_iso: value}. Days without a value object are omitted (=> null)."""
    field, vfield, conv = ROLLUP_FIELDS[data_type]
    out = {}
    for rp in rollup_points or []:
        d = gdate_to_iso((rp.get("civilStartTime") or {}).get("date"))
        if not d:
            log.warning("%s rollup point without civilStartTime.date: keys=%s", data_type, list(rp))
            continue
        val = rp.get(field)
        if not isinstance(val, dict):
            continue
        if vfield not in val:
            if val:
                log.warning("%s rollup: no %s in %s", data_type, vfield, list(val))
            continue
        out[d] = conv(val.get(vfield))
    return out


# ------------------------------------------------------------------ daily (list) types
def _daily_by_date(data_type, points):
    by = defaultdict(list)
    for dp in sorted(points or [], key=_platform_rank):
        p = _payload(dp, data_type)
        if not p:
            continue
        d = gdate_to_iso(p.get("date"))
        if d:
            by[d].append(p)
    for d, lst in by.items():
        if len(lst) > 1:
            log.info("%s: %d points for %s, using first (Fitbit platform preferred)", data_type, len(lst), d)
    return {d: lst[0] for d, lst in by.items()}


def parse_resting_hr(points):
    return {d: rnd(num(p.get("beatsPerMinute")), 1)
            for d, p in _daily_by_date("daily-resting-heart-rate", points).items()
            if num(p.get("beatsPerMinute")) is not None}


def parse_hrv(points):
    out = {}
    for d, p in _daily_by_date("daily-heart-rate-variability", points).items():
        v = {
            "rmssd_ms": rnd(num(p.get("averageHeartRateVariabilityMilliseconds")), 1),
            "deep_sleep_rmssd_ms": rnd(num(p.get("deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds")), 1),
            "entropy": rnd(num(p.get("entropy")), 3),
            "non_rem_heart_rate_bpm": num(p.get("nonRemHeartRateBeatsPerMinute")),
        }
        if any(x is not None for x in v.values()):
            out[d] = v
    return out


def parse_spo2(points):
    out = {}
    for d, p in _daily_by_date("daily-oxygen-saturation", points).items():
        v = {
            "avg_pct": rnd(num(p.get("averagePercentage")), 1),
            "lower_bound_pct": rnd(num(p.get("lowerBoundPercentage")), 1),
            "upper_bound_pct": rnd(num(p.get("upperBoundPercentage")), 1),
            "std_dev_pct": rnd(num(p.get("standardDeviationPercentage")), 2),
        }
        if v["avg_pct"] is not None:
            out[d] = v
    return out


def parse_breathing(points):
    return {d: rnd(num(p.get("breathsPerMinute")), 1)
            for d, p in _daily_by_date("daily-respiratory-rate", points).items()
            if num(p.get("breathsPerMinute")) is not None}


def parse_skin_temp(points):
    out = {}
    for d, p in _daily_by_date("daily-sleep-temperature-derivations", points).items():
        nightly = num(p.get("nightlyTemperatureCelsius"))
        base = num(p.get("baselineTemperatureCelsius"))
        v = {
            # deviation is computed (nightly - 30-day baseline), only when both values are provided
            "deviation_c": rnd(nightly - base, 2) if nightly is not None and base is not None else None,
            "nightly_c": rnd(nightly, 2),
            "baseline_c": rnd(base, 2),
            "relative_stddev_30d_c": rnd(num(p.get("relativeNightlyStddev30dCelsius")), 2),
        }
        if nightly is not None:
            out[d] = v
    return out


# ------------------------------------------------------------------ sleep
STAGE_KEYS = {"DEEP": "deep", "LIGHT": "light", "REM": "rem", "AWAKE": "awake"}


def _interval(obj):
    iv = obj.get("interval") or {}
    s = parse_rfc3339(iv.get("startTime") or obj.get("startTime"))
    e = parse_rfc3339(iv.get("endTime") or obj.get("endTime"))
    return iv, s, e


def _session_date(iv, dt, which, tz):
    """Civil date of a session boundary. Real responses (Oct 2026) omit the output-only
    civilStartTime/civilEndTime, so fall back to the recorded UTC offset (same basis the
    API's civil_* filters use), and only then to the configured timezone."""
    d = gdate_to_iso((iv.get(which) or {}).get("date"))
    if d:
        return d
    if dt is None:
        return None
    off = offset_seconds(iv.get("endUtcOffset" if which == "civilEndTime" else "startUtcOffset"))
    if off is not None:
        return (dt.astimezone(timezone.utc) + off).date().isoformat()
    return to_local(dt, tz).date().isoformat()


def _sleep_record(sl, tz):
    iv, s, e = _interval(sl)
    summ = sl.get("summary") or {}
    stages_list = sl.get("stages") or sl.get("sleepStages") or []  # sleepStages: seen in a guide example
    # stage minutes: prefer API summary, else sum stage segments
    stages = {}
    for st in summ.get("stagesSummary") or []:
        k = STAGE_KEYS.get(st.get("type"))
        if k and num(st.get("minutes")) is not None:
            stages[k] = num(st.get("minutes"))
    if not stages and stages_list:
        acc = defaultdict(float)
        for seg in stages_list:
            k = STAGE_KEYS.get(seg.get("type"))
            a, b = parse_rfc3339(seg.get("startTime")), parse_rfc3339(seg.get("endTime"))
            if k and a and b:
                acc[k] += (b - a).total_seconds() / 60
        stages = {k: int(round(v)) for k, v in acc.items()}
    stype = sl.get("type") or sl.get("sleepType")
    has_stage_detail = any(k in stages for k in ("deep", "light", "rem"))
    asleep = num(summ.get("minutesAsleep"))
    if asleep is None and stages_list:
        tot = 0.0
        for seg in stages_list:
            a, b = parse_rfc3339(seg.get("startTime")), parse_rfc3339(seg.get("endTime"))
            if a and b and seg.get("type") not in ("AWAKE", "RESTLESS"):
                tot += (b - a).total_seconds() / 60
        asleep = int(round(tot)) if tot else None
    in_bed = num(summ.get("minutesInSleepPeriod"))
    if in_bed is None and s and e:
        in_bed = int(round((e - s).total_seconds() / 60))
    md = sl.get("metadata") or {}
    return {
        "_end_date": _session_date(iv, e, "civilEndTime", tz),
        "_start": s,
        "duration_min": asleep,
        "time_in_bed_min": in_bed,
        "bedtime": local_iso_minutes(s, tz),
        "wake_time": local_iso_minutes(e, tz),
        "stages": ({k: stages.get(k) for k in ("deep", "light", "rem", "awake")} if has_stage_detail else None),
        "sleep_type": stype,
        "minutes_to_fall_asleep": num(summ.get("minutesToFallAsleep")),
        # proto3 JSON omits false booleans: real main sleeps carry only mainSleep, naps only nap
        "main_sleep": bool(md.get("mainSleep", False)),
        "nap": bool(md.get("nap", False)),
        "manually_edited": bool(md.get("manuallyEdited", False)),
        "processed": bool(md.get("processed", False)),
    }


def parse_sleep(points, tz):
    """Sleep is attributed to the local date it ENDS on (wake day), like Fitbit's dateOfSleep.
    Main sleep = metadata.mainSleep, else the longest."""
    by = defaultdict(list)
    for dp in points or []:
        sl = _payload(dp, "sleep")
        if not sl:
            continue
        rec = _sleep_record(sl, tz)
        if rec["_end_date"]:
            by[rec["_end_date"]].append(rec)
    out = {}
    for d, recs in by.items():
        mains = [r for r in recs if r.get("main_sleep")]
        pool = mains or [r for r in recs if not r.get("nap")] or recs
        main = max(pool, key=lambda r: (r["duration_min"] or 0, r["time_in_bed_min"] or 0))
        res = {k: v for k, v in main.items() if not k.startswith("_")}
        res["sessions"] = len(recs)
        res["naps"] = sum(1 for r in recs if r.get("nap"))
        tot = [r["duration_min"] for r in recs if r["duration_min"] is not None]
        res["total_asleep_min_all_sessions"] = sum(tot) if tot else None
        out[d] = res
    return out


# ------------------------------------------------------------------ workouts
def parse_exercise(points, tz):
    by = defaultdict(list)
    for dp in points or []:
        ex = _payload(dp, "exercise")
        if not ex:
            continue
        iv, s, e = _interval(ex)
        d = _session_date(iv, s, "civilStartTime", tz)
        if not d:
            continue
        ms = ex.get("metricsSummary") or {}
        act = parse_duration_s(ex.get("activeDuration"))
        if act is None and s and e:
            act = (e - s).total_seconds()
        dist = num(ms.get("distanceMillimeters"))
        by[d].append({
            "_s": s,
            "type": ex.get("exerciseType"),
            "name": ex.get("displayName"),
            "start": local_iso_minutes(s, tz),
            "end": local_iso_minutes(e, tz),
            "duration_min": rnd(act / 60, 1) if act is not None else None,
            "elapsed_min": rnd((e - s).total_seconds() / 60, 1) if s and e else None,
            "calories": rnd(num(ms.get("caloriesKcal")), 1),
            "avg_hr": num(ms.get("averageHeartRateBeatsPerMinute")),
            "steps": num(ms.get("steps")),
            "distance_km": rnd(dist / 1e6, 2) if dist is not None else None,
            "active_zone_minutes": num(ms.get("activeZoneMinutes")),
            # PASSIVELY_MEASURED = auto-detected by the tracker, ACTIVELY_MEASURED = started on device/app
            "auto_detected": {"PASSIVELY_MEASURED": True, "ACTIVELY_MEASURED": False, "MANUAL": False}.get(
                (dp.get("dataSource") or {}).get("recordingMethod")),
        })
    out = {}
    for d, lst in by.items():
        lst.sort(key=lambda w: (w["_s"] is None, w["start"] or ""))
        out[d] = [{k: v for k, v in w.items() if k != "_s"} for w in lst]
    return out
