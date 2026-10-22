"""Small, pure helpers (time parsing, numbers, atomic private writes)."""
import json
import math
import os
import re
import stat
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_FRAC = re.compile(r"(\.\d+)")


def parse_rfc3339(s):
    """Parse an RFC 3339 timestamp (Z or offset, up to nanosecond precision)."""
    if not s:
        return None
    s = s.strip().replace("z", "Z")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    s = _FRAC.sub(lambda m: m.group(1)[:7], s, count=1)  # keep <= 6 fractional digits
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_duration_s(v):
    """Google JSON Duration ("3600s", "1.5s") or number -> seconds (float). None if unparseable."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s.endswith("s"):
        s = s[:-1]
    try:
        return float(s)
    except ValueError:
        m = re.fullmatch(r"P?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?", str(v).strip().upper())
        if m and any(m.groups()):
            h, mi, se = m.groups()
            return int(h or 0) * 3600 + int(mi or 0) * 60 + float(se or 0)
    return None


def offset_seconds(v):
    """utcOffset like "7200s" -> timedelta."""
    sec = parse_duration_s(v)
    return None if sec is None else timedelta(seconds=sec)


def num(v):
    """int64-as-string / number -> int or float; None stays None."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v if math.isfinite(v) else None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):  # "NaN"/"Infinity" would make the daily JSON invalid
        return None
    return int(f) if f.is_integer() and "." not in str(v) else f


def rnd(v, nd=1):
    if v is None:
        return None
    f = float(v)
    return round(f, nd) if math.isfinite(f) else None


def gdate_to_iso(d):
    """google.type.Date dict -> 'YYYY-MM-DD' (None if incomplete)."""
    if not isinstance(d, dict):
        return None
    try:
        y, m, dd = int(d.get("year", 0)), int(d.get("month", 0)), int(d.get("day", 0))
        if not (y and m and dd):
            return None
        return date(y, m, dd).isoformat()
    except (TypeError, ValueError):
        return None


def iso_to_gdate(iso):
    d = date.fromisoformat(iso)
    return {"year": d.year, "month": d.month, "day": d.day}


def to_local(dt, tz):
    if dt is None:
        return None
    return dt.astimezone(ZoneInfo(tz))


def local_iso_minutes(dt, tz):
    loc = to_local(dt, tz)
    return None if loc is None else loc.isoformat(timespec="minutes")


def daterange(start, end_excl):
    d = start
    while d < end_excl:
        yield d
        d += timedelta(days=1)


def chunks(start, end_excl, size):
    d = start
    while d < end_excl:
        e = min(d + timedelta(days=size), end_excl)
        yield d, e
        d = e


def _atomic_write(path, write_fn, mode, suffix):
    """Write via a same-directory temp file (created 0600 by mkstemp, so no window in which
    private data is readable), fsync, chmod to the final mode, then rename over the target."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=suffix)
    try:
        with os.fdopen(fd, "w", newline="" if suffix == ".csv" else None) as f:
            write_fn(f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    try:  # make the rename durable too
        dfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def write_json_atomic(path, obj, mode=0o644):
    def w(f):
        # allow_nan=False: never emit NaN/Infinity (invalid JSON for downstream readers)
        json.dump(obj, f, indent=2, ensure_ascii=False, sort_keys=False, allow_nan=False)
        f.write("\n")
    _atomic_write(path, w, mode, ".json")


def write_text_atomic(path, text, mode=0o600, suffix=".txt"):
    _atomic_write(path, lambda f: f.write(text), mode, suffix)


def ensure_private_dir(p):
    p = Path(p)
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(p, 0o700)
    return p


def ensure_private_file(p):
    """Tighten a secret file to 0600 if something loosened it. Returns True if it had to."""
    try:
        st = os.stat(p)
    except FileNotFoundError:
        return False
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(p, 0o600)
        return True
    return False


def read_json(path, default=None):
    """Corrupt, truncated or non-UTF-8 files read as `default` instead of crashing a run."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, IsADirectoryError, ValueError):  # JSONDecodeError/UnicodeDecodeError are ValueErrors
        return default


_REDACT = [
    (re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)(bearer\s+)?[^\s'\",}]+"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\b(access_token|refresh_token|id_token|client_secret|code|code_verifier|state|token|"
                r"pageToken|key|assertion)=([^&\s'\"]+)"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)(['\"](?:access_token|refresh_token|id_token|client_secret|code_verifier)['\"]\s*:\s*['\"])"
                r"[^'\"]+"), r"\1[REDACTED]"),
    (re.compile(r"ya29\.[A-Za-z0-9._-]+"), "[REDACTED]"),
    (re.compile(r"1//[A-Za-z0-9._-]{8,}"), "[REDACTED]"),
    (re.compile(r"GOCSPX-[A-Za-z0-9_-]+"), "[REDACTED]"),
    (re.compile(r"\b4/[0-9A-Za-z_-]{10,}"), "[REDACTED]"),  # authorization codes
]


def redact(text):
    """Strip anything that looks like a credential from a message before it is logged/printed/stored."""
    s = str(text)
    for rx, rep in _REDACT:
        s = rx.sub(rep, s)
    return s


class RedactingFilter:
    """logging filter: renders the message once and redacts credentials from it (and from tracebacks)."""

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:  # never let a bad format string kill logging
            msg = str(record.msg)
        record.msg, record.args = redact(msg), None
        if record.exc_info and record.exc_info[1] is not None:
            import traceback
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)).rstrip())
            record.exc_info = None
        return True
