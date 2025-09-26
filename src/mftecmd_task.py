import base64
import copy
import csv
import io
import json
import os
import shlex
from datetime import datetime, timezone

from .app import celery
from .utils import _run_ez_tool
from openrelik_worker_common.task_utils import encode_dict_to_base64

# --- MFTECmd Task ---
MFTECMD_TASK_NAME = "openrelik-worker-eztools.tasks.mftecmd"
MFTECMD_TASK_METADATA = {
    "display_name": "EZTool: MFTECmd (MFT Parser)",
    "description": "Runs MFTECmd.exe from Eric Zimmermann's EZTools to parse NTFS $MFT files. Output format must be 'csv', 'json', or 'body' (bodyfile). If not specified, defaults to 'csv'. If 'body' is selected, a drive letter is required (defaults to 'c').",
    "task_config": [
        {
            "name": "output_format",
            "label": "Output Format",
            "description": "Select the output format. MFTECmd supports 'csv', 'json', or 'body' (bodyfile). If not specified, defaults to 'csv'.",
            "type": "select",
            "items": [
                "csv",
                "json",
                "body",
            ],
            "default": "csv",
            "required": False,
        },
        {
            "name": "body_drive_letter",
            "label": "Bodyfile Drive Letter",
            "description": "Drive letter to use for bodyfile output (required for bodyfile, e.g., 'c').",
            "type": "text",
            "default": "c",
            "required": False,
            "visible_if": {"output_format": "body"}
        },
        {
            "name": "bodyfile_name",
            "label": "Bodyfile Output Name",
            "description": "File name for the bodyfile output (e.g., 'output.body'). If not set, defaults to 'output.body'.",
            "type": "text",
            "default": "output.body",
            "required": False,
            "visible_if": {"output_format": "body"}
        },
        {
            "name": "timesketch_ready_csv",
            "label": "Format CSV for Timesketch",
            "description": "When enabled and CSV output is selected, rewrite the MFTECmd results into a Timesketch-friendly CSV timeline.",
            "type": "checkbox",
            "default": False,
            "required": False,
            "visible_if": {"output_format": "csv"},
        },
    ],
}

MFTECMD_OUTPUT_FORMAT_CONFIG = {
    "csv": {
        "flag": "--csv",
        # MFTECmd --csv expects a directory and creates a file like YYYYMMDDHHMMSS_MFTECmd_$MFT_Output.csv inside it.
        "pattern": "*_MFTECmd_*_Output.csv",
        "output_target_type": "directory",
    },
    "json": {
        "flag": "--json",
        # MFTECmd --json expects a directory and creates a file inside it.
        "pattern": "*_MFTECmd_Output.json",
        "output_target_type": "directory",
    },
    "body": {
        "flag": "--body",
        # MFTECmd --body expects a directory and creates a file like output.body (or user-supplied name) inside it.
        "pattern": "output.body",
        "output_target_type": "directory",
    },
}

