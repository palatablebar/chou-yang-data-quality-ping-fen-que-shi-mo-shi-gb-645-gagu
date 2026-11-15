"""发布编排：把一次运行包成带「样本范围」的发布版本，并保证原子落库。

三个口径：
- `baseline` 截止时样本：冻结基线，之后只允许通过补发推进；
- `run`     普通单次运行：同内容重跑只复用运行，不新增版本；
- `supplement` 补发增量：基线 + 补发操作算出的新样本，每次事件推进一版。

发布键（release_key）是幂等闸门：
- 补发批次 `supplement:<id>`：同一批次重试命中既有发布，不新增版本、不重复计数；
- 普通运行 `<kind>:<name>:<digest>:<规则版本>`：同内容重跑命中既有发布。
同 id 但内容不同由上层用 ops_digest 拦成 409，不会静默覆盖。
"""
from __future__ import annotations

import datetime as dt

from ... import config, manifest
from ...errors import BadRequest
from ...store import AlreadyPublished, RunAlreadyExists


def new_release_id() -> str:
    return manifest.new_run_id(prefix="RS")


def release_key(*, kind: str, name: str, digest: str, rule_version: str,
                supplement_id: str | None = None) -> str:
    if supplement_id:
        return f"supplement:{supplement_id}"
    if kind == config.RELEASE_BASELINE:
        return f"baseline:{name}:{digest}:{rule_version}"
    return f"run:{name}:{digest}:{rule_version}"


def build(*, kind: str, name: str, digest: str, stamp: str,
          cutoff_at: str | None = None, supplement_id: str | None = None,
          supplement_ids: list[str] | None = None,
          parent_run_id: str | None = None,
          baseline_run_id: str | None = None,
          operations: dict | None = None) -> dict:
    ids = list(supplement_ids or [])
    if supplement_id and supplement_id not in ids:
        ids.append(supplement_id)
    cutoff_at = normalize_cutoff(cutoff_at)
    ops = {"late": 0, "withdraw": 0, "correct": 0, **(operations or {})}
    scope = {
        "kind": kind,
        "label": config.RELEASE_LABELS.get(kind, kind),
        "cutoff_at": cutoff_at,
        "supplement_ids": ids,
        "supplement_total": len(ids),
        "operations": ops,
        "operations_total": sum(int(value) for key, value in ops.items()
                                if key in config.SUPPLEMENT_OPS),
        "baseline_run_id": baseline_run_id,
        "parent_run_id": parent_run_id,
    }
    return {
        "release_id": new_release_id(),
        "release_key": release_key(kind=kind, name=name, digest=digest,
                                   rule_version=config.RULE_VERSION,
                                   supplement_id=supplement_id),
        "name": name,
        "run_id": "",
        "version": 0,
        "kind": kind,
        "parent_run_id": parent_run_id,
        "baseline_run_id": baseline_run_id,
        "cutoff_at": cutoff_at,
        "supplement_ids": ids,
        "scope": scope,
        "published_at": stamp,
    }


def chain_base(store, release: dict | None, run_id: str) -> str:
    """链条根：补发链指向最初的截止基线；普通运行就是它自己。"""
    if release and release.get("kind") == config.RELEASE_SUPPLEMENT:
        return release.get("baseline_run_id") or run_id
    return run_id


def resolve_kind(value) -> str:
    kind = str(value or config.RELEASE_RUN)
    if kind not in (config.RELEASE_BASELINE, config.RELEASE_RUN):
        raise BadRequest(f"未知发布口径：{kind}，只支持 baseline / run",
                         code="E_RELEASE_KIND")
    return kind


def normalize_cutoff(value) -> str | None:
    """截止时间统一存 UTC；无偏移按单据本地时区 +08:00。"""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise BadRequest(f"截止时间不是 ISO8601：{value}", code="E_BAD_CUTOFF") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=config.CHINA_TZ)
    return moment.astimezone(config.UTC).isoformat(timespec="seconds")


def _io_fields(ingest: dict, stamp: str) -> tuple[dict, dict]:
    dataset = {"name": ingest["name"], "digest": ingest["digest"],
               "encoding": ingest["encoding"],
               "declared_encoding": ingest["declared_encoding"],
               "data": ingest["raw"], "stamp": stamp}
    blob = {"digest": ingest["digest"], "encoding": ingest["encoding"],
            "declared_encoding": ingest["declared_encoding"],
            "data": ingest["raw"], "stamp": stamp}
    return dataset, blob


def publish_new(store, ingest: dict, result: dict, *, release: dict | None,
                stamp: str) -> dict:
    """新内容：写入原文副本、数据集指针、运行、发布与补发记录（一个事务）。

    返回的 dict 一定带 `run_id` 与 `reused_run`：
    - 正常写入：`reused_run = False`，`run_id` 就是 `result["run"]["run_id"]`；
    - 并发竞态（同内容运行刚被别的请求写入）：`reused_run = True`，调用方
      应改读库里已有的运行快照，而不是使用本地这份未落库的计算结果。
    """
    dataset, blob = _io_fields(ingest, stamp)
    release = release or default_release(ingest, stamp)
    try:
        store.publish(result, dataset=dataset, blob=blob, release=release)
    except AlreadyPublished as info:
        return {**info.release, "reused_run": True}
    except RunAlreadyExists as info:
        attached = publish_existing(store, ingest, info.run, release=release,
                                    stamp=stamp)
        return {**attached, "reused_run": True,
                "existing_run": info.run}
    release["reused_run"] = False
    return release


def publish_existing(store, ingest: dict, existing: dict, *, release: dict | None,
                     stamp: str) -> dict:
    """内容与既有运行逐位相同：复用运行，只挂发布指针（不新增版本）。"""
    dataset, blob = _io_fields(ingest, stamp)
    release = release or default_release(ingest, stamp)
    try:
        store.publish_existing(existing["run_id"], dataset=dataset, blob=blob,
                               release=release)
    except AlreadyPublished as info:
        return {**info.release, "reused_run": True}
    release["reused_run"] = True
    return release


def default_release(ingest: dict, stamp: str) -> dict:
    return build(kind=config.RELEASE_RUN, name=ingest["name"],
                 digest=ingest["digest"], stamp=stamp)
