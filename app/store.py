"""SQLite 存储：数据集原文、运行记录、发布版本与补发事件。

约定：
- 数据集按名字唯一：同名文件重新采信即移动指针，原始字节另按 sha256 存
  不可变副本（`dataset_blobs`），旧运行永远能取回自己的输入，不因覆盖丢失；
- 运行记录按 (dataset, digest, rules_version) 幂等：同输入重跑不新增行；
- 一次发布（原始字节 + 数据集指针 + 运行记录 + 发布版本 + 补发事件）在
  单个 `BEGIN IMMEDIATE` 事务里完成：中途失败整笔回滚，库里仍是上一版完整
  快照，不会留下半条运行或半个指针；
- 同名数据集同一时刻只有一个 active 发布版本；旧版本只失去指针，不删除；
- 补发批次按 `supplement_id` 唯一：同一批次重试命中已有记录，不新增版本、
  不重复计数；
- 分数与比率落 TEXT（Decimal 字符串），不落 float；
- 页面、缺失明细与导出读同一个 run_id 快照；未指定时统一取 active 发布版本。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    name TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    encoding TEXT NOT NULL,
    declared_encoding TEXT,
    bytes INTEGER NOT NULL,
    content BLOB NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dataset_blobs (
    digest TEXT PRIMARY KEY,
    encoding TEXT NOT NULL,
    declared_encoding TEXT,
    bytes INTEGER NOT NULL,
    content BLOB NOT NULL,
    first_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    digest TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    version INTEGER NOT NULL,
    score TEXT NOT NULL,
    grade TEXT NOT NULL,
    rows_in INTEGER NOT NULL,
    rows_accepted INTEGER NOT NULL,
    rows_unique INTEGER NOT NULL,
    rows_dup INTEGER NOT NULL,
    rows_quarantine INTEGER NOT NULL,
    rows_blank INTEGER NOT NULL,
    cells_total INTEGER NOT NULL,
    cells_missing INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    seen_seq INTEGER NOT NULL,
    summary_json TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_identity ON runs(name, digest, rules_version);
CREATE INDEX IF NOT EXISTS idx_runs_version ON runs(name, version DESC);

CREATE TABLE IF NOT EXISTS releases (
    release_id TEXT PRIMARY KEY,
    release_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    run_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    parent_run_id TEXT,
    baseline_run_id TEXT,
    cutoff_at TEXT,
    supplement_ids_json TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    published_at TEXT NOT NULL,
    seq INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_releases_name ON releases(name, seq DESC);
CREATE INDEX IF NOT EXISTS idx_releases_run ON releases(run_id, seq DESC);

CREATE TABLE IF NOT EXISTS dataset_active (
    name TEXT PRIMARY KEY,
    release_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    active_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS supplements (
    supplement_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    ops_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    base_run_id TEXT,
    result_run_id TEXT,
    received_at TEXT NOT NULL,
    applied_at TEXT,
    seq INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_supplements_name ON supplements(name, seq);
"""


class AlreadyPublished(Exception):
    """同一发布键在并发下已被写入；调用方回滚并复用既有版本。"""

    def __init__(self, release: dict):
        super().__init__(release.get("release_id", ""))
        self.release = release


class RunAlreadyExists(Exception):
    """同一 (数据集, 摘要, 规则版本) 的运行在并发下已被写入。

    调用方应改走「复用既有运行」的路径，而不是把它当成发布冲突。
    """

    def __init__(self, run: dict):
        super().__init__(run.get("run_id", ""))
        self.run = run


