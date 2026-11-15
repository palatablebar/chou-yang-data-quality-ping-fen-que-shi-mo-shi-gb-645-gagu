"""端到端：截止基线 → 补发 → 同版本导出 → 中断恢复，全程走 HTTP。"""
from __future__ import annotations

import json
from decimal import Decimal

from fixtures import expected

SAMPLE = "tickets_dirty.csv"
CUTOFF = "2026-11-01T00:00:00+08:00"
PAGES = ("/", "/profile", "/missing", "/duplicates", "/outliers", "/batches")
EXPORTS = ("report.csv", "profile.csv", "missing.csv", "duplicates.csv",
           "outliers.csv", "quarantine.csv", "flags.csv")


def make_baseline(client):
    response = client.post("/api/score", json={
        "sample": SAMPLE, "release": "baseline", "cutoff_at": CUTOFF})
    assert response.status_code == 200, response.text
    return response.json()


def late_row(ticket="T-2029-000001", **overrides):
    row = {"ticket_id": ticket, "customer_id": "00999", "channel": "电话",
           "created_at": "2026-11-02T10:00:00+08:00", "first_response_min": "20",
           "handle_hours": "2.00", "satisfaction": "4", "agent": "陈允",
           "region": "华东", "priority": "P2"}
    row.update(overrides)
    return row


def test_report_missing_and_exports_share_one_version(client):
    base = make_baseline(client)
    assert base["score"] == expected.DIRTY["score"]
    assert base["publication"]["kind"] == "baseline"

    supplement = client.post("/api/supplements", json={
        "supplement_id": "SUP-E2E", "dataset": SAMPLE,
        "ops": [{"op": "late", "row": late_row()},
                {"op": "withdraw", "ticket_id": "T-2026-000102", "lineno": 35}],
    })
    assert supplement.status_code == 200, supplement.text
    body = supplement.json()
    run_id = body["run"]["run_id"]

    for page in PAGES:
        response = client.get(page, params={"run_id": run_id})
        assert response.status_code == 200, page
        if page == "/":
            assert body["score"] in response.text

    manifest = json.loads(client.get("/api/export/manifest.json",
                                     params={"run_id": run_id}).content)
    assert manifest["run_id"] == run_id
    assert manifest["score"] == body["score"]
    assert manifest["scope"]["kind"] == "supplement"
    assert manifest["scope"]["baseline_run_id"] == base["run"]["run_id"]

    report = client.get("/api/export/report.csv",
                        params={"run_id": run_id}).content.decode()
    assert f"总分,{body['score']},分" in report

    # 缺失明细与报告总分必须同源：都指向同一 run_id 的快照
    missing = client.get("/api/missing", params={"run_id": run_id}).json()
    assert missing["run_id"] == run_id
    assert missing["account"]["cells_total"] == body["counts"]["cells_total"]


def test_active_version_is_what_report_shows(client):
    base = make_baseline(client)
    supplement = client.post("/api/supplements", json={
        "supplement_id": "SUP-ACTIVE", "dataset": SAMPLE,
        "ops": [{"op": "late", "row": late_row()}]}).json()
    latest = client.get("/api/score/latest").json()
    assert latest["run"]["run_id"] == supplement["run"]["run_id"]
    manifest = json.loads(client.get("/api/export/manifest.json").content)
    assert manifest["scope"]["kind"] == "supplement"

    restored = client.post(
        f"/api/releases/{base['publication']['release_id']}/activate")
    assert restored.status_code == 200
    manifest = json.loads(client.get("/api/export/manifest.json").content)
    assert manifest["run_id"] == base["run"]["run_id"]
    assert manifest["scope"]["kind"] == "baseline"


def test_impact_csv_matches_report_panel(client):
    make_baseline(client)
    body = client.post("/api/supplements", json={
        "supplement_id": "SUP-IMPACT", "dataset": SAMPLE,
        "ops": [{"op": "correct", "ticket_id": "T-2026-000104",
                 "fields": {"satisfaction": "5"}}]}).json()
    text = client.get("/api/export/impact.csv",
                      params={"run_id": body["run"]["run_id"]}).content.decode()
    payload = body["impact"]
    assert f"总分,{payload['score']['before']},{payload['score']['after']}," \
           f"{payload['score']['delta']}" in text
    assert "缺失格合计" in text


def test_supplement_retry_over_http_keeps_versions(client):
    make_baseline(client)
    ops = [{"op": "late", "row": late_row()}]
    first = client.post("/api/supplements", json={
        "supplement_id": "SUP-R", "dataset": SAMPLE, "ops": ops}).json()
    second = client.post("/api/supplements", json={
        "supplement_id": "SUP-R", "dataset": SAMPLE, "ops": ops}).json()
    assert second["replay"] is True
    assert second["run"]["run_id"] == first["run"]["run_id"]
    releases = client.get("/api/releases").json()["releases"]
    assert len(releases) == 2

    conflict = client.post("/api/supplements", json={
        "supplement_id": "SUP-R", "dataset": SAMPLE,
        "ops": [{"op": "late", "row": late_row("T-2029-000002")}]})
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "E_SUPPLEMENT_CONFLICT"


def test_dry_run_preview_over_http(client):
    make_baseline(client)
    before = client.get("/api/releases").json()["releases"]
    preview = client.post("/api/supplements/preview", json={
        "supplement_id": "SUP-PREVIEW", "dataset": SAMPLE,
        "ops": [{"op": "late", "row": late_row()}]})
    assert preview.status_code == 200, preview.text
    assert preview.json()["dry_run"] is True
    assert client.get("/api/releases").json()["releases"] == before


def test_baseline_freeze_blocks_plain_rescore(client):
    make_baseline(client)
    changed = ("ticket_id,customer_id,channel,created_at,first_response_min,"
               "handle_hours,satisfaction,agent,region,priority\n"
               "T-2026-1,001,电话,2026-01-01T00:00:00+08:00,1,1,1,a,b,P1\n")
    blocked = client.post("/api/score/upload", files={
        "file": ("tickets_dirty.csv", changed.encode("utf-8"), "text/csv")})
    assert blocked.status_code == 409
    assert blocked.json()["error"] == "E_BASELINE_FROZEN"


def test_impact_csv_requires_supplement_version(client):
    client.post("/api/score", json={"sample": SAMPLE})
    response = client.get("/api/export/impact.csv")
    assert response.status_code == 400
    assert response.json()["error"] == "E_NO_BASELINE"


def test_impact_score_is_decimal_closed(client):
    base = make_baseline(client)
    body = client.post("/api/supplements", json={
        "supplement_id": "SUP-DEC", "dataset": SAMPLE,
        "ops": [{"op": "withdraw", "ticket_id": "T-2026-000101"}]}).json()
    impact = body["impact"]["score"]
    assert Decimal(impact["before"]) + Decimal(impact["delta"]) == \
        Decimal(impact["after"])
    assert Decimal(impact["delta"]) == Decimal(body["score"]) - Decimal(base["score"])
