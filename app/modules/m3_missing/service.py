"""M3 编排：单元格账 + 连续缺失段 + 缺失模式矩阵。"""
from __future__ import annotations

from . import cells, matrix, runs


def build(profile: dict) -> dict:
    account = cells.account(profile)
    found = runs.find_runs(profile)
    return {
        "account": account,
        "runs": found,
        "run_summary": runs.summary(found),
        "longest_run": runs.longest(found),
        # 快照里保留完整模式表：补发影响对比要看全量差异，页面摘要再截断。
        "patterns": matrix.pattern_table(profile, limit=None),
        "co_missing": matrix.co_missing(profile, limit=None),
    }


def summary(payload: dict, limit: int = 10) -> dict:
    account = payload["account"]
    columns = sorted(account["by_column"],
                     key=lambda item: (-item["missing"], item["name"]))
    return {
        "cells_total": account["cells_total"],
        "missing": account["missing"],
        "filled": account["filled"],
        "closed": account["closed"],
        "columns": columns[:limit],
        "runs": payload["run_summary"],
        "patterns": payload["patterns"][:6],
        "co_missing": payload["co_missing"][:6],
    }
