"""M7 编排：报告数据与七种导出文件，全部读同一条运行快照。

报告页、缺失明细与每个 CSV / 清单都从 `run_id` 指向的同一份 summary_json
取数；未指定 run_id 时统一落到该数据集的 active 发布版本。补发影响是纯读
操作，按链条根与当前版本对比，不写库。
"""
from __future__ import annotations

from fastapi.responses import Response

from ...errors import BadRequest
from ..m6_score import impact as impact_mod
from ..m6_score import runstore
from . import csv_out, manifest_out, report

EXPORTS = {
    "report.csv": ("report.csv", "text/csv; charset=utf-8", csv_out.report_csv),
    "profile.csv": ("profile.csv", "text/csv; charset=utf-8", csv_out.profile_csv),
    "missing.csv": ("missing.csv", "text/csv; charset=utf-8", csv_out.missing_csv),
    "duplicates.csv": ("duplicates.csv", "text/csv; charset=utf-8",
                       csv_out.duplicates_csv),
    "outliers.csv": ("outliers.csv", "text/csv; charset=utf-8", csv_out.outliers_csv),
    "quarantine.csv": ("quarantine.csv", "text/csv; charset=utf-8",
                       csv_out.quarantine_csv),
    "flags.csv": ("flags.csv", "text/csv; charset=utf-8", csv_out.flags_csv),
    "impact.csv": ("impact.csv", "text/csv; charset=utf-8", None),
}


def report_payload(store, run_id: str | None = None) -> dict:
    run, result = _load(store, run_id)
    release = store.release_for_run(run["run_id"])
    payload = report.build(result, release=release,
                           impact=_impact_for(store, release,
                                              store.load_result(run["run_id"])))
    payload["run"] = {**runstore.summary(run)}
    return payload


def export(store, kind: str, run_id: str | None = None) -> Response:
    if kind not in EXPORTS and kind != "manifest.json":
        raise BadRequest(f"未知导出：{kind}")
    run, result = _load(store, run_id)
    release = store.release_for_run(run["run_id"])
    if kind == "manifest.json":
        impact = _impact_for(store, release, result)
        payload = manifest_out.build(result, release=release, impact=impact)
        return Response(content=manifest_out.to_json(payload),
                        media_type="application/json; charset=utf-8",
                        headers=_headers(f"manifest-{run['run_id']}.json"))
    filename, media, builder = EXPORTS[kind]
    if kind == "impact.csv":
        base_id = (release or {}).get("baseline_run_id")
        base = store.load_result(base_id) if base_id else None
        if base is None:
            raise BadRequest("这一版没有可对比的基线，impact.csv 需要补发版本",
                             code="E_NO_BASELINE")
        content = csv_out.impact_csv(base, result, release=release)
    else:
        content = builder(result, release=release)
    return Response(content=content.encode("utf-8"), media_type=media,
                    headers=_headers(f"{run['name']}-{run['run_id']}-{filename}"))


def _headers(filename: str) -> dict:
    return {"Content-Disposition": f'attachment; filename="{filename}"'}


def _impact_for(store, release: dict | None, result: dict | None) -> dict | None:
    if not release or release.get("kind") != "supplement":
        return None
    root_id = release.get("baseline_run_id")
    if not root_id or result is None:
        return None
    base = store.load_result(root_id)
    if base is None:
        return None
    return impact_mod.build(base, result)


def _load(store, run_id: str | None) -> tuple[dict, dict]:
    run = runstore.require_run(store, run_id)
    result = store.load_result(run["run_id"])
    if result is None:
        raise BadRequest(f"运行快照缺失：{run['run_id']}")
    return run, result
