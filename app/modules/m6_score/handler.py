"""M6 入口：跑一次评分、登记截止基线、应用补发、查运行历史与影响。"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from ... import config
from ...errors import Conflict, NotFound
from ...web import Ctx, get_ctx, render
from . import impact, publication, revision, runstore, service

router = APIRouter(tags=["M6 评分与批次"])


def _public(result: dict) -> dict:
    return {
        "run": result["run"],
        "score": result["score"]["score"],
        "grade": result["score"]["grade"],
        "grade_label": result["score"]["grade_label"],
        "penalty_sum": result["score"]["penalty_sum"],
        "dims": result["score"]["dims"],
        "bands": result["score"]["bands"],
        "closed": result["score"]["closed"],
        "replay": result.get("replay", False),
        "publication": result.get("publication"),
        "counts": result["manifest"]["counters"],
        "reconcile": result["reconcile"],
        "dataset": result["dataset"]["name"],
    }


@router.post("/api/score")
def api_score(payload: dict | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    name = str(body.get("sample") or body.get("dataset") or config.DEFAULT_SAMPLE)
    declared = body.get("encoding")
    result = service.run(ctx.settings, ctx.store, name=name,
                         declared=str(declared) if declared else None,
                         expected_version=body.get("expected_version"),
                         release_spec=_release_spec(body, name))
    return _public(result)


@router.post("/api/score/upload")
async def api_score_upload(file: UploadFile = File(...),
                           encoding: str = Form(default=""),
                           expected_version: str = Form(default=""),
                           release: str = Form(default=""),
                           cutoff_at: str = Form(default=""),
                           ctx: Ctx = Depends(get_ctx)) -> dict:
    data = await file.read()
    name = file.filename or "upload.csv"
    body = {"release": release, "cutoff_at": cutoff_at}
    result = service.run(ctx.settings, ctx.store, name=name,
                         data=data, declared=encoding or None,
                         expected_version=expected_version or None,
                         release_spec=_release_spec(body, name))
    return _public(result)


@router.get("/api/score/latest")
def api_latest(run_id: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    run, result = service.latest(ctx.store, run_id)
    return {"run": runstore.summary(run), "score": result.get("score"),
            "publication": ctx.store.release_for_run(run["run_id"]),
            "reconcile": result.get("reconcile")}


@router.get("/api/runs/{run_id}")
def api_run(run_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    run = runstore.require_run(ctx.store, run_id)
    return {"run": runstore.summary(run),
            "publication": ctx.store.release_for_run(run["run_id"]),
            "result": ctx.store.load_result(run["run_id"])}


@router.get("/api/batches")
def api_batches(limit: int = 20, ctx: Ctx = Depends(get_ctx)) -> dict:
    runs = ctx.store.list_runs(limit=limit)
    items = []
    for run in runs:
        release = ctx.store.release_for_run(run["run_id"])
        items.append({**runstore.summary(run), "release": release})
    return {"runs": items}


@router.get("/api/releases")
def api_releases(name: str | None = None, limit: int = 30,
                 ctx: Ctx = Depends(get_ctx)) -> dict:
    active = ctx.store.active_release(name)
    return {"active": active,
            "releases": ctx.store.list_releases(name=name, limit=limit)}


@router.get("/api/reconcile")
def api_reconcile(run_id: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    run, result = service.latest(ctx.store, run_id)
    reconcile = result.get("reconcile", {"accounts": [], "all_balanced": None})
    return {"run_id": run["run_id"], "release": ctx.store.release_for_run(run["run_id"]),
            "accounts": reconcile["accounts"],
            "all_balanced": reconcile.get("all_balanced")}


# ---------- 补发 ----------
@router.post("/api/supplements")
def api_supplement(payload: dict | None = None,
                   ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    name = body.get("dataset") or body.get("sample")
    result = revision.apply(
        ctx.settings, ctx.store,
        supplement_id=str(body.get("supplement_id") or ""),
        ops=body.get("ops"),
        name=str(name) if name else None,
        base_run_id=body.get("base_run_id"),
        expected_version=body.get("expected_version"))
    return result


@router.post("/api/supplements/preview")
def api_supplement_preview(payload: dict | None = None,
                           ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    name = body.get("dataset") or body.get("sample")
    return revision.preview(
        ctx.settings, ctx.store,
        supplement_id=str(body.get("supplement_id") or "preview"),
        ops=body.get("ops"), name=str(name) if name else None,
        base_run_id=body.get("base_run_id"))


@router.get("/api/supplements")
def api_supplements(name: str | None = None, limit: int = 50,
                    ctx: Ctx = Depends(get_ctx)) -> dict:
    rows = ctx.store.list_supplements(name=name, limit=limit)
    for row in rows:
        row["payload"] = _safe_json(row.get("payload_json"))
        row.pop("payload_json", None)
    return {"supplements": rows}


@router.get("/api/impact")
def api_impact(before_run_id: str, after_run_id: str,
               ctx: Ctx = Depends(get_ctx)) -> dict:
    return revision.compare(ctx.store, before_run_id=before_run_id,
                            after_run_id=after_run_id)


@router.post("/api/releases/{release_id}/activate")
def api_activate_release(release_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    """发布中断 / 回滚：先自检完整性，再把 active 指针指回完整旧版。"""
    check = ctx.store.verify_release(release_id)
    if not check["ok"]:
        raise Conflict(f"发布 {release_id} 不完整：缺 {', '.join(check['missing'])}",
                       code="E_RELEASE_INCOMPLETE", detail=check)
    try:
        release = ctx.store.activate_release(release_id, _now())
    except KeyError:
        raise NotFound(f"发布版本不存在：{release_id}", code="E_RELEASE_NOT_FOUND") from None
    return {"active": release, "verified": check}


@router.get("/api/releases/{release_id}/verify")
def api_verify_release(release_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    return ctx.store.verify_release(release_id)


@router.get("/batches")
def page_batches(request: Request, run_id: str | None = None,
                 ctx: Ctx = Depends(get_ctx)):
    run, result = ctx.current_result(run_id)
    release = ctx.current_release(run_id)
    dataset_name = run["name"] if run else None
    runs = ctx.store.list_runs(limit=12)
    releases = (ctx.store.list_releases(name=dataset_name, limit=20)
                if dataset_name else ctx.store.list_releases(limit=20))
    active = ctx.store.active_release(dataset_name) if dataset_name else None
    return render(request, "batches.html", page="batches", page_title="评分批次",
                  run=run, result=result, runs=runs, releases=releases,
                  release=release, active=active,
                  reconcile=result["reconcile"] if result else None,
                  thresholds=runstore.thresholds())


def _stamp(value) -> str | None:
    return publication.normalize_cutoff(value)


def _now() -> str:
    return dt.datetime.now(config.UTC).isoformat(timespec="seconds")


def _release_spec(body: dict, name: str) -> dict:
    kind = publication.resolve_kind(body.get("release"))
    cutoff_at = publication.normalize_cutoff(body.get("cutoff_at"))
    return {"kind": kind, "cutoff_at": cutoff_at}


def _safe_json(text):
    import json
    try:
        return json.loads(text) if text else {}
    except (TypeError, ValueError):
        return {}
