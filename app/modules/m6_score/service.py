"""M6 编排：一次运行把七个阶段串起来，落库为一条可复算的运行记录。

顺序固定：采信 → 画像 → 缺失 → 重复 → 离群 → 评分 → 对账。
同 (数据集名, 输入摘要, 规则版本) 命中已有记录时直接复用，不新增运行行。

发布口径（release kind）：
- `baseline` 截止时样本，一次运行只产生一版，可反复重放；
- `run`     普通单次运行，同内容重跑复用运行；
- `supplement` 截止后补发，基线 + 增量逐位相同时复用运行，不新增版本。
所有口径最后都落成一条 release 指针；报告、缺失明细与导出都读这条 run_id。
"""
from __future__ import annotations

import datetime as dt
import time
from decimal import Decimal

from ... import config, manifest
from ...errors import Conflict
from ..m1_ingest import service as m1_service
from ..m2_profile import service as m2_service
from ..m3_missing import service as m3_service
from ..m4_duplicates import service as m4_service
from ..m5_outliers import service as m5_service
from . import publication
from . import runstore
from . import score as score_mod


def run(settings: config.Settings, store, *, name: str | None = None,
        data: bytes | None = None, declared: str | None = None,
        expected_version: int | None = None, release_spec: dict | None = None) -> dict:
    started_at = dt.datetime.now(config.UTC).isoformat(timespec="seconds")
    clock = time.perf_counter()
    stages: list[dict] = []
    counters: dict = {}

    ingest = m1_service.ingest(settings, name=name, data=data,
                               declared=declared, store=store)
    runstore.stale_check(store, ingest["name"], _int_or_none(expected_version))
    release = _build_release(ingest, release_spec, started_at)
    _guard_frozen_baseline(store, ingest, release)
    existing = store.find_run(ingest["name"], ingest["digest"], settings.rule_version)
    if existing is not None:
        published = publication.publish_existing(
            store, ingest, existing, release=release, stamp=started_at)
        payload = runstore.replay(store, existing, publication=published)
        payload["publication"] = published
        return payload
    result = evaluate(ingest, settings=settings, started_at=started_at, clock=clock,
                      scope=release.get("scope"))
    published = publication.publish_new(
        store, ingest, result, release=release, stamp=started_at)
    if published.get("reused_run"):
        # 并发竞态：同内容运行已被别的请求写入，本地这份计算没落库；
        # 必须改读库里快照，避免把未持久化的 run_id 返回给调用方。
        existing = published.get("existing_run") or store.find_run(
            ingest["name"], ingest["digest"], settings.rule_version)
        if existing is not None:
            payload = runstore.replay(store, existing, publication=published)
            payload["publication"] = published
            return payload
    result["publication"] = published
    return result


def _build_release(ingest: dict, spec: dict | None, stamp: str) -> dict:
    """发布口径在采信后才成立：发布键要用真实输入摘要，不能先用占位值。"""
    spec = spec or {}
    kind = spec.get("kind") or config.RELEASE_RUN
    return publication.build(
        kind=kind, name=ingest["name"], digest=ingest["digest"], stamp=stamp,
        cutoff_at=spec.get("cutoff_at"),
        parent_run_id=spec.get("parent_run_id"),
        baseline_run_id=spec.get("baseline_run_id"))


def _guard_frozen_baseline(store, ingest: dict, release: dict) -> None:
    """基线一经冻结，内容只能通过补发推进：普通运行不允许换掉截止样本。

    同一份内容重放基线是允许的（幂等重跑），但若这次声明了不同的截止时间，
    必须显式拒绝——否则「截止时间」会被悄悄改写，报告口径就不可解释了。
    """
    baseline = store.baseline_release(ingest["name"])
    if baseline is None:
        return
    baseline_run = store.get_run(baseline["run_id"])
    same_content = (baseline_run is not None
                    and baseline_run["digest"] == ingest["digest"])
    if same_content:
        if release["kind"] == config.RELEASE_BASELINE and \
                release.get("cutoff_at") != baseline.get("cutoff_at"):
            raise Conflict(
                f"{ingest['name']} 的截止时间已冻结为 {baseline.get('cutoff_at')}，"
                "不能改写；如需新截止请换数据集名或先重建库",
                code="E_BASELINE_FROZEN",
                detail={"baseline_run_id": baseline["run_id"],
                        "cutoff_at": baseline.get("cutoff_at")})
        return
    if release["kind"] == config.RELEASE_BASELINE:
        raise Conflict(
            f"{ingest['name']} 的截止基线已冻结于 {baseline['run_id']}，"
            "不能再用新内容重建基线；请登记补发批次",
            code="E_BASELINE_FROZEN",
            detail={"baseline_run_id": baseline["run_id"]})
    if release["kind"] == config.RELEASE_RUN:
        raise Conflict(
            f"{ingest['name']} 已冻结截止基线，内容变化请走补发批次",
            code="E_BASELINE_FROZEN",
            detail={"baseline_run_id": baseline["run_id"]})


