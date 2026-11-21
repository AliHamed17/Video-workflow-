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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from .. import net
from ..models import ContentItem

log = logging.getLogger(__name__)

SYNC_KV = ("daemon_heartbeat", "higgsfield_balance", "health_last", "selftest_last", "content_plan",
           "viral_last", "viral_running", "autopilot_paused", "readiness", "trend_radar", "trends_running")
# What a fresh worker needs to pick up where the last one stopped (`vw cloud restore`).
RESTORE_KV = ("content_plan", "viral_last", "viral_used", "last_viral", "last_plan", "last_digest", "last_stats",
              "autopilot_paused", "higgsfield_balance", "alerts_sent", "token_vault", "trend_radar")
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

    def export(self) -> dict:
        resp = net.send(self.http, "GET", f"{self.url}/api/cloud/export",
                        headers=self._headers(**{"Accept-Encoding": "gzip"}))
        return self._check(resp, "export").json()

    def put_media(self, item_id: str, name: str, content_type: str, sha: str, data: bytes) -> None:
        resp = net.send(self.http, "PUT", f"{self.url}/api/cloud/media/{item_id}/{name}",
                        params={"type": content_type, "sha": sha}, content=data,
                        headers=self._headers(**{"Content-Type": "application/octet-stream"}))
        self._check(resp, f"media {item_id}/{name}")


