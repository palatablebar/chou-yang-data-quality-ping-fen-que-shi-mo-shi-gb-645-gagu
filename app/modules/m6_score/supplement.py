"""补发批次：把截止后到达的迟到记录、撤回工单与字段更正落成一次可复算的增量。

约定：
- 补发批次由 `supplement_id` 唯一标识；同一批次重试只复用结果，不新增版本、
  不重复计数；同 id 不同内容直接 409，绝不静默覆盖；
- 三种操作：
  - `late`     追加一条完整记录（10 列，按契约列名给值）；
  - `withdraw` 按工单号删除记录；同号多行时必须给 `lineno` 消除歧义；
  - `correct`  按工单号改字段；同号多行时必须给 `lineno`；未提到的列保留原值；
- 操作直接作用在基线原文的物理行上：未触及的行保持原样（含隔离行、空行、
  重复表头分段），补发只改它声明要改的地方；
- 任一操作定位不到行、列名未知、迟到行列数不对 → 整批拒绝，不落半版。
"""
from __future__ import annotations

import csv
import io

from ... import config, manifest, reasons
from ...errors import BadRequest, UnprocessableInput
from ..m1_ingest import reader, sniff


def _quote(cells: list[str]) -> str:
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="").writerow(cells)
    return buffer.getvalue()


def canonical_ops(ops) -> list[dict]:
    """规范化：键排序、去空白，保证同一批次内容得到同一摘要。"""
    if not isinstance(ops, list) or not ops:
        raise BadRequest("补发批次需要至少一条操作（late / withdraw / correct）",
                         code="E_SUPPLEMENT_EMPTY")
    result = []
    for index, raw in enumerate(ops):
        if not isinstance(raw, dict):
            raise BadRequest(f"第 {index + 1} 条操作不是对象", code="E_SUPPLEMENT_INVALID")
        kind = str(raw.get("op") or "").strip()
        if kind not in config.SUPPLEMENT_OPS:
            raise BadRequest(
                f"第 {index + 1} 条操作类型未知：{kind!r}，只支持 "
                + " / ".join(config.SUPPLEMENT_OPS), code="E_SUPPLEMENT_INVALID")
        ticket = str(raw.get("ticket_id") or "").strip()
        lineno = raw.get("lineno")
        if lineno not in (None, ""):
            try:
                lineno = int(lineno)
            except (TypeError, ValueError):
                raise BadRequest(f"第 {index + 1} 条 lineno 不是整数",
                                 code="E_SUPPLEMENT_INVALID") from None
        else:
            lineno = None
        item = {"op": kind, "ticket_id": ticket, "lineno": lineno, "note": ""}
        if kind == "late":
            fields = raw.get("row") or raw.get("fields") or {}
            if not isinstance(fields, dict) or not fields:
                raise BadRequest(f"第 {index + 1} 条迟到记录缺少 row",
                                 code="E_SUPPLEMENT_INVALID")
            item["fields"] = {str(key): "" if value is None else str(value)
                              for key, value in fields.items()}
        elif kind == "correct":
            fields = raw.get("fields") or raw.get("row") or {}
            if not isinstance(fields, dict) or not fields:
                raise BadRequest(f"第 {index + 1} 条更正缺少 fields",
                                 code="E_SUPPLEMENT_INVALID")
            item["fields"] = {str(key): "" if value is None else str(value)
                              for key, value in fields.items()}
        if kind in ("withdraw", "correct") and not ticket and lineno is None:
            raise BadRequest(f"第 {index + 1} 条需要 ticket_id 或 lineno",
                             code="E_SUPPLEMENT_INVALID")
        item["note"] = str(raw.get("note") or "")
        result.append(item)
    return result


