"""补发编排：基线原文 + 补发操作 → 新样本 → 新发布版本，全程事务化。

关键约束：
- 补发永远在「链条根（截止基线）」上重算，不在上一版结果上叠加，避免同一
  条更正被反复叠加；
- 同一 `supplement_id` 重试：内容一致 → 返回既有版本（replay，不新增）；
  内容不一致 → 409，绝不静默覆盖；
- 影响分析把链条根与当前版本对比，补发多少次都看累计影响。
"""
from __future__ import annotations

import datetime as dt

from ... import config
from ...errors import BadRequest, Conflict, NotFound
from ...store import AlreadyPublished, RunAlreadyExists
from ..m1_ingest import service as m1_service
from . import impact, publication, service
from . import supplement as supplement_mod


def apply(settings: config.Settings, store, *, supplement_id: str, ops,
          name: str | None = None, base_run_id: str | None = None,
          expected_version: int | None = None, dry_run: bool = False) -> dict:
    supplement_id = str(supplement_id or "").strip()
    if not supplement_id:
        raise BadRequest("补发批次需要 supplement_id", code="E_SUPPLEMENT_ID")
    normalized = supplement_mod.canonical_ops(ops)

    base = _resolve_base(store, name=name, base_run_id=base_run_id)
    base_release = store.release_for_run(base["run_id"])
    root_run_id = publication.chain_base(store, base_release, base["run_id"])
    root = store.get_run(root_run_id) or base
    root_release = store.release_for_run(root["run_id"])

    blob = store.get_blob(root["digest"])
    if blob is None:
        raise NotFound(f"基线原文缺失：{root['digest'][:12]}",
                       code="E_BASELINE_MISSING")
    decoded = m1_service.decode_blob(blob)
    digest = supplement_mod.ops_digest(normalized, root["digest"])

    existing = store.get_supplement(supplement_id)
    if existing is not None:
        return _replay_or_conflict(store, existing, digest, supplement_id)

    # 补发是累积的：先把同一链条上已生效的批次按登记顺序重放，再叠加本批；
    # 这样后到的更正不会抹掉先到的更正，也不会把任何一批叠加两次。
    # 内容完全相同的两批（不同批次号）只重放一次——否则会「重复计数」。
    chain = _chain(store, ingest_name=base["name"], root_run_id=root["run_id"])
    content = supplement_mod.content_digest(normalized)
    replay_batches = [item["ops"] for item in chain if item["replay"]]
    already_applied = any(item["content_digest"] == content for item in chain)
    cumulative = list(replay_batches)
    if not already_applied:
        cumulative.append(normalized)
    text, applied = supplement_mod.apply_many(decoded["text"], cumulative,
                                              decoded["delimiter"])
    data = m1_service.encode_like(text, blob, decoded)
    started_at = _now()
    ingest = m1_service.ingest(settings, name=base["name"], data=data,
                               declared=blob.get("declared_encoding"))
    runstore_version = store.latest_version(ingest["name"])
    if expected_version is not None and int(expected_version) < runstore_version:
        raise Conflict(
            f"提交版本 {expected_version} 落后于当前版本 {runstore_version}",
            code="E_STALE_RUN",
            detail={"expected_version": int(expected_version),
                    "latest_version": runstore_version})

    ops_summary = supplement_mod.scope_of(applied, applied["ops"].__len__())
    release = publication.build(
        kind=config.RELEASE_SUPPLEMENT, name=ingest["name"], digest=ingest["digest"],
        stamp=started_at, supplement_id=supplement_id,
        supplement_ids=[item["supplement_id"] for item in chain] + [supplement_id],
        parent_run_id=base["run_id"], baseline_run_id=root["run_id"],
        # 补发版本继承基线的截止时间：报告「样本范围」要能回答「截止到什么时候」
        cutoff_at=(root_release or {}).get("cutoff_at"),
        operations=ops_summary)
    release["scope"]["supplement_digest"] = digest
    release["scope"]["ops"] = _compact(applied)
    release["scope"]["this_batch"] = {
        "operations": {op: sum(1 for item in normalized if item["op"] == op)
                       for op in config.SUPPLEMENT_OPS},
        "ops": [{"op": item["op"], "ticket_id": item["ticket_id"],
                 "lineno": item["lineno"], "fields": sorted(item.get("fields", {}))}
                for item in normalized],
    }

    if dry_run:
        result = service.evaluate(ingest, settings=settings, started_at=started_at,
                                  clock=0.0, scope=release.get("scope"))
        return {"dry_run": True, "publication": _release_view(release, provisional=True),
                "impact": impact.build(store.load_result(root["run_id"]), result),
                "ingest": _ingest_view(ingest)}

    supplement_row = {
        "supplement_id": supplement_id, "name": ingest["name"],
        "ops_digest": digest, "payload": {"ops": normalized},
        "base_run_id": base["run_id"], "received_at": started_at,
        "applied_at": started_at,
    }
    try:
        existing_run = store.find_run(ingest["name"], ingest["digest"],
                                      settings.rule_version)
        if existing_run is not None:
            published = store.publish_existing(
                existing_run["run_id"],
                dataset=_dataset_fields(ingest, started_at),
                blob=_blob_fields(ingest, started_at),
                release=release, supplements=(supplement_row,))
            run_row = existing_run
            result = store.load_result(existing_run["run_id"])
            result["replay"] = True
        else:
            result = service.evaluate(ingest, settings=settings,
                                      started_at=started_at, clock=0.0,
                                      scope=release.get("scope"))
            store.publish(result, dataset=_dataset_fields(ingest, started_at),
                          blob=_blob_fields(ingest, started_at), release=release,
                          supplements=(supplement_row,))
            run_row = result["run"]
    except RunAlreadyExists as info:
        # 竞态：另一请求刚写入同一运行。复用该运行并挂本批发布。
        try:
            store.publish_existing(
                info.run["run_id"],
                dataset=_dataset_fields(ingest, started_at),
                blob=_blob_fields(ingest, started_at),
                release=release, supplements=(supplement_row,))
        except AlreadyPublished as conflict:
            replay_run = store.get_run(conflict.release["run_id"])
            return _replay_or_conflict(store, store.get_supplement(supplement_id),
                                       digest, supplement_id,
                                       release=conflict.release,
                                       result=store.load_result(replay_run["run_id"]))
        run_row = info.run
        result = store.load_result(info.run["run_id"])
        result["replay"] = True
    except AlreadyPublished as info:
        replay_run = store.get_run(info.release["run_id"])
        return _replay_or_conflict(store, store.get_supplement(supplement_id),
                                   digest, supplement_id, release=info.release,
                                   result=store.load_result(replay_run["run_id"]))

    current = store.load_result(run_row["run_id"])
    root_result = store.load_result(root["run_id"])
    return {
        "dry_run": False,
        "publication": store.release_for_run(run_row["run_id"]) or release,
        "run": current["run"],
        "score": current["score"]["score"],
        "grade": current["score"]["grade"],
        "counts": current["manifest"]["counters"],
        "impact": impact.build(root_result, current),
        "applied": applied,
        "supplement_id": supplement_id,
        "base_run_id": base["run_id"],
        "baseline_run_id": root["run_id"],
    }


