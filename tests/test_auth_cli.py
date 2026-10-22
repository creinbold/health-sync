import os
import stat
import threading
import time
import urllib.parse
import urllib.request

import pytest

from conftest import ACCESS_VALUE, CLIENT_SECRET_VALUE, REFRESH_VALUE, FakeHealth, client_json
from health_sync import auth, cli, config


def mode(p):
    return oct(stat.S_IMODE(os.stat(p).st_mode))


def test_install_client_secret_permissions(env):
    src = client_json(env)
    assert auth.find_client_secret() == src  # picked up from ~/Downloads glob
    dst = auth.install_client_secret(src)
    assert mode(dst) == "0o600" and mode(config.SECRETS_DIR) == "0o700"
    c = auth.load_client(dst)
    assert c["kind"] == "installed" and c["token_uri"] == "https://oauth2.googleapis.com/token"


def test_auth_url_has_offline_consent_pkce_and_scopes(env):
    c = auth.load_client(client_json(env))
    url = auth.build_auth_url(c, "http://127.0.0.1:8765", "st", "chal")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert q["redirect_uri"] == ["http://127.0.0.1:8765"] and q["code_challenge_method"] == ["S256"]
    assert q["scope"][0].split() == config.AUTH_SCOPES and set(config.READ_SCOPES) <= set(config.AUTH_SCOPES)
    assert CLIENT_SECRET_VALUE not in url


def test_loopback_flow_end_to_end(env, monkeypatch):
    c = auth.load_client(auth.install_client_secret(client_json(env)))
    httpd, url, pending = auth.start_flow(c, port=0)
    assert mode(config.PENDING_AUTH_FILE) == "0o600"
    port = httpd.server_address[1]
    state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]

    def browser():
        time.sleep(0.2)
        urllib.request.urlopen(f"http://127.0.0.1:{port}/?state={state}&code=4/abc&scope=x").read()
    threading.Thread(target=browser, daemon=True).start()
    params = auth.wait_for_callback(httpd, timeout=10)
    assert params["code"] == "4/abc"
    fake = FakeHealth()
    tok = auth.finish(c, params, pending, session=fake)
    assert tok["refresh_token"] == REFRESH_VALUE and mode(config.TOKEN_FILE) == "0o600"
    assert not config.PENDING_AUTH_FILE.exists()


def test_state_mismatch_rejected(env):
    c = auth.load_client(client_json(env))
    with pytest.raises(auth.AuthError):
        auth.finish(c, {"code": "x", "state": "evil"}, {"state": "good", "redirect_uri": "r", "code_verifier": "v"},
                    session=FakeHealth())


def test_refresh_and_invalid_grant(env):
    c = auth.load_client(client_json(env))
    fake = FakeHealth()
    store = auth.TokenStore()
    store.save({"access_token": "old", "expires_at": 0, "refresh_token": REFRESH_VALUE})
    creds = auth.Credentials(client=c, store=store, session=fake)
    assert creds.access_token() == ACCESS_VALUE + "2"
    assert store.load()["refresh_token"] == REFRESH_VALUE  # kept when Google doesn't rotate it
    assert creds.access_token() == ACCESS_VALUE + "2" and fake.token_calls == 1  # cached
    fake.refresh_status = 400
    with pytest.raises(auth.AuthError):
        creds.access_token(force_refresh=True)


def test_cli_never_prints_secrets(env, capsys, monkeypatch):
    src = client_json(env)
    auth.install_client_secret(src)
    auth.TokenStore().save({"access_token": ACCESS_VALUE, "expires_at": time.time() + 3000,
                            "refresh_token": REFRESH_VALUE, "scopes": config.SCOPES})
    fake = FakeHealth()
    monkeypatch.setattr("requests.Session", lambda: fake)
    rc = cli.main(["status"])
    out = capsys.readouterr()
    text = out.out + out.err
    assert rc == 0 and "live check  : OK" in text
    for s in (ACCESS_VALUE, REFRESH_VALUE, CLIENT_SECRET_VALUE):
        assert s not in text


def test_cli_auth_complete_paste_fallback(env, capsys, monkeypatch):
    c = auth.load_client(auth.install_client_secret(client_json(env)))
    httpd, url, pending = auth.start_flow(c, port=0)
    httpd.server_close()
    fake = FakeHealth()
    monkeypatch.setattr("requests.post", fake.post)
    monkeypatch.setattr("requests.Session", lambda: fake)
    rc = cli.main(["auth-complete", "--redirect-url", f"{pending['redirect_uri']}/?state={pending['state']}&code=4/zz"])
    text = "".join(capsys.readouterr())
    assert rc == 0 and "linked: yes" in text
    for s in (ACCESS_VALUE, REFRESH_VALUE, CLIENT_SECRET_VALUE):
        assert s not in text


def test_cli_sync_without_token_exits_auth_required(env, capsys):
    assert cli.main(["--out-dir", str(env / "daily"), "sync", "--days", "2"]) == cli.EXIT_AUTH
    assert "AUTH REQUIRED" in capsys.readouterr().err


def test_cli_sync_end_to_end(env, capsys, monkeypatch):
    import fixtures
    auth.install_client_secret(client_json(env))
    auth.TokenStore().save({"access_token": ACCESS_VALUE, "expires_at": time.time() + 3000,
                            "refresh_token": REFRESH_VALUE, "scopes": config.SCOPES})
    fake = FakeHealth(fixtures.data())
    monkeypatch.setattr("requests.Session", lambda: fake)
    rc = cli.main(["--out-dir", str(env / "daily"), "backfill", "--from", "2026-10-19", "--to", "2026-10-21"])
    assert rc == 0
    assert sorted(p.name for p in (env / "daily").iterdir()) == ["2026-10-19.json", "2026-10-20.json", "2026-10-21.json"]
    assert ACCESS_VALUE not in "".join(capsys.readouterr())
