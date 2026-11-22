"""Accounts connected on the cloud dashboard: sealed storage, the Connect page, Connect with Google, and
the worker picking them up (sync, restore, startup, a running daemon)."""

import threading
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import SecretStr

from video_workflow import connections
from video_workflow.cloud.app import make_cloud_handler
from video_workflow.cloud.db import DB
from video_workflow.cloud.store import CloudStore
from video_workflow.cloud.sync import CloudClient, CloudSync
from video_workflow.config import AppConfig, Secrets
from video_workflow.factory import build_pipeline, refresh_connections
from video_workflow.llm import LLMRouter
from video_workflow.store import Store

from .conftest import FakeProvider, default_responses

SECRET = "s3cret-cloud"


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    """No real .env and no real credentials leak into these tests."""
    monkeypatch.chdir(tmp_path)
    for name in list(Secrets.model_fields):
        monkeypatch.delenv(name.upper(), raising=False)
    return tmp_path


def test_values_are_sealed_and_only_known_fields_are_kept(tmp_path):
    store = Store(tmp_path)
    vault = connections.update(store, SECRET, {"youtube_client_id": "123-abc.apps.googleusercontent.com",
                                               "youtube_client_secret": "GOCSPX-supersecret", "tiktok_client_key": "  "})
    assert "supersecret" not in str(vault) and vault["fields"] == ["youtube_client_id", "youtube_client_secret"]
    assert connections.unseal(vault, SECRET)["youtube_client_secret"] == "GOCSPX-supersecret"
    from cryptography.exceptions import InvalidTag
    with pytest.raises(InvalidTag):
        connections.unseal(vault, "wrong secret")
    with pytest.raises(ValueError, match="unknown fields"):
        connections.update(store, SECRET, {"vw_cloud_secret": "x"})            # never storable here
    connections.update(store, SECRET, {"youtube_client_secret": None})
    assert connections.unseal(store.kv_get(connections.KV_KEY), SECRET) == {
        "youtube_client_id": "123-abc.apps.googleusercontent.com"}
    status = connections.status(store.kv_get(connections.KV_KEY), SECRET)
    assert status["youtube_client_id"] == {"source": "dashboard", "hint": "nt.com"}
    assert status["youtube_client_secret"]["source"] is None


def test_the_environment_wins_and_the_dashboard_fills_the_gaps(clean_env, monkeypatch):
    store = Store(clean_env / "data")
    connections.update(store, SECRET, {"instagram_access_token": "IG-dashboard", "instagram_user_id": "1784",
                                       "telegram_bot_token": "TG-dashboard"})
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TG-environment")
    assert connections.load_secrets(store).instagram_access_token is None             # no secret, no vault
    monkeypatch.setenv("VW_CLOUD_SECRET", SECRET)
    secrets = connections.load_secrets(store)
    assert isinstance(secrets.instagram_access_token, SecretStr)
    assert secrets.instagram_access_token.get_secret_value() == "IG-dashboard" and secrets.instagram_user_id == "1784"
    assert secrets.telegram_bot_token.get_secret_value() == "TG-environment"
    monkeypatch.setenv("VW_CLOUD_SECRET", "a different secret")
    assert connections.load_secrets(store).instagram_access_token is None             # can't open: env only


def _cloud(tmp_path, secret=SECRET, http=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_cloud_handler(
        db_url=str(tmp_path / "cloud.sqlite3"), token="pw", secret=secret, http=http))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_the_connect_page_saves_without_ever_showing_a_secret(tmp_path):
    server, base = _cloud(tmp_path)
    try:
        with httpx.Client(base_url=base, timeout=20, cookies={"vw_token": "pw"}, follow_redirects=True) as web:
            page = web.get("/connect").text
            assert "Connect your accounts" in page and "Connect with Google" in page and "missing" in page
            # The redirect URI to register (the cloud dashboard is always served over https on Vercel).
            assert f"{base.replace('http://', 'https://')}/connect/youtube/callback" in page
            saved = web.post("/connect/save", data={"service": "instagram", "instagram_access_token": "IGAAsecretvalue",
                                                    "instagram_user_id": "17841400000000"})
            assert "Instagram Reels: saved 2 field(s)" in saved.text
            assert "IGAAsecretvalue" not in saved.text and "…000000" in saved.text      # ids hinted, secrets never
            store = CloudStore(DB(str(tmp_path / "cloud.sqlite3")))
            assert "IGAAsecretvalue" not in str(store.kv_get("connections_vault"))
            assert web.post("/connect/save", data={"service": "nope"}).text.count("Not saved: unknown service") == 1
            removed = web.post("/connect/remove", data={"service": "instagram"})
            assert "Instagram Reels: removed." in removed.text
        assert httpx.post(f"{base}/connect/save", data={"service": "telegram", "telegram_bot_token": "x"}).status_code == 401
    finally:
        server.shutdown()
        server.server_close()

    server, base = _cloud(tmp_path / "nosecret", secret="")
    try:
        with httpx.Client(base_url=base, timeout=20, cookies={"vw_token": "pw"}, follow_redirects=True) as web:
            assert "Set VW_CLOUD_SECRET on the Vercel project" in web.get("/connect").text
            refused = web.post("/connect/save", data={"service": "telegram", "telegram_bot_token": "x"})
            assert "Not saved: accounts are connected on the cloud dashboard" in refused.text
    finally:
        server.shutdown()
        server.server_close()


