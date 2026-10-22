"""Shared fakes. Response shapes mirror the v4 discovery document / reference examples."""
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Tests never see the developer's/installation's settings: drop HSYNC_* from the environment,
# disable the local config file (also for subprocesses) and pin the timezone the fixtures assume.
for _k in [k for k in os.environ if k.startswith("HSYNC_")]:
    del os.environ[_k]
os.environ["HSYNC_LOCAL_ENV"] = ""
os.environ["HSYNC_TZ"] = "Europe/Paris"

CLIENT_SECRET_VALUE = "GOCSPX-test-secret-DO-NOT-PRINT"
REFRESH_VALUE = "1//refresh-token-DO-NOT-PRINT"
ACCESS_VALUE = "ya29.access-token-DO-NOT-PRINT"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point every path at tmp and reload config."""
    from health_sync import config
    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.setattr(config, "APP_DIR", app)
    monkeypatch.setattr(config, "SECRETS_DIR", app / "secrets")
    monkeypatch.setattr(config, "CLIENT_SECRET_FILE", app / "secrets" / "client_secret.json")
    monkeypatch.setattr(config, "TOKEN_FILE", app / "secrets" / "token.json")
    monkeypatch.setattr(config, "PENDING_AUTH_FILE", app / "secrets" / "pending_auth.json")
    monkeypatch.setattr(config, "STATE_FILE", app / "state.json")
    monkeypatch.setattr(config, "RAW_DIR", app / "raw")
    monkeypatch.setattr(config, "LOCK_FILE", app / ".sync.lock")
    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "daily")
    monkeypatch.setattr(config, "DOWNLOADS_GLOB", str(tmp_path / "Downloads" / "client_secret_*.json"))
    return tmp_path


def client_json(tmp_path, kind="installed"):
    d = tmp_path / "Downloads"
    d.mkdir(exist_ok=True)
    p = d / "client_secret_123-abc.apps.googleusercontent.com.json"
    p.write_text(json.dumps({kind: {
        "client_id": "123-abc.apps.googleusercontent.com", "project_id": "example-project",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
        "client_secret": CLIENT_SECRET_VALUE, "redirect_uris": ["http://localhost"]}}))
    return p


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.content = json.dumps(self._body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body


def gdate(iso):
    y, m, d = map(int, iso.split("-"))
    return {"year": y, "month": m, "day": d}


def civil(iso, h=0, mi=0, s=0):
    t = {} if not (h or mi or s) else {"hours": h, "minutes": mi, "seconds": s}
    return {"date": gdate(iso), "time": t}


class FakeHealth:
    """Routes requests like health.googleapis.com/v4 + oauth2 token endpoint."""

    def __init__(self, data=None):
        self.data = data or {}  # data_type -> list of DataPoints (list) or rollup points
        self.calls = []
        self.fail = {}  # data_type -> list of statuses to return first
        self.page_size_split = {}  # data_type -> n per page (simulate pagination)
        self.token_calls = 0
        self.refresh_status = 200

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append((method, url, params, json))
        assert headers["Authorization"].startswith("Bearer ")
        path = urlparse(url).path
        if path.endswith("/identity"):
            return Resp(200, {"name": "users/me/identity", "healthUserId": "111222333", "legacyUserId": "A1B2C3"})
        dt = path.split("/dataTypes/")[1].split("/")[0]
        fl = self.fail.get(dt)
        if fl:
            st = fl.pop(0)
            return Resp(st, {"error": {"code": st, "status": "UNAVAILABLE", "message": "boom"}})
        if path.endswith(":dailyRollUp"):
            r = json["range"]
            a = "%04d-%02d-%02d" % (r["start"]["date"]["year"], r["start"]["date"]["month"], r["start"]["date"]["day"])
            b = "%04d-%02d-%02d" % (r["end"]["date"]["year"], r["end"]["date"]["month"], r["end"]["date"]["day"])
            if dt == "total-calories":
                from datetime import date
                assert (date.fromisoformat(b) - date.fromisoformat(a)).days <= 14
            pts = [p for p in self.data.get(dt, []) if a <= self._rd(p) < b]
            pts.sort(key=self._rd, reverse=True)  # API: newest first
            return Resp(200, {"rollupDataPoints": pts})
        # list
        pts = list(self.data.get(dt, []))
        n = self.page_size_split.get(dt)
        tok = int((params or {}).get("pageToken", 0) or 0)
        if n:
            page, nxt = pts[tok:tok + n], (str(tok + n) if tok + n < len(pts) else None)
        else:
            page, nxt = pts, None
        body = {"dataPoints": page}
        if nxt:
            body["nextPageToken"] = nxt
        return Resp(200, body)

    @staticmethod
    def _rd(p):
        d = p["civilStartTime"]["date"]
        return "%04d-%02d-%02d" % (d["year"], d["month"], d["day"])

    def post(self, url, data=None, timeout=None, **kw):
        assert "oauth2.googleapis.com/token" in url
        self.token_calls += 1
        if data["grant_type"] == "refresh_token":
            if self.refresh_status != 200:
                return Resp(self.refresh_status, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
            return Resp(200, {"access_token": ACCESS_VALUE + "2", "expires_in": 3599, "token_type": "Bearer",
                              "scope": " ".join(__import__("health_sync.config", fromlist=["x"]).SCOPES)})
        return Resp(200, {"access_token": ACCESS_VALUE, "expires_in": 3599, "token_type": "Bearer",
                          "refresh_token": REFRESH_VALUE,
                          "scope": " ".join(__import__("health_sync.config", fromlist=["x"]).AUTH_SCOPES)})


class FakeCreds:
    def __init__(self):
        self.refreshes = 0

    def access_token(self, force_refresh=False):
        if force_refresh:
            self.refreshes += 1
        return "fake-token"
