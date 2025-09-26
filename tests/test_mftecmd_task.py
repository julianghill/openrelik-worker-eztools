"""Tests for the MFTECmd task configuration logic."""

import base64
import copy
import csv
import json
import os

import pytest

from pathlib import Path

from openrelik_worker_common.file_utils import create_output_file
from openrelik_worker_common.task_utils import create_task_result

from src import mftecmd_task
from src.mftecmd_task import _convert_mftecmd_csv_to_timesketch


@pytest.fixture
def fake_run_ez_tool(monkeypatch, tmp_path):
    """Provide a stub for _run_ez_tool that records calls and emits sample CSV data."""

    captured_calls = {}

    def _fake(**kwargs):
        captured_calls.update(kwargs)
        os.makedirs(kwargs["output_path"], exist_ok=True)
        output_file = create_output_file(
            output_base_path=kwargs["output_path"],
            display_name="MFTECmd_output.csv",
            extension="csv",
            data_type="text/csv",
        )
        with open(output_file.path, "w", encoding="utf-8") as fh:
            fh.write(
                "EntryNumber,SequenceNumber,ParentPath,FileName,Size,FileCreated0x10,FileModified0x10\n"
                "42,3,\\\\Users\\\\Bob,document.txt,1337,2025-01-01 12:00:00,2025-01-02 01:02:03\n"
            )

        return create_task_result(
            output_files=[output_file.to_dict()],
            workflow_id=kwargs["workflow_id"],
            command="stub",
            meta={},
        )

    monkeypatch.setattr(mftecmd_task, "_run_ez_tool", _fake)
    return captured_calls


def _decode_result(result_b64: str) -> dict:
    decoded = base64.b64decode(result_b64.encode("utf-8")).decode("utf-8")
    return json.loads(decoded)


def test_mftecmd_bodyfile_configures_arguments_and_pattern(fake_run_ez_tool):
    """Bodyfile output sanitizes config and synchronizes tool expectations."""

    baseline_config = copy.deepcopy(mftecmd_task.MFTECMD_OUTPUT_FORMAT_CONFIG)

    result = mftecmd_task.mftecmd_command.run(
        pipe_result=None,
        input_files=[{"path": "/tmp/$MFT", "display_name": "$MFT"}],
        output_path="/tmp/output",
        workflow_id="wf-123",
        task_config={
            "output_format": "body",
            "body_drive_letter": "d:",
            "bodyfile_name": "nested/custom.body",
            "mftecmd_arguments": "--foo bar",
        },
    )

    called_config = fake_run_ez_tool["task_config"]
    assert called_config["body_drive_letter"] == "D"
    assert called_config["bodyfile_name"] == "custom.body"
    assert "--bdl D" in called_config["mftecmd_arguments"]
    assert "--bodyf custom.body" in called_config["mftecmd_arguments"]

    tool_config = fake_run_ez_tool["tool_output_format_config"]
    assert tool_config["body"]["pattern"] == "custom.body"

    assert mftecmd_task.MFTECMD_OUTPUT_FORMAT_CONFIG == baseline_config

    # Ensure the stubbed result is still returned in base64 format.
    result_dict = _decode_result(result)
    assert result_dict["workflow_id"] == "wf-123"


def test_mftecmd_invalid_format_defaults_to_csv(fake_run_ez_tool):
    """Invalid output formats fall back to CSV expectations."""

    result = mftecmd_task.mftecmd_command.run(
        pipe_result=None,
        input_files=[{"path": "/tmp/$MFT", "display_name": "$MFT"}],
        output_path="/tmp/output",
        workflow_id="wf-456",
        task_config={"output_format": "invalid"},
    )

    called_config = fake_run_ez_tool["task_config"]
    assert called_config["output_format"] == "csv"
    assert fake_run_ez_tool["tool_output_format_config"]["body"]["pattern"] == "output.body"

    result_dict = _decode_result(result)
    assert result_dict["workflow_id"] == "wf-456"


def test_mftecmd_timesketch_conversion(fake_run_ez_tool, tmp_path):
    """CSV output can be rewritten into a Timesketch-friendly timeline."""

    result = mftecmd_task.mftecmd_command.run(
        pipe_result=None,
        input_files=[{"path": "/tmp/$MFT", "display_name": "$MFT"}],
        output_path=str(tmp_path),
        workflow_id="wf-789",
        task_config={
            "output_format": "csv",
            "timesketch_ready_csv": True,
        },
    )

    result_dict = _decode_result(result)
    output_files = result_dict["output_files"]
    assert len(output_files) == 1

    output_meta = output_files[0]
    assert output_meta["display_name"].lower().endswith("_timesketch.csv")
    assert output_meta["data_type"] == "openrelik:eztools:mftecmd:timesketch_csv"

    with open(output_meta["path"], encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    assert rows, "The Timesketch CSV should contain at least one event row"
    assert rows[0]["datetime"].endswith("+0000")
    assert rows[0]["timestamp_desc"].startswith("MFTECmd")
    assert "document.txt" in rows[0]["message"]
    assert rows[0]["source_short"] == "$MFT"
    assert rows[0]["source_long"] == "MFTECmd $MFT Parser"
    assert "zone_identifier" in rows[0]
    assert rows[0]["zone_identifier"] == ""
    assert "alternate_data_stream" in rows[0]
    assert rows[0]["zone_identifier_raw"] == ""
    assert rows[0]["zone_id"] == ""
    assert rows[0]["zone_host_url"] == ""
    assert rows[0]["zone_referrer_url"] == ""


def test_mftecmd_zone_identifier_parsing(tmp_path):
    """Zone.Identifier ADS contents are broken into searchable fields."""

    csv_path = Path(tmp_path) / "sample.csv"
    csv_content = (
        "EntryNumber,SequenceNumber,ParentPath,FileName,Size,FileCreated0x10,ZoneIdContents\n"
        "86683,5,\\\\Users\\\\simon.stark\\\\Downloads,KAPE.zip:Zone.Identifier,124,2024-02-13 16:39:06.000Z,"
        "\"[ZoneTransfer]\\r\\nZoneId=3\\r\\nReferrerUrl=https://justbeamit.com/\\r\\nHostUrl=https://eu.justbeamit.com:8443/download?token=ymma5\\r\\n\"\n"
    )
    csv_path.write_text(csv_content, encoding="utf-8")

    converted_bytes, count = _convert_mftecmd_csv_to_timesketch(str(csv_path))
    assert count == 1

    rows = list(csv.DictReader(converted_bytes.decode("utf-8").splitlines()))
    assert rows[0]["zone_identifier"] == "3"
    assert rows[0]["zone_id"] == "3"
    assert rows[0]["zone_host_url"].startswith("https://eu.justbeamit.com")
    assert rows[0]["zone_referrer_url"] == "https://justbeamit.com/"
    assert "zone_identifier_raw" in rows[0]
    assert "ZoneTransfer" in rows[0]["zone_identifier_raw"]