def build_snapshot(store, config, after_event_id: int = 0, item_limit: int = 150) -> dict:
    items = sorted(store.list(), key=lambda i: i.updated_at, reverse=True)
    kv = {key: value for key in dict.fromkeys(SYNC_KV + RESTORE_KV) if (value := store.kv_get(key)) is not None}
    if "selftest_last" in kv:
        kv["selftest_last"] = {**kv["selftest_last"], "video": None}
    kv["config"] = config.model_dump(mode="json")
    events = list(reversed(store.events(after_id=after_event_id, limit=2000)))
    current = {i.id for i in items}
    pushed = set(store.kv_get("cloud_pushed_ids", []))
    return {
        "worker": "vw",
        "generated_at": datetime.now(UTC).isoformat(),
        "items": [i.model_dump(mode="json") for i in items[:item_limit]],
        "deleted_ids": sorted(pushed - current),   # only what this worker itself removed
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
    for version in item.versions:
        for path, ctype in ((version.final_video, "video/mp4"), (version.thumbnail, "image/jpeg")):
            if path and Path(path).exists():
                out.append((Path(path).name, Path(path), ctype))
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


COMMAND_PATIENCE = timedelta(days=7)   # how long a command waits for a worker that has its item


def _not_here(cmd: dict, what: str) -> tuple[bool, str] | None:
    """A command about something this worker doesn't have: leave it queued for one that does
    (e.g. the real worker, after a restore) — but not forever."""
    try:
        queued = datetime.fromisoformat(cmd.get("at") or "")
    except ValueError:
        queued = datetime.now(UTC)
    if datetime.now(UTC) - queued < COMMAND_PATIENCE:
        return None
    return False, f"{what} not found on any worker within {COMMAND_PATIENCE.days} days"


def apply_command(pipeline, cmd: dict) -> tuple[bool, str] | None:
    """Run one command from the cloud dashboard on this worker.

    Returns (ok, detail), or None when the command is about an item / video this worker doesn't
    have: it then stays queued for the worker that does.
    """
    action, store = cmd["action"], pipeline.store
    try:
        if action == "ideas":
            args = cmd.get("args") or {}
            requests = store.kv_get("pending_requests", [])
            requests.append({"ideas": int(args.get("count", 1)), "seeds": [args["topic"]] if args.get("topic") else [],
                             "url": args.get("url") or None})
            store.kv_set("pending_requests", requests)
            return True, f"queued {args.get('count', 1)} idea(s)"
        if action == "viral":
            from ..web.server import run_viral

            args = cmd.get("args") or {}
            if args.get("op") not in ("scout", "remix", "add"):
                return False, f"unknown viral op {args.get('op')}"
            if args["op"] == "remix":
                known = {f"{v['platform']}:{v['id']}" for v in (store.kv_get("viral_last") or {}).get("videos", [])}
                if args.get("key") not in known:
                    return _not_here(cmd, f"viral video {args.get('key')}")
            before = {i.id for i in store.list()}
            error = run_viral(pipeline, args)
            if error:
                return False, f"viral {args['op']} failed: {error}"
            made = [i for i in store.list() if i.id not in before]
            last = store.kv_get("viral_last") or {}
            return True, (f"viral {args['op']} done · " + (f"our version {made[0].id}: {made[0].idea.title[:60]}"
                                                           if made else f"{len(last.get('videos', []))} videos listed"))
        if action == "trends":
            from ..web.server import run_trends

            error = run_trends(pipeline)
            if error:
                return False, f"trend radar failed: {error}"
            radar = store.kv_get("trend_radar") or {}
            top = radar.get("topics") or []
            return True, f"{len(top)} trend topics" + (f"; top: {top[0]['topic'][:60]}" if top else "")
        if action == "autopilot":
            paused = bool((cmd.get("args") or {}).get("paused"))
            pipeline.set_autopilot_paused(paused, by="cloud dashboard")
            return True, "autopilot paused" if paused else "autopilot resumed"
        if action == "plan":
            days = pipeline.plan(int((cmd.get("args") or {}).get("days", 7)))
            return True, f"planned through {days[-1]['date'] if days else '?'}"
        try:
            item = store.get(cmd["item_id"])
        except KeyError:
            return _not_here(cmd, f"item {cmd['item_id']}")
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


TOKEN_FILES = ("tiktok.token.json", "instagram.token.json")  # tokens the publishers rotate themselves


def _vault_key(secret: str) -> bytes:
    return hashlib.sha256(b"video-workflow token vault v1|" + secret.encode()).digest()


def seal_tokens(data_dir: Path, secret: str) -> dict | None:
    """Encrypt rotated platform tokens so a fresh machine can continue with them.

    The cloud only ever stores ciphertext; the key comes from VW_CLOUD_SECRET, which the
    Vercel app checks but never uses for this.
    """
    files = {n: (data_dir / n).read_text() for n in TOKEN_FILES if (data_dir / n).exists()}
    if not files:
        return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        log.warning("cryptography is not installed: rotated tokens are not carried to other machines")
        return None
    import base64
    import os

    nonce = os.urandom(12)
    sealed = AESGCM(_vault_key(secret)).encrypt(nonce, json.dumps(files).encode(), b"tokens")
    return {"v": 1, "nonce": base64.b64encode(nonce).decode(), "data": base64.b64encode(sealed).decode()}


def open_tokens(vault: dict, data_dir: Path, secret: str) -> list[str]:
    """Write the vault's token files that this machine doesn't have yet. Returns their names."""
    import base64

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    files = json.loads(AESGCM(_vault_key(secret)).decrypt(
        base64.b64decode(vault["nonce"]), base64.b64decode(vault["data"]), b"tokens"))
    written = []
    for name, content in files.items():
        path = data_dir / Path(name).name
        if name in TOKEN_FILES and not path.exists():
            path.write_text(content)
            path.chmod(0o600)
            written.append(name)
    return written


class CloudSync:
    """Keeps one worker's state mirrored to the cloud app and applies the commands sent from it."""

    def __init__(self, pipeline, client: CloudClient):
        self.pipeline = pipeline
        self.client = client
        self.store = pipeline.store

    def apply(self, commands: list[dict]) -> list[dict]:
        results = []
        seen = set(self.store.kv_get("cloud_skipped", []))
        for cmd in commands:
            outcome = apply_command(self.pipeline, cmd)
            if outcome is None:   # not this worker's: leave it queued, note it once
                if cmd["id"] not in seen:
                    seen.add(cmd["id"])
                    self.pipeline.tracer.event(f"cloud.{cmd['action']}", "skipped", cmd.get("item_id"),
                                               "left in the queue: this worker doesn't have that item/video yet")
                continue
            ok, detail = outcome
            self.pipeline.tracer.event(f"cloud.{cmd['action']}", "ok" if ok else "error", cmd.get("item_id"),
                                       f"from {cmd.get('source', 'cloud')}: {detail}")
            results.append({"id": cmd["id"], "ok": ok, "detail": detail})
        self.store.kv_set("cloud_skipped", sorted(seen)[-500:])
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
            vault = seal_tokens(self.store.data_dir, self.client.secret)
            if vault:
                snapshot["kv"]["token_vault"] = vault
            resp = self.client.sync(snapshot)
            self._pushed(snapshot)
            uploaded = self._upload_media(resp.get("media", {}))
            applied = self.apply(resp.get("commands", []))
            if applied:  # push the effect of the commands right away (e.g. the pause state)
                again = build_snapshot(self.store, self.pipeline.config, self.store.kv_get("cloud_last_event", 0))
                self.client.sync(again)
                self._pushed(again)
            self.store.kv_set("sync", {"at": datetime.now(UTC).isoformat(), "url": self.client.url})
            span.detail = (f"{len(snapshot['items'])} items, {len(snapshot['events'])} events, "
                           f"{uploaded} media, {len(applied)} command(s)")
        return {"items": len(snapshot["items"]), "events": len(snapshot["events"]), "media": uploaded,
                "commands": len(applied)}

    def _pushed(self, snapshot: dict) -> None:
        if snapshot["events"]:
            self.store.kv_set("cloud_last_event", snapshot["events"][-1]["id"])
        pushed = set(self.store.kv_get("cloud_pushed_ids", [])) - set(snapshot["deleted_ids"])
        self.store.kv_set("cloud_pushed_ids", sorted(pushed | {i["id"] for i in snapshot["items"]}))

    def restore(self) -> dict:
        """Continue on a fresh machine: pull open + recent items, state and today's trace from the cloud.

        Items this machine already has are kept as they are. Clips are fetched again from their
        Higgsfield URLs; an unfinished video whose files can't be recovered is flagged for you.
        """
        from ..models import ContentItem, Stage
        from ..video.base import download

        with self.pipeline.tracer.span("cloud.restore", None) as span:
            data = self.client.export()
            local = {i.id for i in self.store.list()}
            restored, refetched, flagged = 0, 0, 0
            for raw in data.get("items", []):
                if raw["id"] in local:
                    continue
                item = ContentItem.model_validate(raw)
                folder = self.store.item_dir(item.id)
                for clip in item.clips:
                    clip.path = str(folder / "clips" / Path(clip.path).name) if clip.path else clip.path
                    if clip.path and not Path(clip.path).exists() and clip.url:
                        try:
                            download(clip.url, Path(clip.path), self.pipeline.http)
                            refetched += 1
                        except Exception as exc:
                            log.warning("could not re-fetch %s shot %d: %s", item.id, clip.shot_index, exc)
                for attr in ("final_video", "thumbnail"):
                    value = getattr(item, attr)
                    if value and not Path(value).exists():
                        setattr(item, attr, None)
                missing = [c for c in item.clips if c.path and not Path(c.path).exists()]
                if item.stage in (Stage.RENDERED, Stage.EDITED) and (missing or not item.final_video):
                    if not missing:
                        item.stage = Stage.RENDERED   # clips are back: edit again on this machine
                    else:
                        item.attention = (f"Restored on a new machine without {len(missing)} clip file(s); "
                                          "re-render those shots or retry.")
                        flagged += 1
                self.store.save(item)
                restored += 1
            kv = dict(data.get("kv") or {})
            tokens: list[str] = []
            if kv.get("token_vault"):
                try:
                    tokens = open_tokens(kv.pop("token_vault"), self.store.data_dir, self.client.secret)
                except Exception as exc:  # wrong secret / tampered data: keep going without them
                    log.warning("could not open the token vault: %s", exc)
            for key, value in kv.items():
                if self.store.kv_get(key) is None:
                    self.store.kv_set(key, value)
            self.store.import_events(data.get("events", []), int(data.get("last_event_id") or 0))
            self.store.kv_set("cloud_last_event", int(data.get("last_event_id") or 0))
            self.store.kv_set("cloud_pushed_ids", sorted(set(self.store.kv_get("cloud_pushed_ids", []))
                                                         | {i["id"] for i in data.get("items", [])}))
            report = {"items": restored, "clips_refetched": refetched, "flagged": flagged,
                      "events": len(data.get("events", [])), "state": sorted(kv), "tokens": tokens}
            span.detail = f"{restored} items, {refetched} clips re-fetched, {flagged} flagged"
        return report

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
