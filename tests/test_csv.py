import csv
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# an existing CSV with extra columns maintained by another tool (other_*), which must never be touched
HEADER = ("date,other_steps,steps_best,kcal_total,other_kcal,kcal_active,other_minutes,sedentary_period_min,rhr,hrv_ms,"
          "sleep_asleep_min,sleep_in_bed_min,other_score,bedtime,waketime")


def doc(day, complete=True, steps=5000):
    return {"date": day, "steps": steps, "calories_total": 2100.04, "calories_active": 500.0, "sedentary_minutes": 600,
            "resting_heart_rate": 64, "hrv": {"rmssd_ms": 28.3}, "sleep": {"duration_min": 400, "time_in_bed_min": 440,
            "bedtime": "2026-10-15T22:31+02:00", "wake_time": "2026-10-16T06:01+02:00"}, "workouts": [],
            "spo2": None, "skin_temperature": None, "breathing_rate": None, "meta": {"day_complete": complete}}


def run(tmp, *extra):
    return subprocess.run([sys.executable, str(ROOT / "update_daily_summary.py"), "--csv", str(tmp / "s.csv"),
                           "--daily-dir", str(tmp / "daily"), *extra], capture_output=True, text=True, check=True)


def test_fill_only_empty_append_and_refresh(tmp_path):
    (tmp_path / "s.csv").write_text(HEADER + "\n2026-10-15,1000.0,2000.0,1500.0,900,300.0,80.0,500.0,60.0,30.0,"
                                    "360.0,420.0,80.0,22:00,06:00\n"
                                    "2026-10-16,,,,,,,,,,,,,,\n")
    d = tmp_path / "daily"
    d.mkdir()
    for day, comp in (("2026-10-15", True), ("2026-10-16", True), ("2026-10-17", True), ("2026-10-21", False)):
        (d / f"{day}.json").write_text(json.dumps(doc(day, comp)))
    run(tmp_path)
    rows = {r["date"]: r for r in csv.DictReader(open(tmp_path / "s.csv"))}
    assert rows["2026-10-15"]["steps_best"] == "2000.0" and rows["2026-10-15"]["bedtime"] == "22:00"  # foreign values kept
    assert rows["2026-10-16"]["steps_best"] == "5000.0" and rows["2026-10-16"]["kcal_total"] == "2100.0"
    assert rows["2026-10-16"]["bedtime"] == "22:31" and rows["2026-10-16"]["other_steps"] == ""
    assert "2026-10-17" in rows and "2026-10-21" not in rows  # incomplete day skipped
    assert open(tmp_path / "s.csv").readline().strip() == HEADER  # schema unchanged
    # later run with late-synced data refreshes only cells this script owns
    (d / "2026-10-16.json").write_text(json.dumps(doc("2026-10-16", True, steps=7777)))
    run(tmp_path)
    rows = {r["date"]: r for r in csv.DictReader(open(tmp_path / "s.csv"))}
    assert rows["2026-10-16"]["steps_best"] == "7777.0" and rows["2026-10-15"]["steps_best"] == "2000.0"
    assert (tmp_path / "s.csv.bak-api").exists()


def test_missing_csv_is_created_with_mapped_columns(tmp_path):
    d = tmp_path / "daily"
    d.mkdir()
    (d / "2026-10-15.json").write_text(json.dumps(doc("2026-10-15")))
    assert "would create" in run(tmp_path, "--dry-run").stdout and not (tmp_path / "s.csv").exists()
    run(tmp_path)
    rows = list(csv.DictReader(open(tmp_path / "s.csv")))
    assert open(tmp_path / "s.csv").readline().strip().split(",")[:3] == ["date", "steps_best", "kcal_total"]
    assert len(rows) == 1 and rows[0]["date"] == "2026-10-15" and rows[0]["steps_best"] == "5000.0"
    assert rows[0]["bedtime"] == "22:31" and rows[0]["waketime"] == "06:01"
