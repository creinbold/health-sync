"""Documented-shape SYNTHETIC sample data for 2026-10-19..2026-10-21 (Europe/Paris = UTC+2).

All values are made up (round numbers); never paste real health data here."""
from conftest import civil, gdate

DAYS = ["2026-10-19", "2026-10-20", "2026-10-21"]


def rollup(dt_field, iso, value_obj):
    # real API: civilEndTime is the next day's midnight (exclusive end), "time": {}
    from datetime import date, timedelta
    nxt = (date.fromisoformat(iso) + timedelta(days=1)).isoformat()
    return {"civilStartTime": civil(iso), "civilEndTime": civil(nxt), dt_field: value_obj}


def fitbit_src():
    return {"platform": "FITBIT", "recordingMethod": "PASSIVELY_MEASURED",
            "device": {"displayName": "Fitbit Air", "formFactor": "FITNESS_BAND"}}


def data():
    d = {
        "steps": [rollup("steps", "2026-10-19", {"countSum": "5000"}),
                  rollup("steps", "2026-10-20", {"countSum": "6000"}),
                  rollup("steps", "2026-10-21", {"countSum": "1000"})],
        "total-calories": [rollup("totalCalories", "2026-10-19", {"kcalSum": 2100.04}),
                           rollup("totalCalories", "2026-10-20", {"kcalSum": 2200.5})],
        "active-energy-burned": [rollup("activeEnergyBurned", "2026-10-19", {"kcalSum": 500.0}),
                                 rollup("activeEnergyBurned", "2026-10-20", {"kcalSum": 600.4})],
        "sedentary-period": [rollup("sedentaryPeriod", "2026-10-19", {"durationSum": "36060s"}),
                             rollup("sedentaryPeriod", "2026-10-20", {"durationSum": "30000s"})],
        "daily-resting-heart-rate": [
            {"dataSource": fitbit_src(), "dailyRestingHeartRate": {"date": gdate("2026-10-20"), "beatsPerMinute": "60",
                                                                     "dailyRestingHeartRateMetadata": {"calculationMethod": "WITH_SLEEP"}}},
            {"dataSource": fitbit_src(), "dailyRestingHeartRate": {"date": gdate("2026-10-19"), "beatsPerMinute": "61"}}],
        "daily-heart-rate-variability": [
            {"dataSource": fitbit_src(), "dailyHeartRateVariability": {
                "date": gdate("2026-10-20"), "averageHeartRateVariabilityMilliseconds": 30.04,
                "nonRemHeartRateBeatsPerMinute": "55", "entropy": 2.5,
                "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds": 35.5}}],
        "daily-oxygen-saturation": [
            {"dataSource": fitbit_src(), "dailyOxygenSaturation": {
                "date": gdate("2026-10-20"), "averagePercentage": 97.0, "lowerBoundPercentage": 95.0,
                "upperBoundPercentage": 99.0}}],
        "daily-respiratory-rate": [
            {"dataSource": fitbit_src(), "dailyRespiratoryRate": {"date": gdate("2026-10-20"), "breathsPerMinute": 15.0}}],
        "daily-sleep-temperature-derivations": [
            {"dataSource": fitbit_src(), "dailySleepTemperatureDerivations": {
                "date": gdate("2026-10-20"), "nightlyTemperatureCelsius": 34.0, "baselineTemperatureCelsius": 34.25,
                "relativeNightlyStddev30dCelsius": 0.2}}],
        "sleep": [
            # main night ending 2026-10-20 07:00 local (05:00Z)
            {"name": "users/me/dataTypes/sleep/dataPoints/abcd-1", "dataSource": fitbit_src(), "sleep": {
                "interval": {"startTime": "2026-10-19T21:00:00Z", "startUtcOffset": "7200s",
                             "endTime": "2026-10-20T05:00:00.123456789Z", "endUtcOffset": "7200s"},
                "type": "STAGES",
                "stages": [{"startTime": "2026-10-19T21:00:00Z", "endTime": "2026-10-19T22:00:00Z", "type": "LIGHT",
                            "startUtcOffset": "7200s", "endUtcOffset": "7200s"}],
                "metadata": {"stagesStatus": "SUCCEEDED", "processed": True, "mainSleep": True},
                "summary": {"minutesAsleep": "420", "minutesAwake": "60", "minutesInSleepPeriod": "480",
                            "minutesToFallAsleep": "0", "minutesAfterWakeUp": "0",
                            "stagesSummary": [{"type": "DEEP", "minutes": "60", "count": "4"},
                                              {"type": "LIGHT", "minutes": "240", "count": "30"},
                                              {"type": "REM", "minutes": "120", "count": "6"},
                                              {"type": "AWAKE", "minutes": "60", "count": "20"}]}}},
            # afternoon nap on 2026-10-20 (not main)
            {"name": "users/me/dataTypes/sleep/dataPoints/abcd-2", "sleep": {
                "interval": {"startTime": "2026-10-20T12:00:00Z", "endTime": "2026-10-20T12:40:00Z",
                             "startUtcOffset": "7200s", "endUtcOffset": "7200s"},
                "type": "STAGES",
                "metadata": {"stagesStatus": "SUCCEEDED", "processed": True, "nap": True},
                "summary": {"minutesAsleep": "30", "minutesInSleepPeriod": "40",
                            "stagesSummary": [{"type": "ASLEEP", "minutes": "30"}, {"type": "AWAKE", "minutes": "10"}]}}},
        ],
        "exercise": [
            {"name": "users/me/dataTypes/exercise/dataPoints/ex-1", "dataSource": fitbit_src(), "exercise": {
                "interval": {"startTime": "2026-10-20T16:00:00.800Z", "startUtcOffset": "7200s",
                             "endTime": "2026-10-20T16:40:00Z", "endUtcOffset": "7200s"},
                "exerciseType": "WALKING", "displayName": "Walking", "activeDuration": "2100.000s",
                "metricsSummary": {"caloriesKcal": 200.0, "distanceMillimeters": 3000000.0, "steps": "4000",
                                   "averageHeartRateBeatsPerMinute": "110", "activeZoneMinutes": "15"}}}],
    }
    return d
