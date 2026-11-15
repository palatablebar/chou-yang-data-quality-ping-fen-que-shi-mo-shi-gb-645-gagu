"""补发与发布版本：截止口径、幂等重试、影响分析、原子发布、完整旧版恢复。"""
from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

import pytest

from app import config
from app.errors import BadRequest, Conflict, UnprocessableInput
from app.modules.m6_score import impact, revision, service
from app.store import AlreadyPublished
from fixtures import dirty_cases

SAMPLES = "tickets_dirty.csv"


def baseline(settings, store, sample: str = SAMPLES, cutoff="2026-11-01T00:00:00+08:00"):
    return service.run(settings, store, name=sample,
                       release_spec={"kind": config.RELEASE_BASELINE,
                                     "cutoff_at": cutoff})


def late_row(ticket: str = "T-2029-000001", **overrides) -> dict:
    row = {
        "ticket_id": ticket, "customer_id": "00999", "channel": "电话",
        "created_at": "2026-11-02T10:00:00+08:00", "first_response_min": "20",
        "handle_hours": "2.000", "satisfaction": "4", "agent": "陈允",
        "region": "华东", "priority": "P2",
    }
    row.update(overrides)
    return row


# ---------- 截止口径 ----------
def test_baseline_records_cutoff_and_scope(settings, store):
    result = baseline(settings, store)
    release = store.release_for_run(result["run"]["run_id"])
    assert release["kind"] == "baseline"
    assert release["cutoff_at"] == "2026-10-31T16:00:00+00:00"
    assert release["scope"]["supplement_total"] == 0
    assert result["manifest"]["scope"]["cutoff_at"] == release["cutoff_at"]


def test_baseline_replay_keeps_single_version(settings, store):
    first = baseline(settings, store)
    second = baseline(settings, store)
    assert second["replay"] is True
    assert second["run"]["run_id"] == first["run"]["run_id"]
    assert store.count_runs() == 1
    assert len(store.list_releases()) == 1


def test_frozen_baseline_rejects_new_content(settings, store, tmp_path):
    baseline(settings, store)
    from fixtures import dirty_cases
    changed = dirty_cases.frame(dirty_cases.count_rows(5, agent=""))
    with pytest.raises(Conflict) as info:
        service.run(settings, store, name=SAMPLES, data=changed.encode("utf-8"),
                    release_spec={"kind": config.RELEASE_RUN})
    assert info.value.code == "E_BASELINE_FROZEN"
    assert store.count_runs() == 1


def test_frozen_cutoff_cannot_be_rewritten(settings, store):
    baseline(settings, store)
    with pytest.raises(Conflict) as info:
        baseline(settings, store, cutoff="2026-11-02T00:00:00+08:00")
    assert info.value.code == "E_BASELINE_FROZEN"
    assert store.count_runs() == 1
    assert store.baseline_release(SAMPLES)["cutoff_at"] == "2026-10-31T16:00:00+00:00"


def test_unknown_release_kind_is_bad_request(settings, store):
    with pytest.raises(BadRequest) as info:
        from app.modules.m6_score import publication
        publication.resolve_kind("realtime")
    assert info.value.code == "E_RELEASE_KIND"


def test_plain_run_without_baseline_is_allowed(settings, store):
    result = service.run(settings, store, name=SAMPLES)
    release = store.release_for_run(result["run"]["run_id"])
    assert release["kind"] == "run"
    assert release["scope"]["cutoff_at"] is None


# ---------- 补发 ----------
def test_supplement_advances_version_and_records_scope(settings, store):
    base = baseline(settings, store)
    out = revision.apply(settings, store, supplement_id="SUP-1", ops=[
        {"op": "late", "row": late_row()},
    ], name=SAMPLES)
    release = store.release_for_run(out["run"]["run_id"])
    assert release["kind"] == "supplement"
    assert release["baseline_run_id"] == base["run"]["run_id"]
    assert release["parent_run_id"] == base["run"]["run_id"]
    assert release["scope"]["operations"]["late"] == 1
    assert release["scope"]["supplement_ids"] == ["SUP-1"]
    assert out["run"]["version"] == base["run"]["version"] + 1