def preview(settings: config.Settings, store, *, supplement_id: str, ops,
            name: str | None = None, base_run_id: str | None = None) -> dict:
    return apply(settings, store, supplement_id=supplement_id, ops=ops, name=name,
                 base_run_id=base_run_id, dry_run=True)


def _resolve_base(store, *, name: str | None, base_run_id: str | None) -> dict:
    if base_run_id:
        run = store.get_run(base_run_id)
        if run is None:
            raise NotFound(f"基线运行不存在：{base_run_id}", code="E_RUN_NOT_FOUND")
        return run
    if name:
        run = store.active_run(name) or store.latest_run(name)
    else:
        run = store.active_run() or store.latest_run()
    if run is None:
        raise NotFound("还没有基线运行，先跑一次截止评分", code="E_RUN_NOT_FOUND")
    return run


def _chain(store, *, ingest_name: str, root_run_id: str) -> list[dict]:
    """同一链条上已生效的补发批次，按登记顺序返回。

    每项带 `replay`：内容摘要相同的批次只有最早一条需要重放——不同批次号
    提交了同样的操作时，重放一次就够，多放一次就是重复计数；但仍保留在
    `supplement_ids` 里以便追溯「哪些批次被登记过」。
    """
    rows = store.list_supplements(name=ingest_name, limit=500)
    chain = []
    seen_content: set[str] = set()
    for row in reversed(rows):                     # list 是倒序，翻回登记顺序
        if not row.get("result_run_id"):
            continue
        release = store.release_for_run(row["result_run_id"])
        if not release or release.get("kind") != config.RELEASE_SUPPLEMENT:
            continue
        if release.get("baseline_run_id") != root_run_id:
            continue
        payload = row.get("payload") or _payload_of(row)
        ops = payload.get("ops") or []
        if not ops:
            continue
        content = supplement_mod.content_digest(ops)
        replay = content not in seen_content
        seen_content.add(content)
        chain.append({"supplement_id": row["supplement_id"], "ops": ops,
                      "content_digest": content, "replay": replay})
    return chain


