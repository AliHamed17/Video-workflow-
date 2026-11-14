"""The cloud control room, end to end over real HTTP: worker sync → cloud dashboard → commands back."""

import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

from video_workflow.cloud.app import make_cloud_handler
from video_workflow.cloud.deploy import Vercel, bundle, update_env_file
from video_workflow.cloud.sync import CloudClient, CloudError, CloudSync
from video_workflow.llm import LLMRouter
from video_workflow.models import Stage
from video_workflow.pipeline import Pipeline
from video_workflow.store import Store
from video_workflow.video import Editor
from video_workflow.video.mock import MockGenerator

from .conftest import FakeProvider, default_responses, requires_ffmpeg


@pytest.fixture
def cloud(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_cloud_handler(
        db_url=str(tmp_path / "cloud.sqlite3"), token="pw", secret="s3cret"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@requires_ffmpeg
def test_worker_sync_dashboard_and_commands(config, tmp_path, cloud):
    pipe = Pipeline(config=config, store=Store(tmp_path / "data"),
                    router=LLMRouter(config.llm, providers={"fake": FakeProvider(default_responses())}),
                    generator=MockGenerator(width=360, height=640),
                    editor=Editor(config.video, width=360, height=640))
    edited = pipe.advance(pipe.approve(pipe.develop(pipe.ideate(1)[0])))
    waiting = pipe.develop(pipe.ideate(1)[0])
    assert edited.stage == Stage.EDITED and waiting.stage == Stage.REVIEWED

    sync = CloudSync(pipe, CloudClient(cloud, "s3cret"))
    report = sync.sync()
    assert report["items"] == 2 and report["events"] > 10 and report["media"] >= 2
    assert sync.sync()["media"] == 0  # unchanged media is not uploaded twice

    with httpx.Client(base_url=cloud, timeout=20, follow_redirects=False) as web:
        assert web.get("/healthz").json()["secret"] is True
        assert web.get("/").status_code == 303  # → /login
        assert web.post("/login", data={"token": "nope", "next": "/"}).status_code == 401
        login = web.post("/login", data={"token": "pw", "next": "/"})
        assert login.status_code == 303 and "vw_token" in login.headers["set-cookie"]
        web.cookies.set("vw_token", "pw")
        board = web.get("/")
        assert board.status_code == 200 and edited.idea.title.split()[0] in board.text and "Worker synced" in board.text
        page = web.get(f"/item/{edited.id}")
        assert f"/media/{edited.id}/final.mp4" in page.text and f"/media/{edited.id}/thumbnail.jpg" in page.text
        video = web.get(f"/media/{edited.id}/final.mp4", headers={"Range": "bytes=0-9"})
        assert video.status_code == 206 and len(video.content) == 10
        for path in ("/activity", "/workflow", "/metrics", "/health", "/selftest"):
            assert web.get(path).status_code == 200, path

        # You act in the cloud; the worker applies it.
        assert web.post(f"/item/{waiting.id}/approve").status_code == 303
        assert web.post(f"/item/{edited.id}/reject").status_code == 303
        assert web.post("/ideas", data={"count": "2", "topic": "deep sea"}).status_code == 303
        assert pipe.store.get(waiting.id).stage == Stage.REVIEWED  # nothing happens until the worker syncs
        assert sync.poll() == 3
        assert pipe.store.get(waiting.id).stage == Stage.APPROVED
        assert pipe.store.get(edited.id).stage == Stage.REJECTED
        assert pipe.store.kv_get("pending_requests") == [{"ideas": 2, "seeds": ["deep sea"], "url": None}]
        health = web.get("/health").text
        assert "Requests sent to the worker" in health and health.count(">done<") == 3
        sync.sync()
        assert "rejected" in web.get(f"/item/{edited.id}").text

    assert {e["step"] for e in pipe.store.events(step_prefix="cloud.")} >= {"cloud.sync", "cloud.approve",
                                                                            "cloud.reject", "cloud.ideas"}


def test_worker_api_requires_the_secret(cloud):
    with pytest.raises(CloudError, match="401"):
        CloudClient(cloud, "wrong").commands()
    assert httpx.get(f"{cloud}/api/cloud/commands", timeout=10).status_code == 401


def test_deploy_bundle_and_rest_calls(tmp_path):
    from pathlib import Path

    files = dict(bundle(Path(".")))
    assert {"api/index.py", "vercel.json", "requirements.txt", "cloud/public/robots.txt"} <= set(files)
    assert "src/video_workflow/cloud/app.py" in files and not any("__pycache__" in f for f in files)
    assert not any(f.startswith(("tests/", "data/", ".env")) for f in files)

    calls = []

    def handler(req):
        calls.append((req.method, req.url.path, dict(req.url.params)))
        if req.url.path == "/v9/projects/vw":
            return httpx.Response(200, json={"id": "prj_1", "name": "vw", "targets": {
                "production": {"alias": ["vw-team.vercel.app", "vw.vercel.app"]}}})
        if req.url.path == "/v9/projects/prj_1/env":
            return httpx.Response(200, json={"envs": [{"key": "POSTGRES_URL"}]})
        if req.url.path == "/v13/deployments" and req.method == "POST":
            return httpx.Response(200, json={"id": "dpl_1", "url": "vw-abc.vercel.app", "readyState": "BUILDING"})
        if req.url.path == "/v13/deployments/dpl_1":
            return httpx.Response(200, json={"id": "dpl_1", "url": "vw-abc.vercel.app", "readyState": "READY"})
        return httpx.Response(200, json={})

    from video_workflow.cloud.deploy import deploy
    vercel = Vercel(None, "team_1", http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=x\nVW_CLOUD_URL=old\n")
    result = deploy(Path("."), "vw", vercel, env, {"DASHBOARD_TOKEN": None, "VW_CLOUD_SECRET": None},
                    on_progress=lambda s: None)
    assert result["url"] == "https://vw.vercel.app" and result["created_secrets"] == ["DASHBOARD_TOKEN", "VW_CLOUD_SECRET"]
    text = env.read_text()
    assert "OPENAI_API_KEY=x" in text and "VW_CLOUD_URL=https://vw.vercel.app" in text and "VW_CLOUD_SECRET=" in text
    assert all(c[2].get("teamId") == "team_1" for c in calls)
    assert sum(1 for c in calls if c[1] == "/v2/files") == len(bundle(Path(".")))
    update_env_file(env, {"VW_CLOUD_URL": "https://new"})
    assert env.read_text().count("VW_CLOUD_URL=") == 1