def test_connect_with_google(tmp_path):
    exchanges = []

    def google(req):
        form = parse_qs(req.content.decode())
        exchanges.append(form)
        if form["code"] == ["no-refresh"]:
            return httpx.Response(200, json={"access_token": "a"})
        if form["code"] == ["bad"]:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad Request"})
        return httpx.Response(200, json={"access_token": "a", "refresh_token": "1//refresh-from-google"})

    server, base = _cloud(tmp_path, http=httpx.Client(transport=httpx.MockTransport(google)))
    store = CloudStore(DB(str(tmp_path / "cloud.sqlite3")))
    try:
        with httpx.Client(base_url=base, timeout=20, cookies={"vw_token": "pw"}) as web:
            early = web.get("/connect/youtube/start")
            assert "save+the+OAuth+client+ID" in early.headers["location"].replace("%20", "+")
            web.post("/connect/save", data={"service": "youtube", "youtube_client_id": "cid.apps",
                                            "youtube_client_secret": "csecret"})
            start = web.get("/connect/youtube/start")
            google_url = urlparse(start.headers["location"])
            params = parse_qs(google_url.query)
            assert google_url.netloc == "accounts.google.com" and params["client_id"] == ["cid.apps"]
            callback = f"{base.replace('http://', 'https://')}/connect/youtube/callback"
            assert params["redirect_uri"] == [callback] and params["access_type"] == ["offline"]
            assert "youtube.force-ssl" in params["scope"][0]
            state = params["state"][0]
            forged = web.get("/connect/youtube/callback", params={"code": "c", "state": "attacker"})
            assert "expired" in forged.headers["location"] and not exchanges            # nothing exchanged
            web.get("/connect/youtube/start")
            state = parse_qs(urlparse(web.get("/connect/youtube/start").headers["location"]).query)["state"][0]
            done = web.get("/connect/youtube/callback", params={"code": "good", "state": state})
            assert "YouTube+connected" in done.headers["location"].replace("%20", "+")
            assert exchanges[-1]["client_secret"] == ["csecret"] and exchanges[-1]["grant_type"] == ["authorization_code"]
            again = web.get("/connect/youtube/callback", params={"code": "good", "state": state})
            assert "expired" in again.headers["location"]                              # a state works once
            state = parse_qs(urlparse(web.get("/connect/youtube/start").headers["location"]).query)["state"][0]
            empty = web.get("/connect/youtube/callback", params={"code": "no-refresh", "state": state})
            assert "no+refresh+token" in empty.headers["location"].replace("%20", "+")
        assert connections.unseal(store.kv_get("connections_vault"), SECRET)["youtube_refresh_token"] == \
            "1//refresh-from-google"
    finally:
        server.shutdown()
        server.server_close()


def test_the_worker_gets_them_with_sync_and_restore_and_uses_them(clean_env, monkeypatch, config):
    monkeypatch.setenv("VW_CLOUD_SECRET", SECRET)
    server, base = _cloud(clean_env)
    try:
        with httpx.Client(base_url=base, timeout=20, cookies={"vw_token": "pw"}) as web:
            web.post("/connect/save", data={"service": "youtube", "youtube_client_id": "cid", "youtube_client_secret": "cs",
                                            "youtube_refresh_token": "rt"})
        config.publish.platforms = ["youtube", "instagram"]
        pipe = build_pipeline(config, None, clean_env / "worker")
        pipe.router = LLMRouter(config.llm, pipe.secrets, providers={"fake": FakeProvider(default_responses())})
        assert pipe.publishers == {}                                               # nothing connected yet
        sync = CloudSync(pipe, CloudClient(base, SECRET))
        sync.sync()
        assert pipe.store.kv_get("connections_vault")
        assert refresh_connections(pipe) == ["youtube_client_id", "youtube_client_secret", "youtube_refresh_token"]
        assert set(pipe.publishers) == {"youtube"} and "fake" in pipe.router._providers   # injected ones kept
        assert refresh_connections(pipe) == []                                         # nothing new

        fresh = build_pipeline(config, None, clean_env / "fresh-machine")
        assert fresh.publishers == {}
        report = CloudSync(fresh, CloudClient(base, SECRET)).restore()
        assert report["connections"] == ["youtube_client_id", "youtube_client_secret", "youtube_refresh_token"]
        rebuilt = build_pipeline(config, None, clean_env / "fresh-machine")
        assert set(rebuilt.publishers) == {"youtube"}
    finally:
        server.shutdown()
        server.server_close()


def test_the_local_dashboard_shows_what_the_worker_has(clean_env, monkeypatch, config):
    from video_workflow.web.server import make_handler

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("VW_CLOUD_URL", "https://example.vercel.app")
    pipe = build_pipeline(AppConfig.model_validate(config.model_dump()), None, clean_env / "data")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(pipe))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        page = httpx.get(f"http://127.0.0.1:{server.server_address[1]}/connect", timeout=20).text
        assert "✓ environment" in page and "https://example.vercel.app/connect" in page
        assert "<form method=post action='/connect/save'>" not in page                 # edited on the cloud only
    finally:
        server.shutdown()
        server.server_close()


def test_the_cloud_app_can_seal_on_vercel():
    """The Connect page encrypts on Vercel, so the cloud app's requirements must include cryptography."""
    from pathlib import Path

    requirements = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text()
    assert any(line.split(">=")[0].strip() == "cryptography" for line in requirements.splitlines())