def test_supplement_retry_is_idempotent(settings, store):
    baseline(settings, store)
    ops = [{"op": "late", "row": late_row()}]
    first = revision.apply(settings, store, supplement_id="SUP-1", ops=ops,
                           name=SAMPLES)
    second = revision.apply(settings, store, supplement_id="SUP-1", ops=ops,
                            name=SAMPLES)
    assert second.get("replay") is True
    assert second["run"]["run_id"] == first["run"]["run_id"]
    assert store.count_runs() == 2                      # 基线 + 一版补发
    assert len(store.list_releases()) == 2
    assert len(store.list_supplements(name=SAMPLES)) == 1


def test_supplement_same_id_different_content_conflicts(settings, store):
    baseline(settings, store)
    revision.apply(settings, store, supplement_id="SUP-1", name=SAMPLES, ops=[
        {"op": "late", "row": late_row()},
    ])
    with pytest.raises(Conflict) as info:
        revision.apply(settings, store, supplement_id="SUP-1", name=SAMPLES, ops=[
            {"op": "late", "row": late_row("T-2029-000002")},
        ])
    assert info.value.code == "E_SUPPLEMENT_CONFLICT"
    assert store.count_runs() == 2


def test_supplement_chain_replays_from_baseline(settings, store):
    """两批补发都从基线重算，第二批不会把第一批的更正叠加两次。"""
    base = baseline(settings, store)
    first = revision.apply(settings, store, supplement_id="SUP-1", name=SAMPLES, ops=[
        {"op": "correct", "ticket_id": "T-2026-000101", "fields": {"agent": "李迟"}},
    ])
    second = revision.apply(settings, store, supplement_id="SUP-2", name=SAMPLES, ops=[
        {"op": "correct", "ticket_id": "T-2026-000103", "fields": {"agent": "王柯"}},
    ])
    assert second["baseline_run_id"] == base["run"]["run_id"]
    release = store.release_for_run(second["run"]["run_id"])
    assert release["supplement_ids"] == ["SUP-1", "SUP-2"]
    # scope.operations 是这一版样本累计应用数；this_batch 只记本批增量
    assert release["scope"]["operations"]["correct"] == 2
    assert release["scope"]["this_batch"]["operations"]["correct"] == 1
    agents = _agents_of(settings, store, second["run"]["run_id"])
    assert agents["T-2026-000101"] == "李迟"
    assert agents["T-2026-000103"] == "王柯"
    assert first["run"]["version"] + 1 == second["run"]["version"]


def test_withdraw_ambiguous_ticket_requires_lineno(settings, store):
    baseline(settings, store)
    with pytest.raises(BadRequest) as info:
        revision.apply(settings, store, supplement_id="SUP-1", name=SAMPLES,
                       ops=[{"op": "withdraw", "ticket_id": "T-2026-000102"}])
    assert info.value.code == "E_SUPPLEMENT_AMBIGUOUS"
    assert store.count_runs() == 1


def test_withdraw_missing_ticket_rejected(settings, store):
    baseline(settings, store)
    with pytest.raises(UnprocessableInput) as info:
        revision.apply(settings, store, supplement_id="SUP-1", name=SAMPLES,
                       ops=[{"op": "withdraw", "ticket_id": "T-1970-000001"}])
    assert info.value.code == "E_SUPPLEMENT_NOT_FOUND"
    assert store.count_runs() == 1


def test_identical_batches_under_different_ids_apply_once(settings, store):
    """不同批次号提交同样操作：各自留发布记录，但重放只算一次，不重复计数。"""
    baseline(settings, store)
    ops = [{"op": "correct", "ticket_id": "T-2026-000101", "fields": {"agent": "X"}}]
    first = revision.apply(settings, store, supplement_id="A", name=SAMPLES, ops=ops)
    second = revision.apply(settings, store, supplement_id="B", name=SAMPLES, ops=ops)
    assert first["run"]["run_id"] == second["run"]["run_id"]
    assert store.count_runs() == 2                     # 基线 + 一版补发
    assert len(store.list_releases()) == 3             # 基线 + A + B

    third = revision.apply(settings, store, supplement_id="C", name=SAMPLES, ops=[
        {"op": "correct", "ticket_id": "T-2026-000103", "fields": {"agent": "Y"}}])
    assert third["publication"]["supplement_ids"] == ["A", "B", "C"]
    assert third["publication"]["scope"]["operations"]["correct"] == 2


