#!/usr/bin/env python3
"""Optional: keep a one-row-per-day CSV (daily_summary.csv) filled from the daily JSON files.

Rules:
- If the CSV does not exist yet, it is created with the columns: date + the columns in MAP below.
- An existing CSV may have other columns (e.g. filled by another tool or by hand). Those are never
  touched, and neither is any non-empty cell this script did not fill itself. Only empty cells are filled,
  plus cells this script filled before (tracked in daily_summary.api_cells.json), which it refreshes.
- New dates are appended as rows (columns not in MAP stay empty). Rows stay sorted by date.
- Only complete days (meta.day_complete) are used. The column set/order of an existing CSV is unchanged.
- Writes atomically (CSV, backup and tracking file) and keeps daily_summary.csv.bak-api (one rolling backup).
- Unreadable/corrupt daily files are skipped with a warning; a file whose "date" does not match its
  name is ignored. Concurrent runs are serialized with a lock file next to the CSV.
Defaults: --csv = HSYNC_SUMMARY_CSV (else daily_summary.csv next to the output dir), --daily-dir = HSYNC_OUT_DIR.
If another tool rewrites the CSV from scratch, just run this script again afterwards.
"""
import argparse
import csv
import fcntl
import glob
import io
import os
import re
import sys
from datetime import date

from health_sync import config
from health_sync.util import read_json, write_json_atomic, write_text_atomic

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

MAP = {  # csv column -> getter on the daily JSON
    "steps_best": lambda d: d.get("steps"),
    "kcal_total": lambda d: d.get("calories_total"),
    "kcal_active": lambda d: d.get("calories_active"),
    "sedentary_period_min": lambda d: d.get("sedentary_minutes"),
    "rhr": lambda d: d.get("resting_heart_rate"),
    "hrv_ms": lambda d: (d.get("hrv") or {}).get("rmssd_ms"),
    "sleep_asleep_min": lambda d: (d.get("sleep") or {}).get("duration_min"),
    "sleep_in_bed_min": lambda d: (d.get("sleep") or {}).get("time_in_bed_min"),
    "sleep_total_asleep_min": lambda d: (d.get("sleep") or {}).get("total_asleep_min_all_sessions"),
    "bedtime": lambda d: _hhmm((d.get("sleep") or {}).get("bedtime")),
    "waketime": lambda d: _hhmm((d.get("sleep") or {}).get("wake_time")),
}


def _hhmm(iso):
    return iso[11:16] if iso and len(iso) >= 16 else None


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return repr(round(float(v), 1))  # pandas-style float output (e.g. 3593.0), stable across runs


def _valid_day(s):
    if not isinstance(s, str) or not DATE_RE.fullmatch(s):
        return False
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(config.SUMMARY_CSV), help="CSV to fill (default: %(default)s)")
    ap.add_argument("--daily-dir", default=str(config.OUT_DIR), help="daily JSON dir (default: %(default)s)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    csv_dir = os.path.dirname(os.path.abspath(a.csv))
    if not os.path.isdir(csv_dir):
        print(f"{csv_dir} does not exist; nothing to update", file=sys.stderr)
        return 1
    lock_path = os.path.splitext(a.csv)[0] + ".lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return _update(a)
    finally:
        os.close(fd)


def _update(a):
    if not os.path.exists(a.csv):
        if a.dry_run:
            print(f"{a.csv} does not exist; would create it with columns: date, {', '.join(MAP)}")
            return 0
        write_text_atomic(a.csv, ",".join(["date", *MAP]) + "\n", mode=0o644, suffix=".csv")
        print(f"created {a.csv}")
    track_path = os.path.splitext(a.csv)[0] + ".api_cells.json"
    tracked = read_json(track_path, {})
    if not isinstance(tracked, dict):
        tracked = {}
    with open(a.csv, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        header, rows = list(r.fieldnames or []), list(r)
    if "date" not in header:
        print(f"{a.csv} has no 'date' column; refusing to touch it", file=sys.stderr)
        return 1
    by_date = {row["date"]: row for row in rows}
    filled = appended = 0
    skipped = 0
    for path in sorted(glob.glob(os.path.join(glob.escape(a.daily_dir), "????-??-??.json"))):
        doc = read_json(path)
        stem = os.path.basename(path)[:-5]
        if not isinstance(doc, dict) or doc.get("date") != stem or not _valid_day(stem):
            print(f"WARN: skipping unreadable or inconsistent {os.path.basename(path)}", file=sys.stderr)
            skipped += 1
            continue
        if not (doc.get("meta") or {}).get("day_complete"):
            continue
        day = stem
        row = by_date.get(day)
        if row is None:
            row = {c: "" for c in header}
            row["date"] = day
            by_date[day] = row
            appended += 1
        mine = set(tracked.get(day, []) if isinstance(tracked.get(day), list) else [])
        for col, get in MAP.items():
            if col not in header:
                continue
            try:
                val = _fmt(get(doc))
            except (TypeError, ValueError, AttributeError):
                val = ""  # malformed value in the daily file: leave the cell alone
            if val == "":
                continue
            if row.get(col, "") == "" or col in mine:
                if row.get(col) != val:
                    filled += 1
                row[col] = val
                mine.add(col)
        if mine:
            tracked[day] = sorted(mine)
    out_rows = [by_date[d] for d in sorted(by_date)]
    print(f"cells filled/updated: {filled}, rows appended: {appended}, total rows: {len(out_rows)}"
          + (f", skipped files: {skipped}" if skipped else ""))
    if a.dry_run or (filled == 0 and appended == 0):
        return 0
    with open(a.csv, newline="", encoding="utf-8") as f:
        original = f.read()
    write_text_atomic(a.csv + ".bak-api", original, mode=0o600, suffix=".csv")
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    w.writerows(out_rows)
    write_text_atomic(a.csv, buf.getvalue(), mode=0o644, suffix=".csv")
    write_json_atomic(track_path, tracked, mode=0o600)
    return 0


if __name__ == "__main__":
    sys.exit(main())
