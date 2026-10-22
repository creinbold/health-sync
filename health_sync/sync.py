"""Orchestration: fetch a date range, build one JSON document per local day, write atomically.

Idempotent: a day file is fully rebuilt from the API each run. If one data group fails
(e.g. transient 5xx, missing scope) the previous value for that group is kept and flagged
in meta.stale_groups instead of being overwritten with null.
"""
import errno
import fcntl
import json
import logging
import os
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, parse
from .api import AccountNotLinked
from .auth import AuthError, TokenRefreshError
from .util import chunks, daterange, ensure_private_dir, read_json, redact, write_json_atomic

log = logging.getLogger("hsync.sync")

# group -> (output fields, data type, method)
GROUPS = {
    "steps": (["steps"], "steps", "dailyRollUp"),
    "calories_total": (["calories_total"], "total-calories", "dailyRollUp"),
    "calories_active": (["calories_active"], "active-energy-burned", "dailyRollUp"),
    "sedentary": (["sedentary_minutes"], "sedentary-period", "dailyRollUp"),
    "resting_heart_rate": (["resting_heart_rate"], "daily-resting-heart-rate", "list"),
    "hrv": (["hrv"], "daily-heart-rate-variability", "list"),
    "sleep": (["sleep"], "sleep", "list"),
    "workouts": (["workouts"], "exercise", "list"),
    "spo2": (["spo2"], "daily-oxygen-saturation", "list"),
    "skin_temperature": (["skin_temperature"], "daily-sleep-temperature-derivations", "list"),
    "breathing_rate": (["breathing_rate"], "daily-respiratory-rate", "list"),
}
FIELD_ORDER = ["date", "steps", "calories_total", "calories_active", "sedentary_minutes",
               "resting_heart_rate", "hrv", "sleep", "workouts", "spo2", "skin_temperature",
               "breathing_rate", "meta"]

DAILY_LIST_PARSERS = {
    "daily-resting-heart-rate": parse.parse_resting_hr,
    "daily-heart-rate-variability": parse.parse_hrv,
    "daily-oxygen-saturation": parse.parse_spo2,
    "daily-respiratory-rate": parse.parse_breathing,
    "daily-sleep-temperature-derivations": parse.parse_skin_temp,
}


def snake(dt):
    return dt.replace("-", "_")


def list_filter(data_type, start, end_excl):
    """AIP-160 filters as documented on users.dataTypes.dataPoints.list."""
    a, b = start.isoformat(), end_excl.isoformat()
    if data_type == "sleep":
        # sleep: only interval.end_time / civil_end_time are filterable; we attribute to wake day
        return f'sleep.interval.civil_end_time >= "{a}" AND sleep.interval.civil_end_time < "{b}"'
    if data_type == "exercise":
        return f'exercise.interval.civil_start_time >= "{a}" AND exercise.interval.civil_start_time < "{b}"'
    return f'{snake(data_type)}.date >= "{a}" AND {snake(data_type)}.date < "{b}"'


# ------------------------------------------------------------------ raw recorder
class RawRecorder:
    """Keeps raw API responses (private dir, 700/600) for debugging field shapes."""

    def __init__(self, root=None, keep_days=None):
        self.root = Path(root or config.RAW_DIR)
        self.keep_days = config.RAW_KEEP_DAYS if keep_days is None else keep_days
        self.run_dir = None
        self.n = 0

    def __call__(self, kind, data_type, req, resp):
        if self.run_dir is None:
            ensure_private_dir(self.root)
            name = datetime.now().strftime("%Y%m%dT%H%M%S")
            d, i = self.root / name, 1
            while d.exists():  # same second (or the repeated hour on the DST day): never mix two runs
                d, i = self.root / f"{name}_{i}", i + 1
            self.run_dir = ensure_private_dir(d)
        self.n += 1
        write_json_atomic(self.run_dir / f"{self.n:03d}_{kind}_{data_type}.json",
                          {"kind": kind, "data_type": data_type, "request": req, "response": resp}, mode=0o600)

    def prune(self):
        if not self.root.exists():
            return
        cutoff = time.time() - self.keep_days * 86400
        for d in self.root.iterdir():
            if d.is_symlink() or not d.is_dir() or d == self.run_dir:
                continue
            if d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------ fetch
def fetch_group(api, group, start, end_excl, tz):
    _, data_type, method = GROUPS[group]
    res = {}
    for a, b in chunks(start, end_excl, config.CHUNK_DAYS):
        if method == "dailyRollUp":
            res.update(parse.parse_daily_rollup(data_type, api.daily_rollup(data_type, a, b)))
        else:
            pts = list(api.list_points(data_type, list_filter(data_type, a, b),
                                       page_size=25 if data_type in ("sleep", "exercise") else 1000))
            if data_type == "sleep":
                res.update(parse.parse_sleep(pts, tz))
            elif data_type == "exercise":
                res.update(parse.parse_exercise(pts, tz))
            else:
                res.update(DAILY_LIST_PARSERS[data_type](pts))
    keep = {d.isoformat() for d in daterange(start, end_excl)}
    return {d: v for d, v in res.items() if d in keep}