def _dec(text):
    return None if text is None else Decimal(str(text))


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).resolve().parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
        self._backfill_blobs()

    def _backfill_blobs(self) -> None:
        """旧库只有 datasets 指针：把现存原文补存成不可变副本，旧版本才能补发。"""
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO dataset_blobs (digest,encoding,declared_encoding,bytes,
                        content,first_seen_at)
                   SELECT d.digest,d.encoding,d.declared_encoding,d.bytes,
                          d.content,d.updated_at
                   FROM datasets d
                   WHERE NOT EXISTS (SELECT 1 FROM dataset_blobs b
                                     WHERE b.digest=d.digest)
                   ON CONFLICT(digest) DO NOTHING""")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        """写事务：BEGIN IMMEDIATE 先拿写锁，整笔提交或整笔回滚。"""
        conn = sqlite3.connect(self.path, timeout=15.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ---------- 数据集 ----------
    def save_dataset(self, *, name: str, digest: str, encoding: str,
                     declared_encoding: str | None, data: bytes, stamp: str) -> None:
        """存原始字节：GB18030 文件重算摘要必须与首次采信逐位相同。"""
        with self.transaction() as conn:
            self._save_blob(conn, digest=digest, encoding=encoding,
                            declared_encoding=declared_encoding, data=data,
                            stamp=stamp)
            self._point_dataset(conn, name=name, digest=digest, encoding=encoding,
                                declared_encoding=declared_encoding, data=data,
                                stamp=stamp)

    def _save_blob(self, conn, *, digest: str, encoding: str,
                   declared_encoding: str | None, data: bytes, stamp: str) -> None:
        conn.execute(
            """INSERT INTO dataset_blobs (digest,encoding,declared_encoding,bytes,
                    content,first_seen_at) VALUES (?,?,?,?,?,?)
               ON CONFLICT(digest) DO NOTHING""",
            (digest, encoding, declared_encoding, len(data), data, stamp))

    def _point_dataset(self, conn, *, name: str, digest: str, encoding: str,
                       declared_encoding: str | None, data: bytes,
                       stamp: str) -> None:
        conn.execute(
            """INSERT INTO datasets (name,digest,encoding,declared_encoding,bytes,
                    content,updated_at) VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 digest=excluded.digest, encoding=excluded.encoding,
                 declared_encoding=excluded.declared_encoding,
                 bytes=excluded.bytes, content=excluded.content,
                 updated_at=excluded.updated_at""",
            (name, digest, encoding, declared_encoding, len(data), data, stamp))

    def get_dataset(self, name: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM datasets WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def get_blob(self, digest: str) -> dict | None:
        """按摘要取不可变原文：旧运行复算时永远拿得到当时的字节。"""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM dataset_blobs WHERE digest=?",
                               (digest,)).fetchone()
        return dict(row) if row else None

    def list_datasets(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT name,digest,encoding,bytes,updated_at FROM datasets ORDER BY name"
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 运行记录 ----------
    def latest_version(self, name: str) -> int:
        with self.connect() as conn:
            return self._latest_version(conn, name)

    def _latest_version(self, conn, name: str) -> int:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM runs WHERE name=?", (name,)).fetchone()
        return int(row["v"] or 0)

    def find_run(self, name: str, digest: str, rules_version: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE name=? AND digest=? AND rules_version=?",
                (name, digest, rules_version)).fetchone()
        return dict(row) if row else None

    def save_run(self, run: dict, summary: dict) -> None:
        with self.transaction() as conn:
            self._insert_run(conn, run, summary)

    def _insert_run(self, conn, run: dict, summary: dict) -> None:
        conn.execute(
            """INSERT INTO runs (run_id,name,digest,rules_version,version,score,grade,
                    rows_in,rows_accepted,rows_unique,rows_dup,rows_quarantine,
                    rows_blank,cells_total,cells_missing,created_at,finished_at,
                    last_seen_at,seen_seq,summary_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run["run_id"], run["name"], run["digest"], run["rules_version"],
             int(run["version"]), str(run["score"]), run["grade"],
             int(run["rows_in"]), int(run["rows_accepted"]), int(run["rows_unique"]),
             int(run["rows_dup"]), int(run["rows_quarantine"]),
             int(run["rows_blank"]), int(run["cells_total"]),
             int(run["cells_missing"]), run["created_at"], run["finished_at"],
             run["created_at"], self._next_seq(conn),
             json.dumps(summary, ensure_ascii=False)))

    def touch_run(self, run_id: str, stamp: str) -> None:
        """命中幂等复用时刷新「最近一次查看」，导航回各页仍停在这条记录上。"""
        with self.transaction() as conn:
            conn.execute("UPDATE runs SET last_seen_at=?, seen_seq=? WHERE run_id=?",
                         (stamp, self._next_seq(conn), run_id))

    def _next_seq(self, conn) -> int:
        row = conn.execute("SELECT COALESCE(MAX(seen_seq), 0) + 1 AS n FROM runs").fetchone()
        return int(row["n"])

    def latest_run(self, name: str | None = None) -> dict | None:
        with self.connect() as conn:
            if name:
                row = conn.execute(
                    "SELECT * FROM runs WHERE name=? ORDER BY version DESC LIMIT 1",
                    (name,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM runs ORDER BY seen_seq DESC LIMIT 1"
                ).fetchone()
        return dict(row) if row else None

    def get_run(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY seen_seq DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(row) for row in rows]

    def load_result(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT summary_json FROM runs WHERE run_id=?",
                               (run_id,)).fetchone()
        if not row or not row["summary_json"]:
            return None
        try:
            return json.loads(row["summary_json"])
        except (TypeError, ValueError):
            return None

    def count_runs(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"])

    # ---------- 发布版本 ----------
    def publish(self, result: dict, *, dataset: dict | None = None,
                blob: dict | None = None, release: dict | None = None,
                supplements: tuple = ()) -> int:
        """原子发布：版本号在事务内取，运行、指针、发布与补发记录同进同退。"""
        run = result["run"]
        try:
            with self.transaction() as conn:
                version = self._latest_version(conn, run["name"]) + 1
                run["version"] = version
                result["manifest"]["version"] = version
                if release is not None:
                    release["version"] = version
                    release["run_id"] = run["run_id"]
                if blob is not None:
                    self._save_blob(conn, **blob)
                if dataset is not None:
                    self._point_dataset(conn, **dataset)
                self._insert_run(conn, run, result)
                if release is not None:
                    self._insert_release(conn, release)
                    self._activate(conn, release["name"], release["release_id"],
                                   run["run_id"], release["published_at"])
                for item in supplements:
                    self._insert_supplement(conn, {**item,
                                                   "result_run_id": run["run_id"]})
        except sqlite3.IntegrityError as error:
            message = str(error)
            if release is not None and "release_key" in message:
                existing = self.release_by_key(release["release_key"])
                if existing is not None:
                    raise AlreadyPublished(existing) from None
            if "runs.name, runs.digest, runs.rules_version" in message:
                existing_run = self.find_run(run["name"], run["digest"],
                                             run["rules_version"])
                if existing_run is not None:
                    raise RunAlreadyExists(existing_run) from None
            raise
        return version

    def publish_existing(self, run_id: str, *, dataset: dict | None = None,
                         blob: dict | None = None, release: dict,
                         supplements: tuple = ()) -> dict:
        """内容与既有运行逐位相同时：复用运行，只挂一条新发布（不新增版本）。"""
        try:
            with self.transaction() as conn:
                row = conn.execute(
                    "SELECT version FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if row is None:
                    raise KeyError(run_id)
                release["run_id"] = run_id
                release["version"] = int(row["version"])
                if blob is not None:
                    self._save_blob(conn, **blob)
                if dataset is not None:
                    self._point_dataset(conn, **dataset)
                self._insert_release(conn, release)
                self._activate(conn, release["name"], release["release_id"],
                               run_id, release["published_at"])
                for item in supplements:
                    self._insert_supplement(conn, {**item, "result_run_id": run_id})
        except sqlite3.IntegrityError as error:
            message = str(error)
            if "release_key" in message:
                existing = self.release_by_key(release["release_key"])
                if existing is not None:
                    raise AlreadyPublished(existing) from None
            raise
        return release

    def _insert_release(self, conn, release: dict) -> None:
        conn.execute(
            """INSERT INTO releases (release_id,release_key,name,run_id,version,kind,
                    parent_run_id,baseline_run_id,cutoff_at,supplement_ids_json,
                    scope_json,published_at,seq)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (release["release_id"], release["release_key"], release["name"],
             release["run_id"], int(release["version"]), release["kind"],
             release.get("parent_run_id"), release.get("baseline_run_id"),
             release.get("cutoff_at"),
             json.dumps(release.get("supplement_ids", []), ensure_ascii=False),
             json.dumps(release.get("scope", {}), ensure_ascii=False),
             release["published_at"], self._next_release_seq(conn)))

    def _next_release_seq(self, conn) -> int:
        row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM releases").fetchone()
        return int(row["n"])

    def _activate(self, conn, name: str, release_id: str, run_id: str,
                  stamp: str) -> None:
        seq = self._next_active_seq(conn)
        conn.execute(
            """INSERT INTO dataset_active (name,release_id,run_id,updated_at,active_seq)
               VALUES (?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 release_id=excluded.release_id, run_id=excluded.run_id,
                 updated_at=excluded.updated_at, active_seq=excluded.active_seq""",
            (name, release_id, run_id, stamp, seq))

    def _next_active_seq(self, conn) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(active_seq), 0) + 1 AS n FROM dataset_active").fetchone()
        return int(row["n"])

    # ---------- 补发批次 ----------
    def _insert_supplement(self, conn, item: dict) -> None:
        conn.execute(
            """INSERT INTO supplements (supplement_id,name,ops_digest,payload_json,
                    base_run_id,result_run_id,received_at,applied_at,seq)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (item["supplement_id"], item["name"], item["ops_digest"],
             json.dumps(item.get("payload", {}), ensure_ascii=False),
             item.get("base_run_id"), item.get("result_run_id"),
             item["received_at"], item["applied_at"],
             self._next_supplement_seq(conn)))

    def _next_supplement_seq(self, conn) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM supplements").fetchone()
        return int(row["n"])

    def get_supplement(self, supplement_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM supplements WHERE supplement_id=?",
                               (supplement_id,)).fetchone()
        return dict(row) if row else None

    def list_supplements(self, name: str | None = None, limit: int = 50) -> list[dict]:
        """按登记先后倒序返回；累计重放要翻回正序。"""
        with self.connect() as conn:
            if name:
                rows = conn.execute(
                    """SELECT * FROM supplements WHERE name=?
                       ORDER BY seq DESC LIMIT ?""", (name, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM supplements ORDER BY seq DESC LIMIT ?",
                    (limit,)).fetchall()
        return [dict(row) for row in rows]

    # ---------- 发布查询 ----------
    def release_by_key(self, release_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM releases WHERE release_key=?",
                               (release_key,)).fetchone()
        return self._release_row(row) if row else None

    def get_release(self, release_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM releases WHERE release_id=?",
                               (release_id,)).fetchone()
        return self._release_row(row) if row else None

    def release_for_run(self, run_id: str) -> dict | None:
        """同一运行可能被多条发布引用（补发内容未变时复用运行），取最新一条。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM releases WHERE run_id=? ORDER BY seq DESC LIMIT 1",
                (run_id,)).fetchone()
        return self._release_row(row) if row else None

    def active_release(self, name: str | None = None) -> dict | None:
        with self.connect() as conn:
            if name:
                row = conn.execute(
                    """SELECT r.* FROM releases r
                       JOIN dataset_active a ON a.release_id=r.release_id
                       WHERE a.name=?""", (name,)).fetchone()
            else:
                row = conn.execute(
                    """SELECT r.* FROM releases r
                       JOIN dataset_active a ON a.release_id=r.release_id
                       ORDER BY a.active_seq DESC, r.seq DESC LIMIT 1""").fetchone()
        return self._release_row(row) if row else None

    def baseline_release(self, name: str) -> dict | None:
        """数据集的第一条截止基线；之后内容变化必须通过补发推进。"""
        with self.connect() as conn:
            row = conn.execute(
                """SELECT * FROM releases WHERE name=? AND kind=?
                   ORDER BY seq ASC LIMIT 1""",
                (name, "baseline")).fetchone()
        return self._release_row(row) if row else None

    def active_run(self, name: str | None = None) -> dict | None:
        release = self.active_release(name)
        return self.get_run(release["run_id"]) if release else None

    def activate_release(self, release_id: str, stamp: str) -> dict:
        """运维恢复：把 active 指针指回某个完整旧版（不删除任何历史）。"""
        release = self.get_release(release_id)
        if release is None:
            raise KeyError(release_id)
        with self.transaction() as conn:
            self._activate(conn, release["name"], release["release_id"],
                           release["run_id"], stamp)
        return release

    def verify_release(self, release_id: str) -> dict:
        """发布完整性自检：运行、快照、原文、对账四样都在才算「完整旧版」。"""
        release = self.get_release(release_id)
        if release is None:
            return {"release_id": release_id, "ok": False, "missing": ["release"]}
        missing = []
        run = self.get_run(release["run_id"])
        if run is None:
            missing.append("run")
        result = self.load_result(release["run_id"]) if run else None
        if result is None:
            missing.append("summary")
        if run is not None and self.get_blob(run["digest"]) is None:
            missing.append("blob")
        if result is not None and not result.get("reconcile", {}).get("all_balanced"):
            missing.append("reconcile")
        return {"release_id": release_id, "ok": not missing, "missing": missing,
                "run_id": release["run_id"], "kind": release.get("kind"),
                "version": release.get("version")}

    def list_releases(self, name: str | None = None, limit: int = 30) -> list[dict]:
        with self.connect() as conn:
            if name:
                rows = conn.execute(
                    "SELECT * FROM releases WHERE name=? ORDER BY seq DESC LIMIT ?",
                    (name, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM releases ORDER BY seq DESC LIMIT ?",
                    (limit,)).fetchall()
        return [self._release_row(row) for row in rows]

    @staticmethod
    def _release_row(row) -> dict:
        item = dict(row)
        item["supplement_ids"] = json.loads(item.pop("supplement_ids_json") or "[]")
        item["scope"] = json.loads(item.pop("scope_json") or "{}")
        return item
