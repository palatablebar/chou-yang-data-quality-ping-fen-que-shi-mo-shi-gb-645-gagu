"""运行清单导出：输入摘要、阈值、计数、对账与样本口径，可离线复算。"""
from __future__ import annotations

import json

from ... import config


def build(result: dict, *, release: dict | None = None,
          impact: dict | None = None) -> dict:
    manifest = result["manifest"]
    scope = (release or {}).get("scope") or manifest.get("scope") or {}
    payload = {
        "run_id": manifest["run_id"],
        "version": manifest["version"],
        "dataset": manifest["dataset"],
        "rule_version": manifest["rule_version"],
        "rules_digest": manifest["rules_digest"],
        "input_digest": manifest["input_digest"],
        "encoding": manifest["encoding"],
        "declared_encoding": manifest["declared_encoding"],
        "params": manifest["params"],
        "thresholds": manifest["thresholds"],
        "started_at": manifest["started_at"],
        "finished_at": manifest["finished_at"],
        "elapsed_ms": manifest["elapsed_ms"],
        "timezone": manifest["timezone"],
        "decimals": manifest["decimals"],
        "score": result["score"]["score"],
        "grade": result["score"]["grade"],
        "penalty_sum": result["score"]["penalty_sum"],
        "counters": manifest["counters"],
        "accounts": manifest["accounts"],
        "all_balanced": manifest["all_balanced"],
        "stages": manifest["stages"],
        "weights": [{"key": item["key"], "cap": item["cap"],
                     "tolerance": item["tolerance"]} for item in config.WEIGHTS],
        "grades": [dict(item) for item in config.GRADES],
        "scope": scope,
        "release": _release_view(release),
    }
    if impact is not None:
        payload["impact"] = impact
    return payload


def _release_view(release: dict | None) -> dict | None:
    if not release:
        return None
    return {
        "release_id": release.get("release_id"),
        "kind": release.get("kind"),
        "label": release.get("label") or (release.get("scope") or {}).get("label"),
        "cutoff_at": release.get("cutoff_at"),
        "baseline_run_id": release.get("baseline_run_id"),
        "parent_run_id": release.get("parent_run_id"),
        "supplement_ids": release.get("supplement_ids") or [],
        "published_at": release.get("published_at"),
    }


def to_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
