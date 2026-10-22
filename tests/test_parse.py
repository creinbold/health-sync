from datetime import timezone

from health_sync import parse
from health_sync.util import num, parse_duration_s, parse_rfc3339

import fixtures

TZ = "Europe/Paris"


def test_util_time_and_numbers():
    dt = parse_rfc3339("2026-10-20T03:58:00.123456789Z")
    assert dt.tzinfo is not None and dt.utcoffset().total_seconds() == 0 and dt.microsecond == 123456
    assert parse_rfc3339("2026-10-20T05:58:00+02:00").astimezone(timezone.utc).hour == 3
    assert parse_duration_s("36000s") == 36000 and parse_duration_s("1.5s") == 1.5
    assert parse_duration_s("PT1H30M") == 5400 and parse_duration_s(None) is None
    assert num("8421") == 8421 and num("95.6") == 95.6 and num(None) is None and num("x") is None


def test_camel():
    assert parse.camel("daily-sleep-temperature-derivations") == "dailySleepTemperatureDerivations"
    assert parse.camel("steps") == "steps"


def test_rollups():
    d = fixtures.data()
    assert parse.parse_daily_rollup("steps", d["steps"]) == {"2026-10-19": 5000, "2026-10-20": 6000, "2026-10-21": 1000}
    assert parse.parse_daily_rollup("total-calories", d["total-calories"])["2026-10-19"] == 2100.0
    assert parse.parse_daily_rollup("active-energy-burned", d["active-energy-burned"])["2026-10-20"] == 600.4
    assert parse.parse_daily_rollup("sedentary-period", d["sedentary-period"]) == {"2026-10-19": 601, "2026-10-20": 500}


def test_rollup_window_without_value_is_missing_not_zero():
    pts = [{"civilStartTime": {"date": {"year": 2026, "month": 10, "day": 18}}}]
    assert parse.parse_daily_rollup("steps", pts) == {}


def test_daily_lists():
    d = fixtures.data()
    assert parse.parse_resting_hr(d["daily-resting-heart-rate"]) == {"2026-10-20": 60, "2026-10-19": 61}
    hrv = parse.parse_hrv(d["daily-heart-rate-variability"])["2026-10-20"]
    assert hrv == {"rmssd_ms": 30.0, "deep_sleep_rmssd_ms": 35.5, "entropy": 2.5, "non_rem_heart_rate_bpm": 55}
    assert parse.parse_spo2(d["daily-oxygen-saturation"])["2026-10-20"]["avg_pct"] == 97.0
    assert parse.parse_breathing(d["daily-respiratory-rate"]) == {"2026-10-20": 15.0}
    st = parse.parse_skin_temp(d["daily-sleep-temperature-derivations"])["2026-10-20"]
    assert st["deviation_c"] == -0.25 and st["nightly_c"] == 34.0 and st["baseline_c"] == 34.25


def test_skin_temp_without_baseline_has_no_deviation():
    pts = [{"dailySleepTemperatureDerivations": {"date": {"year": 2026, "month": 10, "day": 1},
                                                 "nightlyTemperatureCelsius": 34.0}}]
    assert parse.parse_skin_temp(pts)["2026-10-01"]["deviation_c"] is None


def test_daily_prefers_fitbit_platform_on_duplicates():
    pts = [{"dataSource": {"platform": "HEALTH_CONNECT"}, "dailyRestingHeartRate": {"date": {"year": 2026, "month": 10, "day": 1}, "beatsPerMinute": "80"}},
           {"dataSource": {"platform": "FITBIT"}, "dailyRestingHeartRate": {"date": {"year": 2026, "month": 10, "day": 1}, "beatsPerMinute": "60"}}]
    assert parse.parse_resting_hr(pts) == {"2026-10-01": 60}


def test_sleep_main_and_stages():
    s = parse.parse_sleep(fixtures.data()["sleep"], TZ)
    assert list(s) == ["2026-10-20"]
    n = s["2026-10-20"]
    assert n["duration_min"] == 420 and n["time_in_bed_min"] == 480
    assert n["bedtime"] == "2026-10-19T23:00+02:00" and n["wake_time"] == "2026-10-20T07:00+02:00"
    assert n["stages"] == {"deep": 60, "light": 240, "rem": 120, "awake": 60}
    assert n["main_sleep"] is True and n["nap"] is False and n["sessions"] == 2 and n["naps"] == 1
    assert n["total_asleep_min_all_sessions"] == 450


def test_sleep_fallbacks_from_segments_and_classic():
    pts = [{"sleep": {"interval": {"startTime": "2026-10-10T21:00:00Z", "endTime": "2026-10-11T05:00:00Z"},
                      "type": "STAGES",
                      "stages": [{"startTime": "2026-10-10T21:00:00Z", "endTime": "2026-10-10T23:00:00Z", "type": "LIGHT"},
                                 {"startTime": "2026-10-10T23:00:00Z", "endTime": "2026-10-11T00:00:00Z", "type": "DEEP"},
                                 {"startTime": "2026-10-11T00:00:00Z", "endTime": "2026-10-11T00:30:00Z", "type": "AWAKE"},
                                 {"startTime": "2026-10-11T00:30:00Z", "endTime": "2026-10-11T05:00:00Z", "type": "REM"}]}},
           {"sleep": {"interval": {"startTime": "2026-10-11T21:00:00Z", "endTime": "2026-10-12T04:00:00Z"},
                      "type": "CLASSIC", "summary": {"minutesAsleep": "400",
                                                     "stagesSummary": [{"type": "ASLEEP", "minutes": "400"}]}}}]
    s = parse.parse_sleep(pts, TZ)
    a = s["2026-10-11"]  # no civilEndTime -> derived from endTime in Europe/Paris
    assert a["duration_min"] == 450 and a["time_in_bed_min"] == 480
    assert a["stages"] == {"deep": 60, "light": 120, "rem": 270, "awake": 30}
    assert s["2026-10-12"]["stages"] is None and s["2026-10-12"]["duration_min"] == 400