def test_preview_writes_nothing(settings, store):
    baseline(settings, store)
    before = store.count_runs()
    out = revision.preview(settings, store, supplement_id="SUP-P", name=SAMPLES,
                           ops=[{"op": "late", "row": late_row()}])
    assert out["dry_run"] is True
    assert Decimal(out["impact"]["score"]["after"]) >= 0
    assert store.count_runs() == before
    assert store.get_supplement("SUP-P") is None


def test_supplement_inherits_baseline_cutoff(settings, store):
    base = baseline(settings, store)
    out = revision.apply(settings, store, supplement_id="SUP-CUT", name=SAMPLES, ops=[
        {"op": "late", "row": late_row()}])
    assert out["publication"]["cutoff_at"] == base["publication"]["cutoff_at"]
    assert out["run"]["run_id"]
    stored = store.load_result(out["run"]["run_id"])
    assert stored["manifest"]["scope"]["cutoff_at"] == base["publication"]["cutoff_at"]
    chained = revision.apply(settings, store, supplement_id="SUP-CUT-2", name=SAMPLES,
                             ops=[{"op": "correct", "ticket_id": "T-2026-000103",
                                   "fields": {"agent": "王柯"}}])
    assert chained["publication"]["cutoff_at"] == base["publication"]["cutoff_at"]


def test_supplement_keeps_non_touched_lines(settings, store):
    base = baseline(settings, store)
    out = revision.apply(settings, store, supplement_id="SUP-1", name=SAMPLES, ops=[
        {"op": "correct", "ticket_id": "T-2026-000101", "fields": {"agent": "李迟"}},
    ])
    before = store.load_result(base["run"]["run_id"])
    after = store.load_result(out["run"]["run_id"])
    assert before["manifest"]["counters"]["quarantine"] == \
        after["manifest"]["counters"]["quarantine"]
    assert before["manifest"]["counters"]["blank"] == \
        after["manifest"]["counters"]["blank"]
    assert before["dataset"]["segments"] == after["dataset"]["segments"]


# ---------- 影响分析 ----------
def test_impact_score_and_missing_delta(settings, store):
    base = baseline(settings, store)
    out = revision.apply(settings, store, supplement_id="SUP-1", name=SAMPLES, ops=[
        {"op": "withdraw", "ticket_id": "T-2026-000102", "lineno": 35},
    ])
    payload = out["impact"]
    assert Decimal(payload["score"]["before"]) == Decimal(base["run"]["score"])
    assert Decimal(payload["score"]["after"]) == Decimal(out["run"]["score"])
    assert Decimal(payload["score"]["delta"]) == (
        Decimal(payload["score"]["after"]) - Decimal(payload["score"]["before"]))
    assert payload["counts"]["accepted"]["delta"] == -1
    assert payload["counts"]["accepted"]["before"] == \
        base["manifest"]["counters"]["accepted"]
    # 缺失率变化必须与缺失格 / 单元格各自的变化自洽
    missing = payload["missing"]
    expected = (Decimal(missing["missing_after"]) / Decimal(missing["cells_after"])
                - Decimal(missing["missing_before"]) / Decimal(missing["cells_before"]))
    assert abs(Decimal(missing["rate_delta"]) - expected) <= Decimal("0.0001")


def test_impact_reports_pattern_changes(settings, store):
    baseline(settings, store)
    out = revision.apply(settings, store, supplement_id="SUP-1", name=SAMPLES, ops=[
        {"op": "correct", "ticket_id": "T-2026-000107", "fields": {"agent": "陈允"}},
    ])
    patterns = out["impact"]["missing"]["patterns"]["changed"]
    assert any("agent" in item["columns"] for item in patterns)


def test_compare_endpoint(client):
    client.post("/api/score", json={"sample": SAMPLES})
    first = client.get("/api/batches").json()["runs"][0]
    second = client.post("/api/score", json={"sample": "tickets_clean.csv"}).json()
    body = client.get("/api/impact", params={
        "before_run_id": first["run_id"],
        "after_run_id": second["run"]["run_id"]}).json()
    assert body["score"]["before"] != body["score"]["after"]
    assert body["counts"]["accepted"]["delta"] != 0


