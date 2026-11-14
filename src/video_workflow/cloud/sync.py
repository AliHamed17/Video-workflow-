"""Worker side of the cloud control room.

Every sync: push a snapshot (items, new trace events, heartbeat, balance, health, config) →
upload changed media (thumbnail, keyframes, QA frames, a compact web preview of each video) →
apply the commands you issued in the cloud dashboard → acknowledge them.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .. import net
from ..models import ContentItem

log = logging.getLogger(__name__)

SYNC_KV = ("daemon_heartbeat", "higgsfield_balance", "health_last", "selftest_last")
MEDIA_ITEMS = 40              # media is kept for the most recent items
MAX_UPLOAD = 4_300_000        # stay under Vercel's 4.5 MB request limit


class CloudError(RuntimeError):
    pass


class CloudClient:
    def __init__(self, url: str, secret: str, http: httpx.Client | None = None):
        self.url = url.rstrip("/")
        self.secret = secret
        self.http = http or net.default_client()

    def _headers(self, **extra) -> dict:
        return {"Authorization": f"Bearer {self.secret}", **extra}

    def _check(self, resp: httpx.Response, what: str) -> httpx.Response:
        if resp.status_code >= 400:
            raise CloudError(f"{what}: HTTP {resp.status_code} {resp.text[:300]}")
        return resp

    def sync(self, snapshot: dict) -> dict:
        body = gzip.compress(json.dumps(snapshot, default=str).encode())
        resp = net.send(self.http, "POST", f"{self.url}/api/cloud/sync", content=body,
                        headers=self._headers(**{"Content-Type": "application/gzip"}))
        return self._check(resp, "sync").json()

    def commands(self) -> list[dict]:
        resp = net.send(self.http, "GET", f"{self.url}/api/cloud/commands", headers=self._headers())
        return self._check(resp, "commands").json().get("commands", [])

    def ack(self, results: list[dict]) -> None:
        if results:
            resp = net.send(self.http, "POST", f"{self.url}/api/cloud/ack", json={"results": results},
                            headers=self._headers())
            self._check(resp, "ack")

    def put_media(self, item_id: str, name: str, content_type: str, sha: str, data: bytes) -> None:
        resp = net.send(self.http, "PUT", f"{self.url}/api/cloud/media/{item_id}/{name}",
                        params={"type": content_type, "sha": sha}, content=data,
                        headers=self._headers(**{"Content-Type": "application/octet-stream"}))
        self._check(resp, f"media {item_id}/{name}")


def build_snapshot(store, config, after_event_id: int = 0, item_limit: int = 150) -> dict:
    items = sorted(store.list(), key=lambda i: i.updated_at, reverse=True)
    kv = {key: store.kv_get(key) for key in SYNC_KV if store.kv_get(key) is not None}
    if "selftest_last" in kv:
        kv["selftest_last"] = {**kv["selftest_last"], "video": None}
    kv["config"] = config.model_dump(mode="json")
    events = list(reversed(store.events(after_id=after_event_id, limit=2000)))
    return {
        "worker": "vw",
        "generated_at": datetime.now(UTC).isoformat(),
        "items": [i.model_dump(mode="json") for i in items[:item_limit]],
        "item_ids": [i.id for i in items],
        "events": events,
        "kv": kv,
    }


def _sha(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def media_files(item: ContentItem) -> list[tuple[str, Path, str]]:
    """(name, local path, content type) of everything the cloud dashboard shows for an item."""
    out: list[tuple[str, Path, str]] = []
    if item.thumbnail and Path(item.thumbnail).exists():
        out.append((Path(item.thumbnail).name, Path(item.thumbnail), "image/jpeg"))
    if item.final_video and Path(item.final_video).exists():
        out.append((Path(item.final_video).name, Path(item.final_video), "video/mp4"))
    for clip in item.clips:
        if clip.keyframe and Path(clip.keyframe).exists():
            out.append((Path(clip.keyframe).name, Path(clip.keyframe), "image/jpeg"))
    for review in item.shot_reviews:
        for frame in review.frames:
            if Path(frame).exists():
                out.append((Path(frame).name, Path(frame), "image/jpeg"))
    seen, unique = set(), []
    for entry in out:
        if entry[0] not in seen:
            seen.add(entry[0])
            unique.append(entry)
    return unique


def web_preview(src: Path, dest: Path) -> Path:
    """A small phone-friendly copy of a video for the cloud dashboard (≈1 MB for 30 s)."""
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vf", "scale=-2:640",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-c:a", "aac", "-b:a", "64k",
                    "-movflags", "+faststart", str(dest)], check=True)
    return dest


def apply_command(pipeline, cmd: dict) -> tuple[bool, str]:
    """Run one command from the cloud dashboard on this worker."""
    action, store = cmd["action"], pipeline.store
    try:
        if action == "ideas":
            args = cmd.get("args") or {}
            requests = store.kv_get("pending_requests", [])
            requests.append({"ideas": int(args.get("count", 1)), "seeds": [args["topic"]] if args.get("topic") else []})
            store.kv_set("pending_requests", requests)
            return True, f"queued {args.get('count', 1)} idea(s)"
        item = store.get(cmd["item_id"])
        if action == "approve":
            pipeline.approve(item, note="approved in the cloud dashboard")
        elif action == "final":
            pipeline.approve_final(item, note="final approved in the cloud dashboard")
        elif action == "reject":
            pipeline.reject(item, note="rejected in the cloud dashboard")
        elif action == "retry":
            pipeline.retry(item)
        elif action == "rerender":
            args = cmd.get("args") or {}
            pipeline.rerender(item, [int(s) for s in args.get("shots", [])], args.get("note", ""))
        elif action == "publish":
            item = pipeline.publish(item, force=True)
            return True, "published to " + (", ".join(sorted(item.published_platforms())) or "nothing yet")
        else:
            return False, f"unknown action {action}"
        return True, f"{action} applied to {item.id}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


class CloudSync:
    """Keeps one worker's state mirrored to the cloud app and applies the commands sent from it."""

    def __init__(self, pipeline, client: CloudClient):
        self.pipeline = pipeline
        self.client = client
        self.store = pipeline.store

    def apply(self, commands: list[dict]) -> list[dict]:
        results = []
        for cmd in commands:
            ok, detail = apply_command(self.pipeline, cmd)
            self.pipeline.tracer.event(f"cloud.{cmd['action']}", "ok" if ok else "error", cmd.get("item_id"),
                                       f"from {cmd.get('source', 'cloud')}: {detail}")
            results.append({"id": cmd["id"], "ok": ok, "detail": detail})
        self.client.ack(results)
        return results

    def poll(self) -> int:
        """Fast path: just apply pending commands (no full sync)."""
        commands = self.client.commands()
        return len(self.apply(commands)) if commands else 0

    def sync(self) -> dict:
        with self.pipeline.tracer.span("cloud.sync", None) as span:
            last = self.store.kv_get("cloud_last_event", 0)
            snapshot = build_snapshot(self.store, self.pipeline.config, last)
            resp = self.client.sync(snapshot)
            if snapshot["events"]:
                self.store.kv_set("cloud_last_event", snapshot["events"][-1]["id"])
            uploaded = self._upload_media(resp.get("media", {}))
            applied = self.apply(resp.get("commands", []))
            self.store.kv_set("sync", {"at": datetime.now(UTC).isoformat(), "url": self.client.url})
            span.detail = (f"{len(snapshot['items'])} items, {len(snapshot['events'])} events, "
                           f"{uploaded} media, {len(applied)} command(s)")
        return {"items": len(snapshot["items"]), "events": len(snapshot["events"]), "media": uploaded,
                "commands": len(applied)}

    def _upload_media(self, remote: dict[str, str]) -> int:
        uploaded = 0
        recent = sorted(self.store.list(), key=lambda i: i.updated_at, reverse=True)[:MEDIA_ITEMS]
        for item in recent:
            for name, path, ctype in media_files(item):
                sha = _sha(path)
                if remote.get(f"{item.id}/{name}") == sha:
                    continue
                data_path = path
                if ctype == "video/mp4":
                    cache = self.store.item_dir(item.id) / "cloud"
                    cache.mkdir(parents=True, exist_ok=True)
                    data_path = web_preview(path, cache / name)
                data = data_path.read_bytes()
                if len(data) > MAX_UPLOAD:
                    log.warning("skipping %s/%s: %d bytes is too large for the cloud", item.id, name, len(data))
                    continue
                self.client.put_media(item.id, name, ctype, sha, data)
                uploaded += 1
        return uploaded
