"""Paths, constants and scopes. Everything can be overridden by env vars (HSYNC_*).

Precedence: environment > local config file (hsync.local.env, see load_local_env) > generic defaults.
"""
import os
import re
import stat
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPO_DIR = Path(__file__).resolve().parent.parent
LOCAL_ENV_DEFAULT = REPO_DIR / "hsync.local.env"
_ENV_KEY = re.compile(r"HSYNC_[A-Z0-9_]+")


def load_local_env(path=None, environ=None):
    """Load HSYNC_* settings from a local, gitignored config file into the environment.

    Default file: hsync.local.env next to the code; HSYNC_LOCAL_ENV overrides the path, an empty
    HSYNC_LOCAL_ENV disables it. Format: KEY=VALUE per line ('#' comments, optional 'export ',
    optional matching quotes). Only HSYNC_* keys are accepted, nothing is expanded or executed,
    variables already set in the environment win. A group/world-writable file is ignored.
    run_sync.sh applies the same rules in bash. Returns {key: value} of what was loaded."""
    env = os.environ if environ is None else environ
    p = env.get("HSYNC_LOCAL_ENV", str(LOCAL_ENV_DEFAULT)) if path is None else path
    if not p:
        return {}
    p = Path(p)
    try:
        st = p.stat()
        if not stat.S_ISREG(st.st_mode):
            return {}
        if st.st_mode & 0o022:
            print(f"hsync: ignoring {p}: group/world-writable (chmod 600 it)", file=sys.stderr)
            return {}
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    loaded = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, val = line.partition("=")
        if not sep or not _ENV_KEY.fullmatch(key) or key == "HSYNC_LOCAL_ENV":
            print(f"hsync: {p}:{n}: ignored (expected HSYNC_NAME=value)", file=sys.stderr)
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key not in env:
            env[key] = val
            loaded[key] = val
    return loaded


def _valid_tz(name):
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False


def system_timezone():
    """IANA name of the system timezone ($TZ, /etc/localtime symlink, /etc/timezone), else 'UTC'."""
    cands = [os.environ.get("TZ", "").lstrip(":")]
    try:
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            name = link.split("zoneinfo/", 1)[1]
            for prefix in ("posix/", "right/"):
                name = name.removeprefix(prefix)
            cands.append(name)
    except OSError:
        pass
    try:
        cands.append(Path("/etc/timezone").read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        pass
    for c in cands:
        if c and not c.startswith("/") and _valid_tz(c):
            return c
    return "UTC"


load_local_env()

APP_DIR = Path(os.environ.get("HSYNC_APP_DIR") or REPO_DIR)
SECRETS_DIR = Path(os.environ.get("HSYNC_SECRETS_DIR", str(APP_DIR / "secrets")))
CLIENT_SECRET_FILE = SECRETS_DIR / "client_secret.json"
TOKEN_FILE = SECRETS_DIR / "token.json"
PENDING_AUTH_FILE = SECRETS_DIR / "pending_auth.json"
STATE_FILE = Path(os.environ.get("HSYNC_STATE_FILE", str(APP_DIR / "state.json")))
RAW_DIR = Path(os.environ.get("HSYNC_RAW_DIR", str(APP_DIR / "raw")))
try:
    RAW_KEEP_DAYS = max(1, int(os.environ.get("HSYNC_RAW_KEEP_DAYS", "14")))
except ValueError:
    RAW_KEEP_DAYS = 14
LOCK_FILE = APP_DIR / ".sync.lock"
OUT_DIR = Path(os.environ.get("HSYNC_OUT_DIR") or APP_DIR / "daily")
SUMMARY_CSV = Path(os.environ.get("HSYNC_SUMMARY_CSV") or OUT_DIR.parent / "daily_summary.csv")
TIMEZONE = os.environ.get("HSYNC_TZ") or system_timezone()
DOWNLOADS_GLOB = os.path.expanduser("~/Downloads/client_secret_*.json")

API_BASE = os.environ.get("HSYNC_API_BASE", "https://health.googleapis.com/v4")
USER = "me"
AUTH_URI_DEFAULT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI_DEFAULT = "https://oauth2.googleapis.com/token"  # noqa: S105  # nosec B105
DEFAULT_PORT = 8765

_S = "https://www.googleapis.com/auth/googlehealth."
# read scopes used by the daily sync (must never be dropped by a re-consent)
READ_SCOPES = [
    _S + "activity_and_fitness.readonly",
    _S + "health_metrics_and_measurements.readonly",
    _S + "sleep.readonly",
    _S + "profile.readonly",
]
SCOPES = READ_SCOPES  # what the daily sync needs (status/sync checks)
# write/log scopes for meals, drinks and symptoms (hsync log-*)
NUTRITION_WRITE_SCOPE = _S + "nutrition.writeonly"
SYMPTOMS_WRITE_SCOPE = _S + "logged_symptoms.writeonly"
SYMPTOMS_READ_SCOPE = _S + "logged_symptoms.readonly"
WRITE_SCOPES = [NUTRITION_WRITE_SCOPE, SYMPTOMS_WRITE_SCOPE, SYMPTOMS_READ_SCOPE]
AUTH_SCOPES = READ_SCOPES + WRITE_SCOPES  # requested at consent time

# Rollup range limit is 14 days for total-calories (90 for others); use 14 everywhere.
CHUNK_DAYS = 14
HTTP_TIMEOUT = 60  # read timeout (s)
HTTP_CONNECT_TIMEOUT = 10
MAX_BACKFILL_DAYS = 3660
LOG_MAX_BYTES = 5_000_000
SCHEMA_VERSION = 1