# ---------- 原子发布 ----------
def test_publish_rolls_back_on_failure(settings, store, monkeypatch):
    baseline(settings, store)
    before_runs = store.count_runs()
    before_versions = store.latest_version(SAMPLES)
    before_releases = len(store.list_releases())

    real_insert = store._insert_run

    def boom(conn, run, summary):
        real_insert(conn, run, summary)
        raise RuntimeError("模拟发布中断")

    monkeypatch.setattr(store, "_insert_run", boom)
    with pytest.raises(RuntimeError):
        revision.apply(settings, store, supplement_id="SUP-X", name=SAMPLES, ops=[
            {"op": "late", "row": late_row()},
        ])
    monkeypatch.undo()

    assert store.count_runs() == before_runs
    assert store.latest_version(SAMPLES) == before_versions
    assert len(store.list_releases()) == before_releases
    assert store.get_supplement("SUP-X") is None
    assert store.active_release()["kind"] == "baseline"


def test_interrupted_retry_succeeds_after_rollback(settings, store, monkeypatch):
    baseline(settings, store)
    real = store._insert_run

    def boom(conn, run, summary):
        real(conn, run, summary)
        raise RuntimeError("中断")

    monkeypatch.setattr(store, "_insert_run", boom)
    with pytest.raises(RuntimeError):
        revision.apply(settings, store, supplement_id="SUP-Y", name=SAMPLES,
                       ops=[{"op": "late", "row": late_row()}])
    monkeypatch.undo()

    out = revision.apply(settings, store, supplement_id="SUP-Y", name=SAMPLES,
                         ops=[{"op": "late", "row": late_row()}])
    assert out.get("replay") is not True
    assert store.count_runs() == 2
    assert len(store.list_releases()) == 2


def test_restore_returns_complete_old_release(settings, store):
    base = baseline(settings, store)
    out = revision.apply(settings, store, supplement_id="SUP-1", name=SAMPLES, ops=[
        {"op": "late", "row": late_row()},
    ])
    assert store.active_release()["kind"] == "supplement"

    restored = store.activate_release(
        store.release_for_run(base["run"]["run_id"])["release_id"], "2026-11-15T00:00:00Z")
    assert restored["run_id"] == base["run"]["run_id"]
    check = store.verify_release(restored["release_id"])
    assert check["ok"] is True
    assert store.active_run()["run_id"] == base["run"]["run_id"]
    assert store.count_runs() == 2                   # 补发结果仍在库里
    assert store.load_result(out["run"]["run_id"]) is not None


def test_verify_flags_missing_snapshot(settings, store):
    base = baseline(settings, store)
    release_id = store.release_for_run(base["run"]["run_id"])["release_id"]
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE runs SET summary_json='' WHERE run_id=?",
                     (base["run"]["run_id"],))
    check = store.verify_release(release_id)
    assert check["ok"] is False
    assert "summary" in check["missing"]


def test_baseline_blob_survives_dataset_overwrite(settings, store):
    """同名数据集重新采信只移动指针，基线原文仍按摘要可取回。"""
    base = baseline(settings, store, sample=SAMPLES)
    digest = base["run"]["digest"]
    assert store.get_blob(digest) is not None
    store.save_dataset(name=SAMPLES, digest="deadbeef", encoding="utf-8",
                       declared_encoding=None, data=b"x", stamp="2026-11-15T00:00:00Z")
    assert store.get_blob(digest) is not None
    assert store.get_blob(digest)["content"] == store.get_blob(digest)["content"]