@celery.task(bind=True, name=MFTECMD_TASK_NAME, metadata=MFTECMD_TASK_METADATA)
def mftecmd_command(
    self,
    pipe_result: str = None,
    input_files: list = None,
    output_path: str = None,
    workflow_id: str = None,
    task_config: dict = None,
) -> str:
    """Run MFTECmd on input MFT files.

    Args:
        pipe_result: Base64-encoded result from the previous Celery task, if any.
        input_files: List of input file dictionaries (unused if pipe_result exists).
        output_path: Path to the output directory.
        workflow_id: ID of the current workflow.
        task_config: User configuration for the task.

    Returns:
        Base64-encoded dictionary containing task results.
    """
    effective_task_config = dict(task_config or {})
    output_format_config = copy.deepcopy(MFTECMD_OUTPUT_FORMAT_CONFIG)
    # Default to 'csv' if not specified or invalid
    output_format = effective_task_config.get("output_format")
    if output_format not in ("csv", "json", "body"):
        output_format = "csv"
    effective_task_config["output_format"] = output_format

    # If bodyfile output, normalize config and ensure required arguments are added.
    if output_format == "body":
        raw_drive_letter = effective_task_config.get("body_drive_letter", "c")
        drive_letter = str(raw_drive_letter).strip() if raw_drive_letter is not None else ""
        drive_letter = drive_letter.rstrip(":\\/") or "c"
        drive_letter = drive_letter[0].upper() if drive_letter else "C"
        if not drive_letter.isalpha():
            drive_letter = "C"
        effective_task_config["body_drive_letter"] = drive_letter

        raw_bodyfile_name = effective_task_config.get("bodyfile_name") or "output.body"
        bodyfile_name = os.path.basename(str(raw_bodyfile_name).strip()) or "output.body"
        effective_task_config["bodyfile_name"] = bodyfile_name

        existing_args = effective_task_config.get("mftecmd_arguments", "")
        args_tokens = shlex.split(existing_args) if existing_args else []
        if "--bdl" not in args_tokens:
            args_tokens.extend(["--bdl", drive_letter])
        if "--bodyf" not in args_tokens:
            args_tokens.extend(["--bodyf", bodyfile_name])
        effective_task_config["mftecmd_arguments"] = " ".join(args_tokens)

        output_format_config["body"]["pattern"] = bodyfile_name

    dotnet_executable_path = os.path.expanduser("/usr/bin/dotnet")
    mftecmd_dll_path = "/opt/MFTECmd_built_from_source/MFTECmd.dll"

    executable_list_for_mftecmd = [
        dotnet_executable_path,
        mftecmd_dll_path,
    ]

    result = _run_ez_tool(
        executable_command_list=executable_list_for_mftecmd,
        tool_display_name="MFTECmd.exe",
        tool_file_argument_flag="-f",
        tool_specific_args_key="mftecmd_arguments",
        tool_output_format_config=output_format_config,
        pipe_result=pipe_result,
        input_files=input_files,
        output_path=output_path,
        workflow_id=workflow_id,
        task_config=effective_task_config,
    )

    if (
        output_format == "csv"
        and effective_task_config.get("timesketch_ready_csv")
        and result
    ):
        try:
            decoded = base64.b64decode(result.encode("utf-8")).decode("utf-8")
            result_dict = json.loads(decoded)
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"Unable to decode MFTECmd task result for Timesketch conversion: {exc}")
            return result

        transformed = False
        for output_file in result_dict.get("output_files", []):
            output_path_for_file = output_file.get("path")
            if not output_path_for_file or not output_path_for_file.lower().endswith(".csv"):
                continue
            converted_bytes, event_count = _convert_mftecmd_csv_to_timesketch(output_path_for_file)
            if not converted_bytes:
                continue
            try:
                with open(output_path_for_file, "wb") as fh:
                    fh.write(converted_bytes)
            except OSError as exc:
                print(f"Failed writing Timesketch-formatted CSV for MFTECmd: {exc}")
                continue

            original_display_name = output_file.get("display_name") or "mftecmd.csv"
            base_name, extension = os.path.splitext(original_display_name)
            if not base_name.lower().endswith("_timesketch"):
                output_file["display_name"] = f"{base_name}_timesketch{extension or '.csv'}"
            else:
                output_file["display_name"] = original_display_name
            output_file["data_type"] = "openrelik:eztools:mftecmd:timesketch_csv"
            transformed = True
            print(
                f"MFTECmd Timesketch conversion wrote {event_count} timeline rows for '{output_file['display_name']}'."
            )

        if transformed:
            result = encode_dict_to_base64(result_dict)

    return result


