"""Local config file (hsync.local.env) and generic defaults."""
import os
import subprocess
import sys

from conftest import ROOT
from health_sync import config


def write(p, text, mode=0o600):
    p.write_text(text)
    os.chmod(p, mode)
    return p


def test_python_loader_rules(tmp_path, capsys):
    f = write(tmp_path / "local.env", "# comment\n\nHSYNC_TZ=Asia/Tokyo\nexport HSYNC_OUT_DIR='/data/daily'\n"
              'HSYNC_DAYS="5"\nPATH=/evil\nHSYNC_LOCAL_ENV=/other\nHSYNC_APP_DIR=$(touch x)\nnot a line\n'
              "HSYNC_RAW_KEEP_DAYS=30\n")
    env = {"HSYNC_RAW_KEEP_DAYS": "7"}
    loaded = config.load_local_env(f, env)
    assert env == {"HSYNC_TZ": "Asia/Tokyo", "HSYNC_OUT_DIR": "/data/daily", "HSYNC_DAYS": "5",
                   "HSYNC_APP_DIR": "$(touch x)", "HSYNC_RAW_KEEP_DAYS": "7"}  # env wins, value taken literally
    assert "HSYNC_RAW_KEEP_DAYS" not in loaded and "PATH" not in env
    assert capsys.readouterr().err.count("ignored") == 3  # PATH, HSYNC_LOCAL_ENV, line without '='
    assert not (tmp_path / "x").exists()


def test_python_loader_ignores_writable_missing_and_disabled(tmp_path, capsys):
    f = write(tmp_path / "local.env", "HSYNC_TZ=Asia/Tokyo\n", mode=0o622)
    env = {}
    assert config.load_local_env(f, env) == {} and env == {} and "group/world-writable" in capsys.readouterr().err
    assert config.load_local_env(tmp_path / "missing.env", env) == {}
    assert config.load_local_env(None, {"HSYNC_LOCAL_ENV": ""}) == {}
    write(f, "HSYNC_TZ=Asia/Tokyo\n")
    env = {"HSYNC_LOCAL_ENV": str(f)}
    assert config.load_local_env(None, env) == {"HSYNC_TZ": "Asia/Tokyo"}


def _defaults(env):
    code = ("from health_sync import config as c; print(c.APP_DIR); print(c.OUT_DIR); print(c.SUMMARY_CSV); "
            "print(c.TIMEZONE)")
    r = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(ROOT), capture_output=True, text=True, check=True)
    return r.stdout.splitlines()


def test_generic_defaults_without_any_settings():
    env = {k: v for k, v in os.environ.items() if not k.startswith("HSYNC_")}
    env.update(HSYNC_LOCAL_ENV="", TZ="Asia/Tokyo", PYTHONPATH=str(ROOT))
    app, out, csv_, tz = _defaults(env)
    assert app == str(ROOT) and out == str(ROOT / "daily") and csv_ == str(ROOT / "daily_summary.csv")
    assert tz == "Asia/Tokyo"


def test_local_file_applies_and_environment_wins(tmp_path):
    f = write(tmp_path / "local.env", f"HSYNC_APP_DIR={tmp_path}\nHSYNC_OUT_DIR={tmp_path}/out/daily\nHSYNC_TZ=America/Denver\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("HSYNC_")}
    env.update(HSYNC_LOCAL_ENV=str(f), HSYNC_TZ="Europe/Lisbon", PYTHONPATH=str(ROOT))
    app, out, csv_, tz = _defaults(env)
    assert (app, out, csv_, tz) == (str(tmp_path), f"{tmp_path}/out/daily", f"{tmp_path}/out/daily_summary.csv",
                                    "Europe/Lisbon")


def test_system_timezone_fallbacks(monkeypatch):
    monkeypatch.setenv("TZ", "America/Denver")
    assert config.system_timezone() == "America/Denver"
    monkeypatch.setenv("TZ", "Not/AZone")
    monkeypatch.setattr(config.os, "readlink", lambda p: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(config.Path, "read_text", lambda self, **k: (_ for _ in ()).throw(OSError()))
    assert config.system_timezone() == "UTC"


# ------------------------------------------------------------------ run_sync.sh loader (bash)
def _run_sync(tmp_path, file_text, mode=0o600, **env):
    f = write(tmp_path / "local.env", file_text, mode)
    e = {k: v for k, v in os.environ.items() if not k.startswith("HSYNC_")}
    # an invalid HSYNC_RUN_TIMEOUT always stops run_sync.sh during validation, before anything runs
    e.update(HSYNC_LOCAL_ENV=str(f), HSYNC_RUN_TIMEOUT="stop", **env)
    return subprocess.run(["bash", str(ROOT / "run_sync.sh")], env=e, capture_output=True, text=True, timeout=10,
                          cwd=str(tmp_path))


def test_run_sync_loads_local_file(tmp_path):
    r = _run_sync(tmp_path, '# x\nexport HSYNC_DAYS="nope"\n')
    assert r.returncode == 1 and "HSYNC_DAYS must be" in r.stderr


def test_run_sync_environment_wins_over_file(tmp_path):
    r = _run_sync(tmp_path, "HSYNC_DAYS=nope\n", HSYNC_DAYS="3")
    assert r.returncode == 1 and "HSYNC_DAYS must be" not in r.stderr and "HSYNC_RUN_TIMEOUT must be" in r.stderr


def test_run_sync_ignores_writable_file(tmp_path):
    r = _run_sync(tmp_path, "HSYNC_DAYS=nope\n", mode=0o666)
    assert r.returncode == 1 and "group/world-writable" in r.stderr and "HSYNC_DAYS must be" not in r.stderr


def test_run_sync_loader_never_executes(tmp_path):
    r = _run_sync(tmp_path, f"HSYNC_DAYS=$(touch {tmp_path}/pwned)\ntouch {tmp_path}/pwned2\nPATH=/evil\n"
                            f"HSYNC_X=`touch {tmp_path}/pwned3`\n")
    assert r.returncode == 1 and "HSYNC_DAYS must be" in r.stderr and r.stderr.count("ignored") == 2
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("pwned")]