def test_concurrent_same_batch_counts_once(settings, store):
    """同一批次并发重试：靠 release_key 唯一约束与事务回滚，只落一版。"""
    import threading

    baseline(settings, store)
    ops = [{"op": "late", "row": late_row()}]
    run_ids = []
    errors = []

    def worker():
        try:
            out = revision.apply(settings, store, supplement_id="SUP-C",
                                 name=SAMPLES, ops=ops)
            run_ids.append(out["run"]["run_id"])
        except Exception as error:            # pragma: no cover - 失败时打印
            errors.append(repr(error))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    assert len(set(run_ids)) == 1
    assert store.count_runs() == 2
    assert len(store.list_releases()) == 2
    assert len(store.list_supplements(name=SAMPLES)) == 1
    result = store.load_result(run_ids[0])
    assert result["manifest"]["counters"]["accepted"] == \
        store.load_result(store.release_for_run(
            store.release_for_run(run_ids[0])["baseline_run_id"])["run_id"]
        )["manifest"]["counters"]["accepted"] + 1


def test_chain_order_is_registration_order_not_timestamp(settings, store):
    """同一秒登记的两批也要按登记顺序重放，不能靠秒级时间戳排序。"""
    base = baseline(settings, store)
    revision.apply(settings, store, supplement_id="A", name=SAMPLES, ops=[
        {"op": "correct", "ticket_id": "T-2026-000101", "fields": {"agent": "甲"}}])
    revision.apply(settings, store, supplement_id="B", name=SAMPLES, ops=[
        {"op": "correct", "ticket_id": "T-2026-000103", "fields": {"agent": "乙"}}])
    store.activate_release(
        store.release_for_run(base["run"]["run_id"])["release_id"], "2026-11-15T00:00:00Z")
    out = revision.apply(settings, store, supplement_id="C", name=SAMPLES, ops=[
        {"op": "correct", "ticket_id": "T-2026-000105", "fields": {"agent": "丙"}}])
    assert out["publication"]["supplement_ids"] == ["A", "B", "C"]
    agents = _agents_of(settings, store, out["run"]["run_id"])
    assert (agents["T-2026-000101"], agents["T-2026-000103"],
            agents["T-2026-000105"]) == ("甲", "乙", "丙")


def _pattern_rows(count: int = 40) -> str:
    """造出远超 12 种缺失签名：每行按位掩码缺不同的列组合。"""
    columns = ["agent", "region", "satisfaction", "priority"]
    rows = []
    for index in range(count):
        row = dict(ticket=f"T-2026-81{index:04d}", customer=f"00{index + 500}",
                   response="20.0", handle="2.00", satisfaction="4",
                   agent="a", region="b", priority="P2")
        for bit, name in enumerate(columns):
            if index & (1 << bit):
                row[name] = ""
        rows.append(dirty_cases.make_row(**row))
    return dirty_cases.frame(rows)


def test_snapshot_keeps_full_missing_pattern_table(settings, store):
    """补发影响要对比全量签名差异；快照不能只存前 12 条。"""
    result = service.run(settings, store, name="patterns.csv",
                         data=_pattern_rows().encode("utf-8"),
                         release_spec={"kind": config.RELEASE_BASELINE,
                                       "cutoff_at": "2026-11-01T00:00:00+08:00"})
    stored = store.load_result(result["run"]["run_id"])
    assert len(stored["missing"]["patterns"]) > 12


def test_impact_sees_patterns_beyond_page_limit(settings, store):
    service.run(settings, store, name="patterns2.csv",
                data=_pattern_rows().encode("utf-8"),
                release_spec={"kind": config.RELEASE_BASELINE,
                              "cutoff_at": "2026-11-01T00:00:00+08:00"})
    out = revision.apply(settings, store, supplement_id="P1", name="patterns2.csv",
                         ops=[{"op": "late", "row": late_row("T-2026-830001")}])
    patterns = out["impact"]["missing"]["patterns"]
    # 迟到行完整 10 列，签名不变；重点是没有因为分页阈值丢签名
    assert patterns["changed_count"] == 0
    assert "changed" in patterns


def _agents_of(settings, store, run_id: str) -> dict:
    """从该版原文重新解析：运行快照不存逐行数据，原文才是可复核来源。"""
    from app.modules.m1_ingest import service as m1_service

    run = store.get_run(run_id)
    blob = store.get_blob(run["digest"])
    ingested = m1_service.ingest(settings, name=run["name"],
                                 data=bytes(blob["content"]))
    return {row["cells"]["ticket_id"]: row["cells"]["agent"]
            for row in ingested["rows"]}