def _payload_of(row: dict) -> dict:
    import json
    text = row.get("payload_json")
    if not text:
        return {}
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return {}


def _compact(applied: dict) -> list[dict]:
    return [{**item, "code": f"K_SUPPLEMENT_{item['op'].upper()}"}
            for item in applied["ops"]]


def _replay_or_conflict(store, existing: dict | None, digest: str,
                        supplement_id: str, *, release: dict | None = None,
                        result: dict | None = None) -> dict:
    if existing is None:
        raise Conflict("补发批次状态不一致，请重试", code="E_SUPPLEMENT_CONFLICT")
    if existing["ops_digest"] != digest:
        raise Conflict(
            f"补发批次 {supplement_id} 已用不同内容登记过，拒绝覆盖",
            code="E_SUPPLEMENT_CONFLICT",
            detail={"supplement_id": supplement_id,
                    "registered_digest": existing["ops_digest"],
                    "submitted_digest": digest})
    run_id = existing.get("result_run_id") or (release or {}).get("run_id")
    run = store.get_run(run_id) if run_id else None
    if run is None:
        raise Conflict("补发批次缺少结果运行，请重建", code="E_SUPPLEMENT_CONFLICT")
    current = result or store.load_result(run_id)
    current = {**current, "replay": True}
    publication_row = release or store.release_for_run(run_id)
    root_id = (publication_row or {}).get("baseline_run_id") or run_id
    root = store.load_result(root_id) or current
    return {
        "dry_run": False,
        "publication": publication_row,
        "run": current["run"],
        "score": current["score"]["score"],
        "grade": current["score"]["grade"],
        "counts": current["manifest"]["counters"],
        "impact": impact.build(root, current),
        "applied": None,
        "supplement_id": supplement_id,
        "base_run_id": existing.get("base_run_id"),
        "baseline_run_id": root_id,
        "replay": True,
    }


def _dataset_fields(ingest: dict, stamp: str) -> dict:
    return {"name": ingest["name"], "digest": ingest["digest"],
            "encoding": ingest["encoding"],
            "declared_encoding": ingest["declared_encoding"],
            "data": ingest["raw"], "stamp": stamp}


def _blob_fields(ingest: dict, stamp: str) -> dict:
    return {"digest": ingest["digest"], "encoding": ingest["encoding"],
            "declared_encoding": ingest["declared_encoding"],
            "data": ingest["raw"], "stamp": stamp}


def _ingest_view(ingest: dict) -> dict:
    return {"name": ingest["name"], "digest": ingest["digest"],
            "counts": ingest["counts"], "segments": ingest["segments"]}


def _release_view(release: dict, *, provisional: bool = False) -> dict:
    return {**release, "provisional": provisional}


def compare(store, *, before_run_id: str, after_run_id: str) -> dict:
    before = store.load_result(before_run_id)
    after = store.load_result(after_run_id)
    if before is None or after is None:
        raise NotFound("对比运行快照缺失", code="E_RUN_NOT_FOUND")
    return impact.build(before, after)


def _now() -> str:
    return dt.datetime.now(config.UTC).isoformat(timespec="seconds")
