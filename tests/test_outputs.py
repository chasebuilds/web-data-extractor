"""`--out data.jsonl` and `--out data.parquet`, next to the workbook and CSV files."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from web_data_extractor.cli import main
from web_data_extractor.config import load_config
from web_data_extractor.export import ExportError, require_format, write_outputs
from web_data_extractor.fetch import PoliteFetcher
from web_data_extractor.runner import run

REPO = Path(__file__).resolve().parent.parent
FIXTURE_CONFIG = REPO / "configs" / "fixture.yaml"


@pytest.fixture
def result(fixture_site, fake_clock):
    config = load_config(FIXTURE_CONFIG, {"base_url": fixture_site.base_url})
    fetcher = PoliteFetcher(
        config.politeness,
        allowed_hosts=config.allowed_hosts,
        sleep=fake_clock.sleep,
        clock=fake_clock.clock,
    )
    with fetcher:
        return run(config, fetcher=fetcher)


def test_jsonl_holds_one_record_per_line_in_sheet_column_order(result, tmp_path):
    result.records[0]["name"] = "Café Lamp"  # non-ASCII text survives as is
    paths = write_outputs(result, tmp_path / "data.jsonl")
    lines = paths["jsonl"].read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(result.records) == 60
    rows = [json.loads(line) for line in lines]
    assert all(list(row) == result.record_columns for row in rows)
    assert rows[0]["name"] == "Café Lamp"
    assert "Café" in lines[0]
    assert isinstance(rows[0]["price"], float)
    assert rows == [{c: r.get(c) for c in result.record_columns} for r in result.records]
    # The usual audit files sit in the same folder as the data file.
    assert paths["xlsx"].parent == tmp_path
    assert (tmp_path / "records.csv").exists() and (tmp_path / "run_log.csv").exists()


def test_parquet_round_trips_with_types_and_column_order(result, tmp_path):
    pq = pytest.importorskip("pyarrow.parquet")
    paths = write_outputs(result, tmp_path / "data.parquet")
    table = pq.read_table(paths["parquet"])
    assert table.num_rows == len(result.records)
    assert table.column_names == result.record_columns
    assert str(table.schema.field("price").type) == "double"
    assert str(table.schema.field("rating").type) == "int64"
    expected = [{c: r.get(c) for c in result.record_columns} for r in result.records]
    assert table.to_pylist() == expected
    assert (tmp_path / "fixture-catalog.xlsx").exists()


def test_parquet_with_no_records_still_has_the_columns(result, tmp_path):
    pq = pytest.importorskip("pyarrow.parquet")
    empty = replace(result, records=[])
    paths = write_outputs(empty, tmp_path / "empty.parquet")
    table = pq.read_table(paths["parquet"])
    assert table.num_rows == 0
    assert table.column_names == result.record_columns


def test_folder_output_writes_no_data_file(result, tmp_path):
    paths = write_outputs(result, tmp_path / "plain")
    assert "jsonl" not in paths and "parquet" not in paths
    assert not list((tmp_path / "plain").glob("*.jsonl"))


def test_demo_writes_jsonl_from_the_command_line(tmp_path, virtual_time, capsys):
    out = tmp_path / "run" / "data.jsonl"
    code = main(
        [
            "demo",
            "--config", str(FIXTURE_CONFIG),
            "--site", str(REPO / "fixture_site"),
            "--port", "0",
            "--out", str(out),
        ]
    )  # fmt: skip
    assert code == 0
    assert len(out.read_text(encoding="utf-8").splitlines()) == 60
    assert (out.parent / "fixture-catalog.xlsx").exists()
    assert "Wrote 60 records to" in capsys.readouterr().out


def test_missing_pyarrow_fails_before_any_request(scripted, monkeypatch, tmp_path, capsys):
    monkeypatch.setitem(sys.modules, "pyarrow", None)  # makes `import pyarrow` fail
    with pytest.raises(ExportError, match="pyarrow"):
        require_format("data.parquet")
    code = main(
        [
            "run", str(FIXTURE_CONFIG),
            "--var", f"base_url={scripted.base_url}",
            "--out", str(tmp_path / "data.parquet"),
        ]
    )  # fmt: skip
    assert code == 2
    assert "pyarrow" in capsys.readouterr().err
    assert scripted.hits == []