def ops_digest(ops: list[dict], base_digest: str) -> str:
    parts = [base_digest]
    for item in ops:
        parts.append(item["op"])
        parts.append(item["ticket_id"])
        parts.append("" if item["lineno"] is None else item["lineno"])
        for key in sorted(item.get("fields", {})):
            parts.append(f"{key}={item['fields'][key]}")
    return manifest.digest_parts([str(part) for part in parts])


def content_digest(ops: list[dict]) -> str:
    """只看本批操作内容，不含基线：用来识别「不同批次号、同样操作」的重复补发。"""
    parts = []
    for item in ops:
        parts.append(item["op"])
        parts.append(item["ticket_id"])
        parts.append("" if item["lineno"] is None else item["lineno"])
        for key in sorted(item.get("fields", {})):
            parts.append(f"{key}={item['fields'][key]}")
    return manifest.digest_parts(parts)


def _column_name(key: str, index: int) -> str:
    if key in config.COLUMN_MAP:
        return key
    for column, aliases in config.ALIASES.items():
        if key in aliases:
            return column
    raise BadRequest(f"第 {index + 1} 条字段未知：{key}", code="E_SUPPLEMENT_INVALID")


def _column_position(header: sniff.HeaderInfo, name: str) -> int:
    position = header.indexes.get(name)
    if position is None:
        raise UnprocessableInput(f"基线表头没有列 {name}，无法补发",
                                 code="E_SUPPLEMENT_INVALID")
    return position


def apply(text: str, ops: list[dict], delimiter: str = ",") -> tuple[str, dict]:
    """在基线原文上应用补发操作，返回新文本与逐条处理摘要。"""
    lines = reader.physical_lines(text)
    records = reader.iter_records(text, delimiter)
    if not records:
        raise UnprocessableInput("基线没有可解析的记录", code="E_SUPPLEMENT_INVALID")
    header = sniff.map_header(records[0].cells)
    if header.missing:
        raise UnprocessableInput("基线表头缺列，无法补发", code="E_SUPPLEMENT_INVALID")
    ticket_index = header.indexes.get("ticket_id")
    data_records = records[1:]

    spans: dict[int, tuple[int, int, reader.Record]] = {}
    for record in data_records:
        start = record.lineno - 1
        spans[start] = (start, start + record.physical, record)

    # 先做定位与校验，全部通过后才改文本
    edits: dict[int, tuple[str, list[str]]] = {}
    deletes: set[int] = set()
    appended: list[str] = []
    applied: list[dict] = []

    def locate(item: dict) -> list[tuple[int, tuple[int, int, reader.Record]]]:
        found = []
        for start, span in spans.items():
            record = span[2]
            if item["lineno"] is not None:
                if record.lineno != item["lineno"]:
                    continue
                if item["ticket_id"] and ticket_index is not None \
                        and header.value(record, "ticket_id").strip() != item["ticket_id"]:
                    continue
            elif item["ticket_id"]:
                if ticket_index is None:
                    continue
                if header.value(record, "ticket_id").strip() != item["ticket_id"]:
                    continue
            found.append((start, span))
        found.sort(key=lambda pair: pair[1][2].lineno)
        return found

    for index, item in enumerate(ops):
        if item["op"] == "late":
            cells = [""] * header.width
            for key, value in item["fields"].items():
                position = _column_position(header, _column_name(key, index))
                cells[position] = value
            if ticket_index is not None and not cells[ticket_index].strip():
                raise BadRequest(f"第 {index + 1} 条迟到记录缺少 ticket_id",
                                 code="E_SUPPLEMENT_INVALID")
            appended.append(_quote(cells))
            applied.append({"op": "late",
                            "ticket_id": cells[ticket_index].strip()
                            if ticket_index is not None else "",
                            "lineno": None, "fields": sorted(item["fields"])})
            continue

        found = locate(item)
        if not found:
            raise UnprocessableInput(
                f"第 {index + 1} 条{'撤回' if item['op'] == 'withdraw' else '更正'}"
                f"定位不到记录：{item['ticket_id'] or item['lineno']}",
                code="E_SUPPLEMENT_NOT_FOUND")
        if len(found) > 1 and item["lineno"] is None:
            raise BadRequest(
                f"第 {index + 1} 条匹配到 {len(found)} 行（行号 "
                + "、".join(str(span[2].lineno) for _, span in found)
                + "），请带 lineno 指定", code="E_SUPPLEMENT_AMBIGUOUS")
        if item["op"] == "withdraw":
            for start, span in found:
                deletes.add(start)
                applied.append({"op": "withdraw", "ticket_id": item["ticket_id"],
                                "lineno": span[2].lineno, "fields": []})
        else:
            for start, span in found:
                record = span[2]
                cells = list(record.cells)
                while len(cells) < header.width:
                    cells.append("")
                changed = False
                for key, value in item["fields"].items():
                    position = _column_position(header, _column_name(key, index))
                    if cells[position] != value:
                        changed = True
                    cells[position] = value
                if changed:
                    # 真正改了值才重写该行；原样的行保持原始字节与行尾，
                    # 避免「值没变但格式被重排」导致摘要漂移。
                    edits[start] = (span[0], span[1], _quote(cells))
                applied.append({"op": "correct", "ticket_id": item["ticket_id"],
                                "lineno": span[2].lineno,
                                "fields": sorted(item["fields"]),
                                "changed": changed})

    for start in deletes & set(edits):
        raise BadRequest(f"同一次补发既撤回又更正行 {spans[start][2].lineno}",
                         code="E_SUPPLEMENT_CONFLICT")

    rebuilt: list[str] = []
    position = 0
    while position < len(lines):
        if position in deletes:
            span = spans[position]
            position = span[1]
            continue
        if position in edits:
            _, end, replacement = edits[position]
            rebuilt.append(replacement + "\n")
            position = end
            continue
        rebuilt.append(lines[position])
        position += 1
    if appended:
        if rebuilt and not rebuilt[-1].endswith(("\n", "\r")):
            rebuilt[-1] = rebuilt[-1] + "\n"
        rebuilt.extend(line + "\n" for line in appended)
    summary = {
        "late": sum(1 for item in applied if item["op"] == "late"),
        "withdraw": sum(1 for item in applied if item["op"] == "withdraw"),
        "correct": sum(1 for item in applied if item["op"] == "correct"),
        "ops": applied,
        "code_labels": {op: reasons.label(f"K_SUPPLEMENT_{op.upper()}")
                        for op in config.SUPPLEMENT_OPS},
    }
    return "".join(rebuilt), summary


