"""补发影响：把两版运行差成「总分变化 + 缺失模式变化」，全部 Decimal 对齐。

对比对象是链条根（截止基线）与当前版本，补发了多少次都能看到累计影响；
每一项都同时给 before / after / delta，缺省值为 0 而不是省略，避免读者把
「没变化」误读成「没统计」。
"""
from __future__ import annotations

from decimal import Decimal

ZERO = Decimal("0")
QUANT = Decimal("0.01")


def _dec(value) -> Decimal:
    if value is None or value == "":
        return ZERO
    return Decimal(str(value))


def _delta(before, after) -> str:
    return str((_dec(after) - _dec(before)).quantize(QUANT))


def build(before: dict, after: dict) -> dict:
    return {
        "score": _score(before, after),
        "counts": _counts(before, after),
        "missing": _missing(before, after),
        "duplicates": _duplicates(before, after),
        "outliers": _outliers(before, after),
        "summary": _summary(before, after),
    }


def _score(before: dict, after: dict) -> dict:
    left = before["score"]
    right = after["score"]
    dims = []
    left_dims = {item["key"]: item for item in left["dims"]}
    for item in right["dims"]:
        old = left_dims.get(item["key"], {})
        dims.append({
            "key": item["key"], "label": item["label"],
            "before": old.get("penalty", "0"), "after": item["penalty"],
            "delta": _delta(old.get("penalty", "0"), item["penalty"]),
            "rate_before": old.get("rate", "0"), "rate_after": item["rate"],
            "rate_delta": str((_dec(item["rate"]) - _dec(old.get("rate")))
                              .quantize(Decimal("0.0001"))),
        })
    return {
        "before": left["score"], "after": right["score"],
        "delta": _delta(left["score"], right["score"]),
        "penalty_before": left["penalty_sum"], "penalty_after": right["penalty_sum"],
        "penalty_delta": _delta(left["penalty_sum"], right["penalty_sum"]),
        "grade_before": left["grade"], "grade_after": right["grade"],
        "grade_label_before": left["grade_label"],
        "grade_label_after": right["grade_label"],
        "dims": dims,
    }


def _counts(before: dict, after: dict) -> dict:
    left = before["manifest"]["counters"]
    right = after["manifest"]["counters"]
    keys = ("raw_records", "accepted", "quarantine", "blank", "rows_unique",
            "rows_dup", "cells_total", "cells_missing", "cells_value",
            "cells_format_bad", "outlier_cells", "tested_cells")
    return {key: {"before": left.get(key, 0), "after": right.get(key, 0),
                  "delta": right.get(key, 0) - left.get(key, 0)}
            for key in keys}


def _missing(before: dict, after: dict) -> dict:
    left = {item["name"]: item for item in before["missing"]["account"]["by_column"]}
    columns = []
    for item in after["missing"]["account"]["by_column"]:
        old = left.get(item["name"], {})
        columns.append({
            "name": item["name"], "label": item["label"],
            "missing_before": old.get("missing", 0),
            "missing_after": item["missing"],
            "missing_delta": item["missing"] - old.get("missing", 0),
            "rate_before": old.get("missing_rate", "0.0000"),
            "rate_after": item["missing_rate"],
            "rate_delta": str((_dec(item["missing_rate"])
                               - _dec(old.get("missing_rate")))
                              .quantize(Decimal("0.0001"))),
        })
    return {
        "cells_before": before["missing"]["account"]["cells_total"],
        "cells_after": after["missing"]["account"]["cells_total"],
        "missing_before": before["missing"]["account"]["missing"],
        "missing_after": after["missing"]["account"]["missing"],
        "missing_delta": (after["missing"]["account"]["missing"]
                          - before["missing"]["account"]["missing"]),
        "rate_before": _rate(before),
        "rate_after": _rate(after),
        "rate_delta": str((_dec(_rate(after)) - _dec(_rate(before)))
                          .quantize(Decimal("0.0001"))),
        "columns": columns,
        "runs": _runs(before, after),
        "patterns": _patterns(before, after),
        "co_missing": _co_missing(before, after),
    }


def _rate(result: dict) -> str:
    account = result["missing"]["account"]
    total = account["cells_total"]
    if not total:
        return "0.0000"
    return str((Decimal(account["missing"]) / Decimal(total))
               .quantize(Decimal("0.0001")))


def _runs(before: dict, after: dict) -> dict:
    def index(result):
        return {(item["column"], item["start_lineno"]): item
                for item in result["missing"]["runs"]}
    old, new = index(before), index(after)
    added = [new[key] for key in sorted(new) if key not in old]
    removed = [old[key] for key in sorted(old) if key not in new]
    return {
        "before": len(old), "after": len(new),
        "delta": len(new) - len(old),
        "added": added[:20], "removed": removed[:20],
    }


def _patterns(before: dict, after: dict) -> dict:
    def index(result):
        return {tuple(item["columns"]): item for item in result["missing"]["patterns"]}
    old, new = index(before), index(after)
    rows = []
    for key in sorted(set(old) | set(new), key=lambda keys: (len(keys), keys)):
        left = old.get(key, {}).get("count", 0)
        right = new.get(key, {}).get("count", 0)
        if left == right:
            continue
        item = new.get(key) or old.get(key)
        rows.append({"columns": list(key), "labels": item["labels"],
                     "before": left, "after": right, "delta": right - left})
    rows.sort(key=lambda item: (-abs(item["delta"]), item["columns"]))
    return {"changed": rows[:12], "changed_count": len(rows)}


def _co_missing(before: dict, after: dict) -> dict:
    def index(result):
        return {(item["left"], item["right"]): item["count"]
                for item in result["missing"]["co_missing"]}
    old, new = index(before), index(after)
    rows = []
    for key in sorted(set(old) | set(new)):
        left, right = old.get(key, 0), new.get(key, 0)
        if left == right:
            continue
        rows.append({"left": key[0], "right": key[1],
                     "before": left, "after": right, "delta": right - left})
    rows.sort(key=lambda item: (-abs(item["delta"]), item["left"], item["right"]))
    return {"changed": rows[:12], "changed_count": len(rows)}


def _duplicates(before: dict, after: dict) -> dict:
    return {
        "unique_before": before["duplicates"]["unique_rows"],
        "unique_after": after["duplicates"]["unique_rows"],
        "duplicate_before": before["duplicates"]["duplicate_rows"],
        "duplicate_after": after["duplicates"]["duplicate_rows"],
        "conflicts_before": len(before["duplicates"]["conflicts"]),
        "conflicts_after": len(after["duplicates"]["conflicts"]),
    }


def _outliers(before: dict, after: dict) -> dict:
    return {
        "cells_before": before["outliers"]["counts"]["outlier_cells"],
        "cells_after": after["outliers"]["counts"]["outlier_cells"],
        "tested_before": before["outliers"]["counts"]["tested_cells"],
        "tested_after": after["outliers"]["counts"]["tested_cells"],
    }


def _summary(before: dict, after: dict) -> dict:
    """一句话结论所需的最小事实集；文案由前端按这些事实拼接。"""
    score_delta = _dec(_delta(before["score"]["score"], after["score"]["score"]))
    missing_delta = after["missing"]["account"]["missing"] - \
        before["missing"]["account"]["missing"]
    return {
        "score_delta": str(score_delta),
        "direction": ("up" if score_delta > 0 else
                      "down" if score_delta < 0 else "flat"),
        "missing_delta": missing_delta,
        "grade_changed": before["score"]["grade"] != after["score"]["grade"],
    }
