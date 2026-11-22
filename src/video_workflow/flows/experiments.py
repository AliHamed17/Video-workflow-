"""Experiments: A/B tests on our own videos (see experiments.py for the statistics).

Every new Short joins the running test on the side with fewer videos; its writing, editing and posting
use that side's settings (`config_for(item)`). Once both sides have enough judged videos the test ends
with a winner or "no clear difference". A winner's settings can be adopted (`config_overrides`, applied
at every start) and undone.
"""

from __future__ import annotations

import logging
from typing import Any

from .. import experiments, growth
from ..config import AppConfig
from ..models import ContentItem
from ..notify import Notice

log = logging.getLogger(__name__)


class ExperimentFlow:
    # ---- the record -------------------------------------------------------------------------
    def experiments_list(self) -> list[dict]:
        return self.store.kv_get("experiments", []) or []

    def _save_experiments(self, records: list[dict]) -> None:
        self.store.kv_set("experiments", records[-50:])

    def running_experiment(self) -> dict | None:
        return next((e for e in self.experiments_list() if e["status"] == "running"), None)

    def start_experiment(self, key: str) -> dict:
        if key not in experiments.SPECS:
            raise ValueError(f"unknown experiment {key!r}")
        if self.running_experiment():
            raise ValueError("an experiment is already running — stop it first")
        exp = experiments.new(key, self.clock())
        self._save_experiments([*self.experiments_list(), exp])
        self.tracer.event("experiment.start", "ok", detail=f"{exp['title']}: {' vs '.join(a['name'] for a in exp['arms'])}")
        return exp

    def stop_experiment(self) -> dict | None:
        records = self.experiments_list()
        exp = next((e for e in records if e["status"] == "running"), None)
        if exp:
            exp["status"] = "stopped"
            self._save_experiments(records)
            self.tracer.event("experiment.stop", "ok", detail=exp["title"])
        return exp

    # ---- videos in a test -------------------------------------------------------------------------
    def assign_experiment(self, item: ContentItem) -> None:
        """Put a new Short on the side of the running test with fewer videos."""
        if item.kind != "short" or item.experiment:
            return
        records = self.experiments_list()
        exp = next((e for e in records if e["status"] == "running"), None)
        if exp is None:
            return
        arm = experiments.pick_arm(exp)
        exp["assigned"][item.id] = arm
        item.experiment = {"id": exp["id"], "arm": arm}
        self._save_experiments(records)
        self.tracer.event("experiment.assign", "ok", item.id, f"{exp['title']}: {arm}")

    def config_for(self, item: ContentItem) -> AppConfig:
        """The settings this video is made and posted with (its test side's overrides, if any)."""
        if not item.experiment:
            return self.config
        exp = next((e for e in self.experiments_list() if e["id"] == item.experiment["id"]), None)
        arm = next((a for a in (exp or {}).get("arms", []) if a["name"] == item.experiment["arm"]), None)
        return experiments.with_overrides(self.config, arm["overrides"]) if arm else self.config

    # ---- judging ---------------------------------------------------------------------------------
    def evaluate_experiments(self) -> dict | None:
        """Update the running test; end it when both sides have enough judged videos."""
        records = self.experiments_list()
        exp = next((e for e in records if e["status"] == "running"), None)
        cfg = self.config.experiments
        if exp is None:
            if cfg.auto_start:
                done = {e["key"] for e in records}
                nxt = next((s.key for s in experiments.CATALOG if s.key not in done), None)
                if nxt:
                    return self.start_experiment(nxt)
            return None
        rows = growth.video_rows(self.store.list(), self.clock(), self.tz, self.config.growth.mature_hours)
        result = experiments.evaluate(exp, rows, cfg)
        exp["result"] = {k: v for k, v in result.items() if k != "arms"} | {
            "arms": {n: {k: v for k, v in a.items() if k != "scores"} for n, a in result["arms"].items()}}
        if result["status"] != "collecting":
            exp["status"] = "done"
            line = (f"{result['winner']} wins: {result['lift'] * 100:+.0f}% on the median (p={result['p']})"
                    if result["winner"] else f"no clear difference yet (p={result['p']})")
            self.tracer.event("experiment.done", "ok", detail=f"{exp['title']}: {line}")
            self.notify(Notice(kind="info", title=f"Experiment finished: {exp['title']}", lines=[
                exp["question"], line,
                ("Adopt it on the Growth page (or /experiments on Telegram)." if result["winner"] and not cfg.auto_adopt
                 else "")]))
        self._save_experiments(records)
        if exp["status"] == "done" and result["winner"] and cfg.auto_adopt:
            self.adopt_experiment(exp["id"], result["winner"])
        return exp

    # ---- adopting a winner --------------------------------------------------------------------------
    def adopt_experiment(self, exp_id: str, arm_name: str) -> dict[str, Any]:
        records = self.experiments_list()
        exp = next((e for e in records if e["id"] == exp_id), None)
        arm = next((a for a in (exp or {}).get("arms", []) if a["name"] == arm_name), None)
        if arm is None:
            raise ValueError("unknown experiment or side")
        overrides = self.store.kv_get("config_overrides", {}) or {}
        for path, value in arm["overrides"].items():
            was = overrides.get(path, {}).get("was", experiments.get_dotted(self.config, path))
            overrides[path] = {"value": value, "was": was, "from": f"{exp['title']}: {arm_name}"}
            experiments.set_dotted(self.config, path, value)      # live, for this running worker
        self.store.kv_set("config_overrides", overrides)
        exp["adopted"] = arm_name
        self._save_experiments(records)
        self.tracer.event("experiment.adopt", "ok", detail=f"{exp['title']}: {arm_name}")
        return arm["overrides"]

    def undo_override(self, path: str) -> None:
        overrides = self.store.kv_get("config_overrides", {}) or {}
        entry = overrides.pop(path, None)
        if entry is None:
            raise ValueError(f"{path} isn't overridden")
        experiments.set_dotted(self.config, path, entry["was"])
        self.store.kv_set("config_overrides", overrides)
        self.tracer.event("experiment.undo", "ok", detail=f"{path} back to {entry['was']!r}")


def apply_overrides(config: AppConfig, store) -> list[str]:
    """Adopted winners, on top of the channel YAML (called when a pipeline is built)."""
    applied = []
    for path, entry in (store.kv_get("config_overrides", {}) or {}).items():
        try:
            experiments.set_dotted(config, path, entry["value"])
            applied.append(path)
        except (AttributeError, ValueError) as exc:
            log.warning("could not apply %s: %s", path, exc)
    return applied