def apply_many(text: str, batches: list[list[dict]],
               delimiter: str = ",") -> tuple[str, dict]:
    """按登记顺序逐批应用，合并成一份逐条处理摘要。

    后一批定位到前一批刚写入的行也能命中，因为每一批都作用在上一批的文本
    结果上；空批次直接跳过，不影响已有行。
    """
    current = text
    merged: list[dict] = []
    for batch in batches:
        if not batch:
            continue
        current, summary = apply(current, batch, delimiter)
        merged.extend(summary["ops"])
    return current, {
        "late": sum(1 for item in merged if item["op"] == "late"),
        "withdraw": sum(1 for item in merged if item["op"] == "withdraw"),
        "correct": sum(1 for item in merged if item["op"] == "correct"),
        "ops": merged,
        "code_labels": {op: reasons.label(f"K_SUPPLEMENT_{op.upper()}")
                        for op in config.SUPPLEMENT_OPS},
    }


def scope_of(applied: dict, ops_total: int) -> dict:
    """样本范围里的操作计数是「这一版样本累计应用了多少」，不是本批增量。"""
    return {
        "late": applied["late"],
        "withdraw": applied["withdraw"],
        "correct": applied["correct"],
        "ops_total": ops_total,
        "tickets": [item["ticket_id"] for item in applied["ops"] if item["ticket_id"]],
    }
