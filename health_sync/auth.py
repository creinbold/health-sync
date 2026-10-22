"""OAuth 2.0 for an installed ("Desktop app") client with a loopback redirect + PKCE.

Secrets never get printed: only booleans, scopes and expiry times are reported.
"""
import base64
import glob
import hashlib
import hmac
import http.server
import ipaddress
import json
import logging
import os
import secrets as pysecrets
import time
import urllib.parse
from pathlib import Path

import requests

from . import config
from .util import ensure_private_dir, ensure_private_file, read_json, redact, write_json_atomic, write_text_atomic

log = logging.getLogger("hsync.auth")
PENDING_MAX_AGE_S = 12 * 3600  # an authorization started more than 12 h ago is not completed
TOKEN_RETRIES = 3
_sleep = time.sleep  # patched in tests


class AuthError(Exception):
    """Raised when user re-authorization is required."""


class TokenRefreshError(RuntimeError):
    """Token endpoint unreachable / 5xx after retries (transient; not a re-auth case)."""


# ---------------------------------------------------------------- client JSON
def find_client_secret(explicit=None):
    if explicit:
        return Path(explicit).expanduser()
    if config.CLIENT_SECRET_FILE.exists():
        return config.CLIENT_SECRET_FILE
    cands = sorted(glob.glob(config.DOWNLOADS_GLOB), key=os.path.getmtime, reverse=True)
    return Path(cands[0]) if cands else None


def load_client(path=None):
    """Return dict(client_id, client_secret, auth_uri, token_uri, kind) from a Google client JSON."""
    path = Path(path or config.CLIENT_SECRET_FILE)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise AuthError(f"cannot read client JSON {path}: {type(e).__name__}") from None
    if not isinstance(data, dict):
        raise AuthError(f"{path} is not a Google OAuth client JSON")
    kind = "installed" if "installed" in data else "web" if "web" in data else None
    if kind is None:
        raise AuthError(f"{path} is not a Google OAuth client JSON (no 'installed'/'web' key)")
    c = data[kind]
    if not isinstance(c, dict) or not c.get("client_id"):
        raise AuthError("client JSON has no client_id")
    return {
        "client_id": c["client_id"],
        "client_secret": c.get("client_secret"),
        "auth_uri": config.AUTH_URI_DEFAULT,  # always the v2 endpoint
        # pinned: a tampered client JSON must not be able to send the refresh token elsewhere
        "token_uri": config.TOKEN_URI_DEFAULT,
        "kind": kind,
    }


def install_client_secret(src):
    """Copy the downloaded client JSON into secrets/ with 600 perms."""
    ensure_private_dir(config.SECRETS_DIR)
    src = Path(src).expanduser().resolve()
    dst = config.CLIENT_SECRET_FILE
    load_client(src)  # validate before copying
    if src != dst.resolve():
        # atomic + created 0600: no window in which the client secret is readable by others
        write_text_atomic(dst, src.read_text(encoding="utf-8"), mode=0o600, suffix=".json")
    os.chmod(dst, 0o600)
    return dst


# ---------------------------------------------------------------- token store
class TokenStore:
    def __init__(self, path=None):
        self.path = Path(path or config.TOKEN_FILE)

    def load(self):
        if ensure_private_file(self.path):
            log.warning("token file had loose permissions; reset to 0600")
        tok = read_json(self.path)
        return tok if isinstance(tok, dict) else None

    def save(self, tok):
        ensure_private_dir(self.path.parent)
        write_json_atomic(self.path, tok, mode=0o600)


