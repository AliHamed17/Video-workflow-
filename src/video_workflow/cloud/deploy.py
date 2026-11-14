"""`vw cloud deploy`: ship the cloud control room to Vercel through the Vercel REST API.

1. Find or create the Vercel project.
2. Make sure DASHBOARD_TOKEN and VW_CLOUD_SECRET exist on it (generated once, also written to
   the local .env so this worker can sync).
3. Upload the bundle (api/index.py, vercel.json, requirements.txt, cloud/public/, the package).
4. Create a production deployment and wait until it is ready.

Auth: VERCEL_TOKEN (sent as a bearer token). In Claude Code cloud sessions the network proxy
injects a saved token instead, so the variable can stay empty.
"""

from __future__ import annotations

import hashlib
import secrets as pysecrets
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from .. import net

API = "https://api.vercel.com"
BUNDLE_ROOTS = ["api", "cloud/public", "src/video_workflow"]
BUNDLE_FILES = ["vercel.json", "requirements.txt"]
SKIP = {"__pycache__", ".pytest_cache"}


class VercelError(RuntimeError):
    pass


def bundle(repo: Path) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    for name in BUNDLE_FILES:
        files.append((name, (repo / name).read_bytes()))
    for root in BUNDLE_ROOTS:
        for path in sorted((repo / root).rglob("*")):
            if path.is_file() and not SKIP & set(path.parts) and path.suffix not in {".pyc"}:
                files.append((path.relative_to(repo).as_posix(), path.read_bytes()))
    return files


class Vercel:
    def __init__(self, token: str | None = None, team_id: str | None = None, http: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.token, self.team_id = token, team_id
        self.http = http or net.default_client()
        self.sleep = sleep

    def _req(self, method: str, path: str, *, ok404: bool = False, **kw) -> dict | None:
        params = dict(kw.pop("params", {}) or {})
        if self.team_id:
            params["teamId"] = self.team_id
        headers = dict(kw.pop("headers", {}) or {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        resp = net.send(self.http, method, f"{API}{path}", params=params, headers=headers, **kw)
        if ok404 and resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise VercelError(f"{method} {path}: HTTP {resp.status_code} {resp.text[:400]}")
        return resp.json() if resp.content else {}

    def user(self) -> dict:
        return self._req("GET", "/v2/user")["user"]

    def project(self, name: str) -> dict | None:
        return self._req("GET", f"/v9/projects/{name}", ok404=True)

    def ensure_project(self, name: str) -> dict:
        return self.project(name) or self._req("POST", "/v10/projects", json={"name": name, "framework": None})

    def env_keys(self, project_id: str) -> set[str]:
        return {e["key"] for e in self._req("GET", f"/v9/projects/{project_id}/env").get("envs", [])}

    def set_env(self, project_id: str, key: str, value: str) -> None:
        self._req("POST", f"/v10/projects/{project_id}/env", params={"upsert": "true"},
                  json={"key": key, "value": value, "type": "encrypted",
                        "target": ["production", "preview", "development"]})

    def upload(self, data: bytes) -> str:
        sha = hashlib.sha1(data).hexdigest()
        self._req("POST", "/v2/files", content=data,
                  headers={"x-vercel-digest": sha, "Content-Type": "application/octet-stream"})
        return sha

    def deploy(self, project: dict, files: list[tuple[str, bytes]], timeout_s: float = 600,
               on_progress: Callable[[str], None] = lambda s: None) -> dict:
        manifest = []
        for path, data in files:
            manifest.append({"file": path, "sha": self.upload(data), "size": len(data)})
        on_progress(f"uploaded {len(manifest)} files")
        dep = self._req("POST", "/v13/deployments", json={
            "name": project["name"], "project": project["id"], "target": "production", "files": manifest,
            "projectSettings": {"framework": None},
        })
        deadline = time.monotonic() + timeout_s
        while dep.get("readyState") not in ("READY", "ERROR", "CANCELED"):
            if time.monotonic() > deadline:
                raise VercelError(f"Deployment {dep.get('id')} still {dep.get('readyState')} after {timeout_s}s")
            self.sleep(4)
            dep = self._req("GET", f"/v13/deployments/{dep['id']}")
            on_progress(dep.get("readyState", "?"))
        if dep["readyState"] != "READY":
            raise VercelError(f"Deployment {dep['id']} {dep['readyState']}: {dep.get('errorMessage', '')}")
        return dep

    def production_url(self, project_name: str) -> str | None:
        project = self.project(project_name) or {}
        aliases = [a.get("domain") for a in (project.get("targets", {}).get("production", {}) or {}).get("alias", [])
                   if isinstance(a, dict)] or (project.get("targets", {}).get("production", {}) or {}).get("alias", [])
        aliases = [a for a in aliases if a]
        return f"https://{sorted(aliases, key=len)[0]}" if aliases else None


def update_env_file(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in values:
            lines[i] = f"{key}={values[key]}"
            seen.add(key)
    lines += [f"{k}={v}" for k, v in values.items() if k not in seen]
    path.write_text("\n".join(lines) + "\n")


def deploy(repo: Path, project_name: str, vercel: Vercel, env_file: Path, local: dict[str, str | None],
           on_progress: Callable[[str], None] = print) -> dict:
    """Returns {url, deployment_url, project_id, created_secrets}."""
    project = vercel.ensure_project(project_name)
    keys = vercel.env_keys(project["id"])
    created = {}
    for key in ("DASHBOARD_TOKEN", "VW_CLOUD_SECRET"):
        value = local.get(key)
        if value:
            vercel.set_env(project["id"], key, value)  # keep Vercel in step with this worker
            continue
        # Unknown locally (first deploy, or lost .env): mint a new one. Save it right away so a
        # failed deploy can never leave Vercel with a secret this worker doesn't know.
        value = pysecrets.token_urlsafe(24)
        vercel.set_env(project["id"], key, value)
        update_env_file(env_file, {key: value})
        created[key] = value
        if key in keys:
            on_progress(f"{key} rotated (it was set on Vercel but missing locally); update other workers")
    db_ready = bool({"POSTGRES_URL", "DATABASE_URL"} & keys)
    on_progress("database connected" if db_ready else
                "no database connected yet: add Neon Postgres to the project (Storage tab) — the app falls back "
                "to an ephemeral SQLite file until then")
    dep = vercel.deploy(project, bundle(repo), on_progress=on_progress)
    url = vercel.production_url(project_name) or f"https://{dep['url']}"
    update_env_file(env_file, {"VW_CLOUD_URL": url})
    return {"url": url, "deployment_url": f"https://{dep['url']}", "project_id": project["id"],
            "created_secrets": sorted(created)}