def test_sleep_tolerates_guide_example_field_names():
    # The sleep guide example uses sleepType/sleepStages at top level; reference uses type/stages.
    pts = [{"sleep": {"startTime": "2026-04-20T22:30:00Z", "endTime": "2026-04-21T06:30:00Z", "sleepType": "STAGES",
                      "sleepStages": [{"startTime": "2026-04-20T22:30:00Z", "endTime": "2026-04-20T23:45:00Z", "type": "LIGHT"},
                                      {"startTime": "2026-04-20T23:45:00Z", "endTime": "2026-04-21T01:15:00Z", "type": "DEEP"}]}}]
    s = parse.parse_sleep(pts, TZ)["2026-04-21"]
    assert s["stages"]["deep"] == 90 and s["sleep_type"] == "STAGES"


def test_exercise():
    w = parse.parse_exercise(fixtures.data()["exercise"], TZ)["2026-10-20"]
    assert w == [{"type": "WALKING", "name": "Walking", "start": "2026-10-20T18:00+02:00",
                  "end": "2026-10-20T18:40+02:00", "duration_min": 35.0, "elapsed_min": 40.0,
                  "calories": 200.0, "avg_hr": 110, "steps": 4000, "distance_km": 3.0, "active_zone_minutes": 15,
                  "auto_detected": True}]


def test_unknown_shape_yields_nothing():
    assert parse.parse_resting_hr([{"foo": "bar"}]) == {}
    assert parse.parse_sleep([{"name": "x"}], TZ) == {}


def test_real_shape_sleep_date_from_utc_offset_and_omitted_bools():
    # Shape as returned live (2026-10; values synthetic): no civil times, false booleans omitted,
    # nap flagged only via "nap".
    pts = [{"sleep": {"interval": {"startTime": "2026-05-11T20:00:00Z", "startUtcOffset": "7200s",
                                   "endTime": "2026-05-11T21:00:00Z", "endUtcOffset": "7200s"},
                      "type": "STAGES", "metadata": {"stagesStatus": "SUCCEEDED", "processed": True, "nap": True},
                      "summary": {"minutesAsleep": "50", "minutesInSleepPeriod": "60"}}},
           {"sleep": {"interval": {"startTime": "2026-05-11T22:30:00Z", "startUtcOffset": "7200s",
                                   "endTime": "2026-05-12T04:00:00Z", "endUtcOffset": "7200s"},
                      "type": "STAGES", "metadata": {"stagesStatus": "SUCCEEDED", "processed": True, "mainSleep": True},
                      "summary": {"minutesAsleep": "300", "minutesInSleepPeriod": "330"}}}]
    s = parse.parse_sleep(pts, TZ)
    # the evening nap ends 23:00 local on 05-11 -> 05-11; the night ends 06:00 local on 05-12
    assert s["2026-05-11"]["nap"] is True and s["2026-05-11"]["main_sleep"] is False
    assert s["2026-05-12"]["duration_min"] == 300 and s["2026-05-12"]["naps"] == 0


def test_sleep_date_uses_recorded_offset_not_config_tz():
    # recorded while travelling at UTC-4: ends 23:30 local on 10-01, which is 05:30 on 10-02 in Paris
    pts = [{"sleep": {"interval": {"startTime": "2026-10-01T22:00:00Z", "startUtcOffset": "-14400s",
                                   "endTime": "2026-10-02T03:30:00Z", "endUtcOffset": "-14400s"},
                      "metadata": {"mainSleep": True}, "summary": {"minutesAsleep": "300"}}}]
    assert list(parse.parse_sleep(pts, TZ)) == ["2026-10-01"]


def test_real_shape_exercise_fractional_duration_and_manual_start():
    pts = [{"dataSource": {"recordingMethod": "ACTIVELY_MEASURED", "platform": "FITBIT"},
            "exercise": {"interval": {"startTime": "2026-05-03T14:00:05Z", "startUtcOffset": "7200s",
                                      "endTime": "2026-05-03T14:20:00Z", "endUtcOffset": "7200s"},
                         "exerciseType": "TENNIS", "displayName": "Workout", "activeDuration": "1110.123s",
                         "metricsSummary": {"caloriesKcal": 120, "averageHeartRateBeatsPerMinute": "120",
                                            "heartRateZoneDurations": {"lightTime": "600s"}}, "exerciseMetadata": {}}}]
    w = parse.parse_exercise(pts, TZ)["2026-05-03"][0]
    assert w["duration_min"] == 18.5 and w["calories"] == 120 and w["avg_hr"] == 120
    assert w["auto_detected"] is False and w["distance_km"] is None and w["steps"] is None
