"""Thin Google Health API v4 client: list (paginated) and dailyRollUp, with retries.

Every response is handed to an optional `recorder(kind, data_type, request, response_json)`
so raw payloads can be kept for debugging field shapes.
"""
import logging
import random
import time

import requests

from . import config
from .auth import AuthError
from .util import redact

log = logging.getLogger("hsync.api")
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_BACKOFF_S = 60
MAX_PAGES = 500  # hard stop if the server keeps handing out page tokens


class ApiError(Exception):
    def __init__(self, status, message, reason=None, payload=None):
        super().__init__(f"HTTP {status}: {message}" + (f" [{reason}]" if reason else ""))
        self.status, self.reason, self.payload = status, reason, payload


class AccountNotLinked(ApiError):
    pass


def _error(r):
    try:
        j = r.json()
    except ValueError:
        return ApiError(r.status_code, redact((r.text or "")[:300]))
    e = j.get("error", {}) if isinstance(j, dict) else {}
    if not isinstance(e, dict):
        e = {"message": str(e)}
    reason = None
    for d in e.get("details", []) or []:
        if isinstance(d, dict) and d.get("reason"):
            reason = d["reason"]
    cls = AccountNotLinked if reason == "ACCOUNT_NOT_LINKED" else ApiError
    return cls(r.status_code, redact(f"{e.get('status', '')} {e.get('message', '')}".strip()), reason, j)


class HealthApi:
    def __init__(self, creds, session=None, recorder=None, base=None, max_retries=5, sleep=time.sleep):
        self.creds = creds
        self.s = session or requests.Session()
        self.recorder = recorder
        self.base = (base or config.API_BASE).rstrip("/")
        if not self.base.startswith("https://"):
            raise ValueError("API base must be https:// (bearer tokens are never sent in clear text)")
        self.max_retries = max_retries
        self.sleep = sleep

    # ------------------------------------------------------------ transport
    def _request(self, method, path, params=None, body=None):
        url = f"{self.base}/{path.lstrip('/')}"
        refreshed = False
        attempt = 0
        while True:
            headers = {"Authorization": f"Bearer {self.creds.access_token()}", "Accept": "application/json"}
            try:
                r = self.s.request(method, url, params=params, json=body, headers=headers,
                                   timeout=(config.HTTP_CONNECT_TIMEOUT, config.HTTP_TIMEOUT))
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt >= self.max_retries:
                    # type only: the str() of a requests error carries the full URL incl. query params
                    raise ApiError(0, f"network error ({type(e).__name__}) on {method} {path}") from None
                self._backoff(attempt, None)
                attempt += 1
                continue
            if r.status_code == 401 and not refreshed:
                self.creds.access_token(force_refresh=True)
                refreshed = True
                continue
            if r.status_code in RETRY_STATUS and attempt < self.max_retries:
                self._backoff(attempt, r.headers.get("Retry-After"))
                attempt += 1
                continue
            if r.status_code == 401:
                raise AuthError("API returned 401 after token refresh; re-run: hsync auth")
            if r.status_code >= 400:
                raise _error(r)
            if not r.content:
                return {}
            try:
                j = r.json()
            except ValueError:
                raise ApiError(r.status_code, "response is not valid JSON") from None
            if not isinstance(j, dict):
                raise ApiError(r.status_code, f"unexpected JSON type {type(j).__name__}")
            return j

    def _backoff(self, attempt, retry_after):
        try:
            wait = float(retry_after)
            if wait != wait:  # NaN
                raise ValueError
        except (TypeError, ValueError):  # absent or an HTTP-date: exponential backoff with jitter
            wait = min(MAX_BACKOFF_S, 2 ** (attempt + 1)) + random.uniform(0, 1)  # noqa: S311  # nosec B311
        wait = max(0.0, min(MAX_BACKOFF_S, wait))
        log.warning("retrying in %.1fs", wait)
        self.sleep(wait)

    def _record(self, kind, data_type, req, resp):
        if self.recorder:
            try:
                self.recorder(kind, data_type, req, resp)
            except Exception:  # recording must never break a sync
                log.exception("raw recorder failed")

    # ------------------------------------------------------------ endpoints
    def identity(self):
        j = self._request("GET", f"users/{config.USER}/identity")
        self._record("get", "identity", {}, j)
        return j

    def list_points(self, data_type, filter_=None, page_size=None):
        """GET users/me/dataTypes/{type}/dataPoints with pagination. Yields DataPoint dicts."""
        path = f"users/{config.USER}/dataTypes/{data_type}/dataPoints"
        params = {}
        if filter_:
            params["filter"] = filter_
        if page_size:
            params["pageSize"] = page_size
        seen, tokens = 0, set()
        while True:
            j = self._request("GET", path, params=params)
            self._record("list", data_type, dict(params), j)
            pts = j.get("dataPoints") or []
            for dp in pts if isinstance(pts, list) else []:
                if isinstance(dp, dict):
                    seen += 1
                    yield dp
            tok = self._next_token(j, tokens, data_type)
            if not tok:
                break
            params = dict(params, pageToken=tok)
        log.debug("%s: %d data points", data_type, seen)

    def daily_rollup(self, data_type, start, end_excl, window_days=1):
        """POST users/me/dataTypes/{type}/dataPoints:dailyRollUp over [start, end_excl) civil dates."""
        path = f"users/{config.USER}/dataTypes/{data_type}/dataPoints:dailyRollUp"
        body = {
            "range": {
                "start": {"date": {"year": start.year, "month": start.month, "day": start.day}},
                "end": {"date": {"year": end_excl.year, "month": end_excl.month, "day": end_excl.day}},
            },
            "windowSizeDays": window_days,
            "pageSize": 1000,
        }
        out, tokens = [], set()
        while True:
            j = self._request("POST", path, body=body)
            self._record("dailyRollUp", data_type, dict(body), j)
            pts = j.get("rollupDataPoints") or []
            out.extend(p for p in (pts if isinstance(pts, list) else []) if isinstance(p, dict))
            tok = self._next_token(j, tokens, data_type)
            if not tok:
                return out
            body = dict(body, pageToken=tok)

    @staticmethod
    def _next_token(j, seen_tokens, data_type):
        tok = j.get("nextPageToken")
        if not tok or not isinstance(tok, str):
            return None
        if tok in seen_tokens or len(seen_tokens) >= MAX_PAGES:
            raise ApiError(0, f"{data_type}: pagination loop (repeated page token or > {MAX_PAGES} pages)")
        seen_tokens.add(tok)
        return tok

    # ------------------------------------------------------------ writes (hsync log-* / delete-log)
    def create_point(self, data_type, body):
        """POST users/me/dataTypes/{type}/dataPoints -> Operation (done + response DataPoint)."""
        return self._request("POST", f"users/{config.USER}/dataTypes/{data_type}/dataPoints", body=body)

    def get_point(self, name):
        return self._request("GET", name)

    def batch_delete(self, data_type, names):
        """POST users/me/dataTypes/{type}/dataPoints:batchDelete {names: [...]} -> Operation."""
        return self._request("POST", f"users/{config.USER}/dataTypes/{data_type}/dataPoints:batchDelete",
                             body={"names": list(names)})
