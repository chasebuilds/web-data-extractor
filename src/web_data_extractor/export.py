"""Writing the result as CSV files and one XLSX workbook, then reading it back."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from .runner import RunResult

RUNLOG_COLUMNS = [
    "seq",
    "url",
    "kind",
    "status",
    "cache_hit",
    "robots",
    "attempt",
    "waited_seconds",
    "fetched_at",
    "note",
]
_FORMULA_STARTS = ("=", "+", "-", "@")
# `--out` ending in one of these writes Records in that format, next to the usual files.
DATA_FORMATS = (".jsonl", ".parquet")


class ExportError(RuntimeError):
    """Raised when an output format cannot be written, for example a missing extra."""


def require_format(out: str | Path) -> None:
    """Fail before any crawl when the chosen output format cannot be written."""
    if Path(out).suffix.lower() == ".parquet":
        try:
            import pyarrow  # noqa: F401
        except ImportError as exc:
            raise ExportError(
                "Parquet output needs pyarrow: pip install 'web-data-extractor[parquet]'"
            ) from exc


def _csv_safe(value: Any) -> Any:
    # Scraped text that starts like a formula would run when a client opens the CSV.
    if isinstance(value, str) and value.startswith(_FORMULA_STARTS):
        return "'" + value
    return "" if value is None else value


def _tables(result: RunResult) -> dict[str, tuple[list[str], list[list[Any]]]]:
    def rows(dicts: list[dict], columns: list[str]) -> list[list[Any]]:
        return [[d.get(c) for c in columns] for d in dicts]

    return {
        "Records": (result.record_columns, rows(result.records, result.record_columns)),
        "Rejected": (result.rejected_columns, rows(result.rejected, result.rejected_columns)),
        "RunLog": (RUNLOG_COLUMNS, rows([asdict(e) for e in result.log], RUNLOG_COLUMNS)),
        "Summary": (["metric", "value"], [list(pair) for pair in result.summary]),
    }


def _write_csv(path: Path, columns: list[str], rows: list[list[Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows([_csv_safe(v) for v in row] for row in rows)


def _write_xlsx(path: Path, tables: dict[str, tuple[list[str], list[list[Any]]]]) -> None:
    workbook = Workbook()
    workbook.remove(workbook.active)
    for title, (columns, rows) in tables.items():
        sheet = workbook.create_sheet(title)
        sheet.append(columns)
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for row in rows:
            sheet.append(row)
            for cell in sheet[sheet.max_row]:
                # openpyxl would store a leading "=" as a live formula.
                if isinstance(cell.value, str) and cell.data_type == "f":
                    cell.data_type = "s"
        sheet.freeze_panes = "A2"
        if rows:
            sheet.auto_filter.ref = sheet.dimensions
        for index, column in enumerate(columns, start=1):
            longest = max([len(column), *(len(str(r[index - 1] or "")) for r in rows)])
            sheet.column_dimensions[get_column_letter(index)].width = min(60, longest + 2)
    workbook.save(path)


def _verify(path: Path, tables: dict[str, tuple[list[str], list[list[Any]]]]) -> list[str]:
    """Reopen the workbook and compare what is on disk with what was meant to be written."""
    problems = []
    workbook = load_workbook(path, read_only=True)
    for title in ("Records", "Rejected", "RunLog"):
        columns, rows = tables[title]
        on_disk = list(workbook[title].iter_rows(values_only=True))
        if [*on_disk[0]] != columns:
            problems.append(f"{title}: header differs")
        if len(on_disk) - 1 != len(rows):
            problems.append(f"{title}: {len(on_disk) - 1} rows on disk, expected {len(rows)}")
    workbook.close()
    records_columns, records = tables["Records"]
    for needed in ("source_url", "fetched_at"):
        index = records_columns.index(needed)
        if any(not row[index] for row in records):
            problems.append(f"Records: a row has no {needed}")
    return problems


def _records(result: RunResult) -> list[dict[str, Any]]:
    columns = result.record_columns
    return [{c: r.get(c) for c in columns} for r in result.records]


def write_jsonl(result: RunResult, path: Path) -> None:
    """One JSON object per record, columns in the same order as the Records sheet."""
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in _records(result):
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    with path.open(encoding="utf-8") as handle:
        written = sum(1 for _ in handle)
    if written != len(result.records):
        raise RuntimeError(f"{path}: {written} lines on disk, expected {len(result.records)}")


def write_parquet(result: RunResult, path: Path) -> None:
    require_format(path)
    import pyarrow as pa
    import pyarrow.parquet as pq

    columns = result.record_columns
    if result.records:
        table = pa.Table.from_pylist(_records(result)).select(columns)
    else:
        table = pa.table({c: pa.array([], type=pa.string()) for c in columns})
    pq.write_table(table, path)
    on_disk = pq.read_table(path)
    if on_disk.num_rows != len(result.records) or on_disk.column_names != columns:
        raise RuntimeError(f"{path}: rows or columns on disk differ from the Records sheet")


def write_outputs(result: RunResult, out: str | Path) -> dict[str, Path]:
    """Write the workbook and CSVs into a folder.

    When `out` names a .jsonl or .parquet file, Records are also written there in
    that format, and the workbook and CSVs go in the same folder as that file.
    """
    target = Path(out)
    data_format = target.suffix.lower() if target.suffix.lower() in DATA_FORMATS else ""
    if data_format:
        require_format(target)
    out = target.parent if data_format else target
    out.mkdir(parents=True, exist_ok=True)
    tables = _tables(result)
    xlsx_path = out / f"{result.config.name}.xlsx"
    _write_xlsx(xlsx_path, tables)
    problems = _verify(xlsx_path, tables)
    verdict = "passed" if not problems else "FAILED: " + "; ".join(problems)
    result.summary.append(("export_check", verdict))
    tables = _tables(result)
    _write_xlsx(xlsx_path, tables)

    paths = {"xlsx": xlsx_path}
    for title, filename in (
        ("Records", "records.csv"),
        ("Rejected", "rejected.csv"),
        ("RunLog", "run_log.csv"),
        ("Summary", "summary.csv"),
    ):
        paths[title] = out / filename
        _write_csv(paths[title], *tables[title])
    if data_format == ".jsonl":
        write_jsonl(result, target)
        paths["jsonl"] = target
    elif data_format == ".parquet":
        write_parquet(result, target)
        paths["parquet"] = target
    if problems:
        raise RuntimeError("export check failed: " + "; ".join(problems))
    return paths