def _pkce():
    verifier = base64.urlsafe_b64encode(pysecrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_auth_url(client, redirect_uri, state, code_challenge, scopes=None):
    params = {
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes or config.AUTH_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return client["auth_uri"] + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


def _token_record(resp_json, prev=None):
    if not isinstance(resp_json, dict) or not isinstance(resp_json.get("access_token"), str):
        raise TokenRefreshError("token endpoint returned no access_token")
    now = int(time.time())
    rec = dict(prev or {})
    rec["access_token"] = resp_json["access_token"]
    try:
        expires_in = int(resp_json.get("expires_in", 3600))
    except (TypeError, ValueError):
        expires_in = 3600
    rec["expires_at"] = now + max(0, expires_in - 60)
    rec["token_type"] = resp_json.get("token_type", "Bearer")
    if resp_json.get("refresh_token"):
        rec["refresh_token"] = resp_json["refresh_token"]
        rec["refresh_token_obtained_at"] = now
    if resp_json.get("refresh_token_expires_in"):
        try:
            rec["refresh_token_expires_at"] = now + int(resp_json["refresh_token_expires_in"])
        except (TypeError, ValueError):
            pass
    if isinstance(resp_json.get("scope"), str):
        rec["scopes"] = resp_json["scope"].split()
    rec["updated_at"] = now
    return rec


def _token_post(client, data, session, retries):
    """POST to the token endpoint: TLS verified (requests default), timeouts, no redirects,
    retries on network errors / 429 / 5xx. Never includes request data in errors."""
    s = session or requests
    last = None
    for attempt in range(retries + 1):
        try:
            r = s.post(client["token_uri"], data=data, timeout=config.HTTP_TIMEOUT, allow_redirects=False)
        except requests.RequestException as e:
            last = f"network error ({type(e).__name__})"
        else:
            if r.status_code not in (429, 500, 502, 503, 504):
                return r
            last = f"HTTP {r.status_code}"
        if attempt < retries:
            _sleep(min(30, 2 ** attempt))
    raise TokenRefreshError(f"token endpoint unavailable: {last}")


def _json(r):
    try:
        return r.json()
    except ValueError:
        return None


def exchange_code(client, code, redirect_uri, code_verifier, session=None):
    data = {
        "code": code,
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }
    if client.get("client_secret"):
        data["client_secret"] = client["client_secret"]
    try:
        r = _token_post(client, data, session, retries=1)  # codes are single-use: retry network errors only once
    except TokenRefreshError as e:
        raise AuthError(f"token exchange failed: {e}") from None
    if r.status_code != 200:
        raise AuthError(f"token exchange failed: HTTP {r.status_code} {_safe_err(r)}")
    j = _json(r)
    if not isinstance(j, dict) or not j.get("refresh_token"):
        raise AuthError("no refresh_token returned (consent must use access_type=offline&prompt=consent)")
    return _token_record(j)


def refresh(client, tok, session=None):
    if not tok or not tok.get("refresh_token"):
        raise AuthError("no refresh token stored; run: hsync auth")
    data = {
        "client_id": client["client_id"],
        "refresh_token": tok["refresh_token"],
        "grant_type": "refresh_token",
    }
    if client.get("client_secret"):
        data["client_secret"] = client["client_secret"]
    r = _token_post(client, data, session, retries=TOKEN_RETRIES)
    if r.status_code != 200:
        err = _safe_err(r)
        if r.status_code in (400, 401, 403):
            # invalid_grant (revoked/expired, e.g. 7-day Testing limit), invalid_client, unauthorized_client
            raise AuthError(f"refresh token rejected (HTTP {r.status_code} {err}); re-run: hsync auth")
        raise TokenRefreshError(f"token refresh failed: HTTP {r.status_code} {err}")
    return _token_record(_json(r), prev=tok)


def _safe_err(r):
    """Only the OAuth error code/description; never echo request data."""
    j = _json(r)
    if not isinstance(j, dict):
        return redact((r.text or "")[:200])
    e = j.get("error")
    if isinstance(e, dict):
        return redact(f"{e.get('status')} {e.get('message')}")
    return redact(f"{e}: {j.get('error_description', '')}".strip())


class Credentials:
    """Holds the client + token, refreshes on demand (not on a schedule)."""

    def __init__(self, client=None, store=None, session=None):
        self.store = store or TokenStore()
        self.session = session
        self._client = client
        self.tok = self.store.load()

    @property
    def client(self):
        if self._client is None:
            if not config.CLIENT_SECRET_FILE.exists():
                raise AuthError(f"missing {config.CLIENT_SECRET_FILE}; run: hsync auth --client-secret <file>")
            ensure_private_file(config.CLIENT_SECRET_FILE)
            self._client = load_client(config.CLIENT_SECRET_FILE)
        return self._client

    def access_token(self, force_refresh=False):
        if not self.tok:
            raise AuthError("not authorized yet; run: hsync auth")
        if force_refresh or not self.tok.get("access_token") or time.time() >= self.tok.get("expires_at", 0):
            self.tok = refresh(self.client, self.tok, self.session)
            self.store.save(self.tok)
        return self.tok["access_token"]


# ---------------------------------------------------------------- loopback flow
def _state_ok(got, expected):
    return isinstance(got, str) and isinstance(expected, str) and hmac.compare_digest(got, expected)


class _Handler(http.server.BaseHTTPRequestHandler):
    result = None
    expected_state = None
    timeout = 10  # per-connection socket timeout: a stalled client can't block the flow

    def do_GET(self):  # noqa: N802
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" not in q and "error" not in q:
            self.send_response(404)
            self.end_headers()
            return
        state_ok = _state_ok(q.get("state", [None])[0], type(self).expected_state)
        ok = state_ok and "code" in q
        if state_ok:  # a request with a wrong/missing state (CSRF, stray tab) is ignored, flow keeps waiting
            type(self).result = {k: v[0] for k, v in q.items()}
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = ("Google Health authorization received. You can close this tab."
               if ok else "Authorization failed or state mismatch. Check the terminal.")
        self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode())

    def log_message(self, *a):  # silence (URL contains the code)
        pass


def parse_redirect_url(url):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return {k: v[0] for k, v in q.items()}


def finish(client, params, pending, session=None, store=None):
    """Validate callback params against pending state and store tokens. Returns token record."""
    if params.get("error"):
        raise AuthError(f"authorization denied: {redact(params['error'])[:100]}")
    if not _state_ok(params.get("state"), pending.get("state")):
        raise AuthError("state mismatch; start again with: hsync auth")
    created = pending.get("created")
    if not isinstance(created, (int, float)) or time.time() - created > PENDING_MAX_AGE_S:
        raise AuthError("pending authorization expired; start again with: hsync auth")
    if not params.get("code"):
        raise AuthError("no authorization code in redirect")
    tok = exchange_code(client, params["code"], pending["redirect_uri"], pending["code_verifier"], session)
    store = store or TokenStore()
    old = store.load()
    lost = _lost_read_scopes(old, tok)
    if lost:
        # re-consent with some read boxes unticked: keep the working token, park the new one
        side = store.path.with_name("token.rejected.json")
        write_json_atomic(side, tok, mode=0o600)
        raise AuthError("new grant is missing read scopes the current token has ("
                        + ", ".join(s.rsplit(".", 2)[-2] + "." + s.rsplit(".", 1)[-1] for s in lost)
                        + "); kept the existing token. Re-run hsync auth and tick all boxes.")
    store.save(tok)  # atomic replace: only after a successful exchange
    try:
        config.PENDING_AUTH_FILE.unlink()
    except FileNotFoundError:
        pass
    return tok


def _lost_read_scopes(old, new):
    if not old or not old.get("refresh_token"):
        return []
    had = set(old.get("scopes") or config.READ_SCOPES)
    got = set(new.get("scopes") or [])
    if not got:  # token endpoint didn't report scopes: nothing to compare
        return []
    return [s for s in config.READ_SCOPES if s in had and s not in got]


def start_flow(client, port=config.DEFAULT_PORT, host="127.0.0.1"):
    """Bind loopback server, persist pending PKCE state (600), return (server, url, pending)."""
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError("the OAuth redirect listener must bind to a loopback address")
    _Handler.result = None
    httpd = http.server.HTTPServer((host, port), _Handler)
    redirect_uri = f"http://{host}:{httpd.server_address[1]}"
    verifier, challenge = _pkce()
    state = pysecrets.token_urlsafe(24)
    _Handler.expected_state = state
    pending = {"state": state, "code_verifier": verifier, "redirect_uri": redirect_uri, "created": int(time.time())}
    ensure_private_dir(config.SECRETS_DIR)
    write_json_atomic(config.PENDING_AUTH_FILE, pending, mode=0o600)
    return httpd, build_auth_url(client, redirect_uri, state, challenge), pending


def wait_for_callback(httpd, timeout):
    httpd.timeout = 1
    deadline = time.time() + timeout
    while _Handler.result is None and time.time() < deadline:
        httpd.handle_request()
    httpd.server_close()
    return _Handler.result