def evaluate(ingest: dict, *, settings: config.Settings, started_at: str,
             clock: float, scope: dict | None = None) -> dict:
    """纯计算：不写库，便于测试与即时试算（预览补发影响）。"""
    stages: list[dict] = []
    stages.append(_stage("ingest", ingest["counts"]["raw_records"],
                         ingest["counts"]["accepted"],
                         ingest["counts"]["quarantine"], clock))

    profile = m2_service.build(ingest)
    stages.append(_stage("profile", len(profile["rows"]),
                         profile["counts"]["cells_total"], 0, clock))

    missing = m3_service.build(profile)
    stages.append(_stage("missing", profile["counts"]["cells_total"],
                         missing["account"]["filled"], missing["account"]["missing"],
                         clock))

    duplicates = m4_service.build(profile)
    stages.append(_stage("duplicates", duplicates["rows"],
                         duplicates["unique_rows"], duplicates["duplicate_rows"], clock))

    outliers = m5_service.build(profile)
    stages.append(_stage("outliers", outliers["counts"]["tested_cells"],
                         outliers["counts"]["tested_cells"]
                         - outliers["counts"]["outlier_cells"],
                         outliers["counts"]["outlier_cells"], clock))

    counts = {
        "cells_total": profile["counts"]["cells_total"],
        "cells_missing": profile["counts"]["cells_missing"],
        "cells_value": profile["counts"]["cells_value"],
        "cells_ok": profile["counts"]["cells_ok"],
        "format_bad": profile["counts"]["cells_format_bad"],
        "rows_accepted": len(profile["rows"]),
        "rows_dup": duplicates["duplicate_rows"],
        "outlier_cells": outliers["counts"]["outlier_cells"],
        "tested_cells": outliers["counts"]["tested_cells"],
    }
    scored = score_mod.evaluate(counts)
    stages.append(_stage("score", len(scored["dims"]), 1, 0, clock))

    reconcile = _reconcile(ingest, profile, missing, duplicates, outliers, scored)
    evidence_rows = (len(ingest["quarantine"]) + len(duplicates["conflicts"])
                     + len(duplicates["hash_groups"]) + len(outliers["cells"]))
    stages.append(_stage("export", evidence_rows, evidence_rows, 0, clock))

    counters = {
        "raw_records": ingest["counts"]["raw_records"],
        "accepted": ingest["counts"]["accepted"],
        "quarantine": ingest["counts"]["quarantine"],
        "blank": ingest["counts"]["blank"],
        "cells_total": counts["cells_total"],
        "cells_missing": counts["cells_missing"],
        "cells_value": counts["cells_value"],
        "cells_ok": counts["cells_ok"],
        "cells_format_bad": counts["format_bad"],
        "rows_unique": duplicates["unique_rows"],
        "rows_dup": duplicates["duplicate_rows"],
        "outlier_cells": counts["outlier_cells"],
        "tested_cells": counts["tested_cells"],
        "segments": ingest["segments"],
    }
    run_id = manifest.new_run_id()
    finished_at = dt.datetime.now(config.UTC).isoformat(timespec="seconds")
    payload = manifest.build(
        run_id=run_id, version=0, name=ingest["name"], digest=ingest["digest"],
        settings=settings, encoding=ingest["encoding"],
        declared_encoding=ingest["declared_encoding"],
        params={"delimiter": ingest["delimiter"], "segments": ingest["segments"],
                "declared_encoding": ingest["declared_encoding"]},
        started_at=started_at, finished_at=finished_at,
        elapsed_ms=int((time.perf_counter() - clock) * 1000),
        stages=[_stage_export(item) for item in stages],
        counters=counters, reconcile=reconcile,
        thresholds=runstore.thresholds(), scope=scope)
    result = {
        "run": {
            "run_id": run_id, "name": ingest["name"], "digest": ingest["digest"],
            "rules_version": settings.rule_version, "version": 0,
            "score": scored["score"], "grade": scored["grade"],
            "rows_in": ingest["counts"]["raw_records"],
            "rows_accepted": ingest["counts"]["accepted"],
            "rows_unique": duplicates["unique_rows"],
            "rows_dup": duplicates["duplicate_rows"],
            "rows_quarantine": ingest["counts"]["quarantine"],
            "rows_blank": ingest["counts"]["blank"],
            "cells_total": counts["cells_total"],
            "cells_missing": counts["cells_missing"],
            "created_at": started_at, "finished_at": finished_at,
        },
        "dataset": {
            "name": ingest["name"], "digest": ingest["digest"],
            "encoding": ingest["encoding"],
            "declared_encoding": ingest["declared_encoding"],
            "delimiter": ingest["delimiter"], "segments": ingest["segments"],
            "header": ingest["header"], "counts": ingest["counts"],
            "flags": _flags(ingest, scope),
            "quarantine_summary": m1_service.quarantine_summary(ingest["quarantine"]),
            "quarantine": ingest["quarantine"],
        },
        "profile": {
            "columns": profile["columns"],
            "profile": profile["profile"],
            "counts": profile["counts"],
        },
        "missing": missing,
        "duplicates": duplicates,
        "outliers": outliers,
        "score": scored,
        "reconcile": reconcile,
        "manifest": payload,
        "replay": False,
    }
    return _json_safe(result)