def fetch_range(api, start, end_excl, tz):
    """-> (values {group: {date: value}}, errors {group: message})."""
    values, errors = {}, {}
    for g in GROUPS:
        try:
            values[g] = fetch_group(api, g, start, end_excl, tz)
        except (AuthError, AccountNotLinked, TokenRefreshError):
            raise  # credential problems are fatal for the whole run (nothing is written)
        except Exception as e:  # one failing group must not sink the rest
            msg = redact(f"{type(e).__name__}: {e}" if not str(e) else str(e))[:500]
            log.error("group %s failed: %s", g, msg)
            errors[g] = msg
    return values, errors


# ------------------------------------------------------------------ build / write
def build_day(day_iso, values, errors, previous, today_iso, tz, sync_time):
    doc = {"date": day_iso}
    stale = []
    if not isinstance(previous, dict) or previous.get("date") != day_iso:
        previous = None  # corrupt/foreign file: never carry its values over
    for g, (fields, data_type, method) in GROUPS.items():
        if g in errors:
            if previous and all(f in previous for f in fields):
                for f in fields:
                    doc[f] = previous[f]
                stale.append(g)
            else:
                for f in fields:
                    doc[f] = None
            continue
        v = values.get(g, {}).get(day_iso)
        if g == "workouts":
            v = v or []  # fetched successfully: no sessions means an empty list
        doc[fields[0]] = v
    doc["meta"] = {
        "source": "Google Health API v4 (health.googleapis.com)",
        "user": "users/me",
        "sync_time": sync_time,
        "timezone": tz,
        "day_complete": day_iso < today_iso,
        "endpoints": {g: f"{m} {dt}" for g, (_, dt, m) in GROUPS.items()},
        "errors": {g: errors[g] for g in errors} or None,
        "stale_groups": stale or None,
        "schema_version": config.SCHEMA_VERSION,
        "generator": "health-sync",
    }
    return {k: doc[k] for k in FIELD_ORDER}


def write_days(values, errors, start, end_excl, out_dir, tz):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_tmp(out_dir)
    now = datetime.now(ZoneInfo(tz))
    today = now.date().isoformat()
    written = []
    for d in daterange(start, end_excl):
        iso = d.isoformat()
        path = out_dir / f"{iso}.json"
        prev = read_json(path)
        doc = build_day(iso, values, errors, prev, today, tz, now.isoformat(timespec="seconds"))
        write_json_atomic(path, doc, mode=0o644)
        written.append(str(path))
    return written


def _cleanup_tmp(d, max_age_s=3600):
    """Remove temp files left behind by a killed run (atomic writes never leave partial targets)."""
    cutoff = time.time() - max_age_s
    for p in d.glob(".tmp-*"):
        try:
            if p.is_file() and not p.is_symlink() and p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def today_local(tz):
    return datetime.now(ZoneInfo(tz)).date()


class LockBusy(RuntimeError):
    """Another run holds the lock (exit 4)."""


class Lock:
    """flock(2)-based: the kernel drops the lock when the holder dies, so a leftover lock FILE
    from a crashed/killed run is never stale. The file is opened without following symlinks,
    never truncated before the lock is held, and carries the holder's pid for diagnostics."""

    def __init__(self, path=None):
        self.path = Path(path or config.LOCK_FILE)
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise RuntimeError(f"refusing to use symlinked lock file {self.path}") from None
            raise
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = ""
            try:
                holder = os.pread(fd, 32, 0).decode(errors="ignore").strip()
            except OSError:
                pass
            os.close(fd)
            raise LockBusy("another health-sync run holds the lock" + (f" (pid {holder})" if holder else "")) from None
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
        self.fd = fd
        return self

    def __exit__(self, *a):
        try:
            os.ftruncate(self.fd, 0)
        except OSError:
            pass
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)
        self.fd = None


def update_state(**kw):
    st = read_json(config.STATE_FILE, {}) or {}
    if not isinstance(st, dict):
        st = {}
    st.update({k: (redact(v) if isinstance(v, str) else v) for k, v in kw.items()})
    write_json_atomic(config.STATE_FILE, st, mode=0o600)
    return st


def run(api, start, end_excl, out_dir=None, tz=None, recorder=None):
    """Sync [start, end_excl). Returns dict summary. Raises AuthError/AccountNotLinked."""
    tz = tz or config.TIMEZONE
    out_dir = out_dir or config.OUT_DIR
    if not start < end_excl:
        raise ValueError("empty date range")
    t0 = datetime.now(ZoneInfo(tz)).isoformat(timespec="seconds")
    update_state(last_attempt=t0)
    values, errors = fetch_range(api, start, end_excl, tz)
    if len(errors) == len(GROUPS):
        update_state(last_error=f"{t0}: all data groups failed: {json.dumps(errors)[:800]}")
        raise RuntimeError("all data groups failed; nothing written: " + "; ".join(f"{k}: {v}" for k, v in errors.items()))
    written = write_days(values, errors, start, end_excl, out_dir, tz)
    if recorder:
        recorder.prune()
    summary = {"range": [start.isoformat(), (end_excl - timedelta(days=1)).isoformat()],
               "files": len(written), "errors": errors or None}
    kw = {"last_sync": t0, "last_sync_range": summary["range"], "last_sync_errors": errors or None}
    if not errors:
        kw["last_full_success"] = t0
    update_state(**kw)
    return summary