def _convert_mftecmd_csv_to_timesketch(csv_path: str) -> tuple[bytes | None, int]:
    """Convert MFTECmd CSV output into a Timesketch-friendly timeline.

    Args:
        csv_path: Path to the MFTECmd-generated CSV file.

    Returns:
        A tuple containing the converted CSV bytes (or None if conversion
        could not be performed) and the number of timeline rows generated.
    """
    try:
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as input_fh:
            reader = csv.DictReader(input_fh)
            fieldnames = reader.fieldnames or []
            timestamp_columns = [
                name
                for name in fieldnames
                if name
                and any(token in name.lower() for token in ("0x10", "0x30"))
            ]

            if not timestamp_columns:
                print(
                    "MFTECmd Timesketch conversion skipped: no recognizable timestamp columns found."
                )
                return None, 0

            output_handle = io.StringIO()
            timesketch_fields = [
                "datetime",
                "timestamp_desc",
                "message",
                "source",
                "source_short",
                "source_long",
                "host",
                "user",
                "display_name",
                "filename",
                "filepath",
                "entry_number",
                "sequence_number",
                "size",
                "zone_identifier",
                "zone_identifier_raw",
                "zone_id",
                "zone_host_url",
                "zone_referrer_url",
                "zone_source_url",
                "alternate_data_stream",
                "extra_attributes",
            ]
            writer = csv.DictWriter(output_handle, fieldnames=timesketch_fields)
            writer.writeheader()

            event_rows = 0
            for row in reader:
                if not row:
                    continue

                filename = (row.get("FileName") or row.get("Name") or "").strip()
                parent_path = (
                    row.get("ParentPath")
                    or row.get("Directory")
                    or row.get("Path")
                    or row.get("FullPath")
                    or ""
                ).strip()
                if parent_path and filename:
                    if parent_path.endswith("\\") or parent_path.endswith("/"):
                        full_path = f"{parent_path}{filename}"
                    else:
                        full_path = f"{parent_path}\\{filename}"
                else:
                    full_path = filename or parent_path

                entry_number = (row.get("EntryNumber") or "").strip()
                sequence_number = (row.get("SequenceNumber") or "").strip()
                size = (row.get("Size") or row.get("PhysicalSize") or "").strip()
                user = (row.get("OwnerSID") or row.get("Owner") or "").strip()
                zone_identifier = (
                    row.get("ZoneIdentifier")
                    or row.get("ZoneId")
                    or row.get("ZoneID")
                    or ""
                )
                zone_contents_raw = (
                    row.get("ZoneIdContents")
                    or row.get("ZoneIdentifierContents")
                    or ""
                )
                zone_contents = _parse_zone_identifier_contents(zone_contents_raw)
                zone_id_value = (
                    zone_contents.get("zoneid")
                    or zone_contents.get("zone_id")
                    or ""
                )
                host_url = (
                    zone_contents.get("hosturl")
                    or zone_contents.get("zonehosturl")
                    or zone_contents.get("url")
                    or ""
                )
                referrer_url = (
                    zone_contents.get("referrerurl")
                    or zone_contents.get("zonereferrerurl")
                    or ""
                )
                source_url = (
                    zone_contents.get("sourceurl")
                    or zone_contents.get("zonetransferurl")
                    or ""
                )
                if not zone_identifier and zone_id_value:
                    zone_identifier = zone_id_value
                ads_name = (
                    row.get("StreamName")
                    or row.get("AlternateDataStream")
                    or row.get("ADS")
                    or ""
                )

                message_parts = [full_path or "<unknown path>"]
                if size:
                    message_parts.append(f"Size: {size}")
                if entry_number or sequence_number:
                    message_parts.append(
                        f"Entry: {entry_number}{':' if sequence_number else ''}{sequence_number}"
                    )
                if zone_identifier:
                    message_parts.append(f"ZoneIdentifier: {zone_identifier}")
                if ads_name:
                    message_parts.append(f"ADS: {ads_name}")
                if host_url:
                    message_parts.append(f"HostUrl: {host_url}")
                if referrer_url:
                    message_parts.append(f"ReferrerUrl: {referrer_url}")
                message = " | ".join(part for part in message_parts if part)

                hostname = (
                    row.get("VolumeName")
                    or row.get("DriveLetter")
                    or row.get("VolumeSerialNumber")
                    or ""
                )

                for column_name in timestamp_columns:
                    raw_value = row.get(column_name)
                    if raw_value is None:
                        continue
                    value = raw_value.strip()
                    if not value or value.upper() in {"N/A", "NA", "0"}:
                        continue

                    description = _describe_mft_timestamp(column_name)
                    normalized_datetime = _normalize_timestamp(value)
                    if not normalized_datetime:
                        continue
                    consumed_keys = {
                        "EntryNumber",
                        "SequenceNumber",
                        "ParentPath",
                        "Directory",
                        "Path",
                        "FullPath",
                        "FileName",
                        "Name",
                        "Size",
                        "PhysicalSize",
                        "OwnerSID",
                        "Owner",
                        "VolumeName",
                        "DriveLetter",
                        "VolumeSerialNumber",
                        column_name,
                        "ZoneIdentifier",
                        "ZoneId",
                        "ZoneID",
                        "ZoneIdContents",
                        "ZoneIdentifierContents",
                    }
                    extra_attributes = {
                        key: value
                        for key, value in row.items()
                        if key not in consumed_keys and value not in (None, "")
                    }
                    zone_extra_keys = {
                        key: value
                        for key, value in zone_contents.items()
                        if key
                        not in {
                            "zoneid",
                            "zone_id",
                            "hosturl",
                            "zonehosturl",
                            "referrerurl",
                            "zonereferrerurl",
                            "url",
                            "sourceurl",
                            "zonetransferurl",
                        }
                        and value
                    }
                    if zone_extra_keys:
                        extra_attributes.update(
                            {f"zone_{key}": value for key, value in zone_extra_keys.items()}
                        )

                    writer.writerow(
                        {
                            "datetime": normalized_datetime,
                            "timestamp_desc": description,
                            "message": message,
                            "source": "MFTECmd",
                            "source_short": "$MFT",
                            "source_long": "MFTECmd $MFT Parser",
                            "host": hostname,
                            "user": user,
                            "display_name": f"MFTE:{full_path}" if full_path else "MFTECmd",
                            "filename": filename,
                            "filepath": full_path,
                            "entry_number": entry_number,
                            "sequence_number": sequence_number,
                            "size": size,
                            "zone_identifier": zone_identifier,
                            "zone_identifier_raw": zone_contents_raw,
                            "zone_id": zone_id_value,
                            "zone_host_url": host_url,
                            "zone_referrer_url": referrer_url,
                            "zone_source_url": source_url,
                            "alternate_data_stream": ads_name,
                            "extra_attributes": json.dumps(extra_attributes) if extra_attributes else "",
                        }
                    )
                    event_rows += 1

            if event_rows == 0:
                print(
                    "MFTECmd Timesketch conversion skipped: no timestamp rows produced after filtering."
                )
                return None, 0

            return output_handle.getvalue().encode("utf-8"), event_rows
    except FileNotFoundError:
        print(
            f"MFTECmd Timesketch conversion skipped: file not found at path '{csv_path}'."
        )
    except Exception as exc:  # pylint: disable=broad-except
        print(f"MFTECmd Timesketch conversion failed: {exc}")

    return None, 0