def _flags(ingest: dict, scope: dict | None) -> list[dict]:
    """补发版本的标记表要额外说明「证据来自补发后样本」，避免读者把
    「原本的截断行现在算列数不符」误读成未补发。"""
    flags = m1_service.flag_summary(ingest["flags"])
    if scope and scope.get("kind") == config.RELEASE_SUPPLEMENT:
        ops = scope.get("operations") or {}
        flags.append({
            "code": "K_SUPPLEMENT_APPLIED", "label": "已应用补发",
            "level": "info", "count": scope.get("supplement_total", 0),
            "detail": (f"迟到 {ops.get('late', 0)} / 撤回 {ops.get('withdraw', 0)}"
                       f" / 更正 {ops.get('correct', 0)}；"
                       "隔离与标记基于补发后样本重新判定"),
        })
    return flags


def _stage(key: str, in_count: int, out_count: int, quarantine_count: int,
           clock: float) -> dict:
    return {
        "stage": key,
        "label": config.STAGE_LABELS.get(key, key),
        "in_count": int(in_count),
        "out_count": int(out_count),
        "quarantine_count": int(quarantine_count),
        "status": "done",
        "elapsed_ms": int((time.perf_counter() - clock) * 1000),
        "note": "",
    }


def _stage_export(item: dict) -> dict:
    return {**item}


def _reconcile(ingest: dict, profile: dict, missing: dict, duplicates: dict,
               outliers: dict, scored: dict) -> dict:
    counts = ingest["counts"]
    cells_total = profile["counts"]["cells_total"]
    cells_missing = profile["counts"]["cells_missing"]
    cells_bad = profile["counts"]["cells_format_bad"]
    cells_value = profile["counts"]["cells_value"]
    cells_ok = profile["counts"]["cells_ok"]
    penalty_sum = Decimal(scored["penalty_sum"])
    total_score = Decimal(scored["score"])
    accounts = [
        _account("rows", "数据行账", counts["raw_records"],
                 counts["accepted"] + counts["quarantine"] + counts["blank"],
                 "原始记录 = 采纳 + 隔离 + 空行"),
        _account("accepted", "采纳行账", counts["accepted"],
                 duplicates["unique_rows"] + duplicates["duplicate_rows"],
                 "采纳 = 唯一 + 重复"),
        _account("cells", "单元格账", cells_total,
                 cells_missing + cells_value,
                 "单元格 = 缺失 + 有值"),
        _account("value", "有值格账", cells_value,
                 cells_ok + cells_bad,
                 "有值 = 合格 + 格式不合格"),
        _account("score", "分数账", Decimal("100"), total_score + penalty_sum,
                 "总分 + 扣分 = 100"),
    ]
    return {
        "accounts": accounts,
        "all_balanced": all(item["balanced"] for item in accounts),
        "diff_count": sum(1 for item in accounts if not item["balanced"]),
        "missing_runs": len(missing["runs"]),
        "key_conflicts": len(duplicates["conflicts"]),
        "degenerate_columns": [item["column"] for item in outliers["degenerate"]],
    }


def _account(key: str, label: str, left, right, note: str) -> dict:
    left_dec = Decimal(str(left))
    right_dec = Decimal(str(right))
    diff = left_dec - right_dec
    return {"key": key, "label": label, "left": left_dec, "right": right_dec,
            "diff": diff, "balanced": diff == 0, "note": note}


def _int_or_none(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def latest(store, run_id: str | None = None) -> tuple[dict, dict]:
    run = runstore.require_run(store, run_id)
    return run, store.load_result(run["run_id"]) or {}


def summary(result: dict) -> dict:
    return {
        "run": result["run"],
        "score": result["score"]["score"],
        "grade": result["score"]["grade"],
        "grade_label": result["score"]["grade_label"],
        "penalty_sum": result["score"]["penalty_sum"],
        "dims": result["score"]["dims"],
        "bands": result["score"]["bands"],
        "reconcile": result["reconcile"],
        "dataset": result["dataset"]["name"],
    }