def _describe_mft_timestamp(column_name: str) -> str:
    """Generate a human-readable description for a timestamp column."""
    lowered = column_name.lower()
    if "0x10" in lowered:
        attribute = "$STANDARD_INFORMATION"
    elif "0x30" in lowered:
        attribute = "$FILE_NAME"
    else:
        attribute = "MFTECmd"

    if "created" in lowered:
        action = "Created"
    elif "modified" in lowered and "entry" in lowered:
        action = "Entry Modified"
    elif "modified" in lowered:
        action = "Modified"
    elif "accessed" in lowered:
        action = "Accessed"
    else:
        action = column_name

    return f"MFTECmd {attribute} - {action}"


def _normalize_timestamp(raw_value: str) -> str | None:
    """Normalize MFTECmd timestamps to Timesketch's expected format."""
    value = raw_value.strip()
    if not value:
        return None

    parse_formats = [
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    ]

    dt_obj = None

    candidate = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        dt_obj = datetime.fromisoformat(candidate)
    except ValueError:
        for fmt in parse_formats:
            try:
                dt_obj = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue

    if dt_obj is None:
        return None

    if dt_obj.tzinfo is None:
        dt_obj = dt_obj.replace(tzinfo=timezone.utc)
    else:
        dt_obj = dt_obj.astimezone(timezone.utc)

    return dt_obj.strftime("%Y-%m-%dT%H:%M:%S%z")


def _parse_zone_identifier_contents(raw_value: str) -> dict[str, str]:
    """Parse Zone.Identifier ADS contents into key/value pairs."""
    if not raw_value:
        return {}

    normalized = raw_value.strip().strip('"')
    normalized = normalized.replace("\\r", "\n").replace("\\n", "\n")
    normalized = normalized.replace("\r", "\n")

    parsed: dict[str, str] = {}
    for line in normalized.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("["):
            continue
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        parsed[key.strip().lower()] = value.strip()
    return parsed
