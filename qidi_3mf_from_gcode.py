#!/usr/bin/env python3

from __future__ import annotations

import argparse
import ast
import base64
import csv
import hashlib
import json
import math
import re
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from xml.sax.saxutils import escape, quoteattr


DEFAULT_CLIENT_VERSION = "02.05.02.50"
DEFAULT_PRINTER_MODEL = "X-Max 4"
WIPE_TOWER_ID = 1000

# 1x1 transparent PNG fallback. OrcaSlicer can embed PNG thumbnails in G-code;
# the script extracts the largest embedded thumbnail when --thumbnail is omitted.
PLACEHOLDER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)

SETTING_RE = re.compile(r"^;\s*([^=]+?)\s*=\s*(.*)$")
TOTAL_LAYER_RE = re.compile(r"^;\s*total layer number:\s*(\d+)\s*$", re.IGNORECASE | re.MULTILINE)
SET_TOTAL_LAYER_RE = re.compile(r"\bSET_PRINT_STATS_INFO\s+[^\n;]*TOTAL_LAYER\s*=\s*(\d+)", re.IGNORECASE)
M73_LINE_RE = re.compile(r"^M73\b(?P<params>.*)$", re.IGNORECASE | re.MULTILINE)
ESTIMATE_RE = re.compile(r"^;\s*estimated printing time \([^)]*\)\s*=\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
FIRST_LAYER_ESTIMATE_RE = re.compile(
    r"^;\s*estimated first layer printing time \([^)]*\)\s*=\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
EXCLUDE_OBJECT_LINE_RE = re.compile(r"^EXCLUDE_OBJECT_DEFINE\b(?P<params>.*)$", re.IGNORECASE | re.MULTILINE)
PRINTING_OBJECT_RE = re.compile(
    r"^;\s*printing object\s+(?P<name>.+?)\s+id:(?P<object_id>\S+)\s+copy\s+(?P<copy>\S+)",
    re.IGNORECASE | re.MULTILINE,
)
THUMBNAIL_BEGIN_RE = re.compile(r"^;\s*thumbnail\s+begin\s+(?P<width>\d+)x(?P<height>\d+)\s+(?P<size>\d+)\s*$", re.IGNORECASE)
THUMBNAIL_END_RE = re.compile(r"^;\s*thumbnail\s+end\s*$", re.IGNORECASE)
XY_MOVE_RE = re.compile(r"^(?:G0|G1|G2|G3)\b(?P<params>.*)$", re.IGNORECASE)
GCODE_PARAM_RE = re.compile(r"([A-Za-z])\s*(-?\d+(?:\.\d+)?)")
TOOL_SELECT_RE = re.compile(r"^T(?P<tool>\d+)\b", re.IGNORECASE | re.MULTILINE)

SEMICOLON_LIST_SETTINGS = {
    "extruder_colour",
    "filament_colour",
    "filament_colour_type",
    "filament_ids",
    "filament_map",
    "filament_multi_colour",
    "filament_settings_id",
    "filament_type",
    "filament_vendor",
}
SEMICOLON_SCALAR_SETTINGS = {
    "before_layer_change_gcode",
    "change_extrusion_role_gcode",
    "change_filament_gcode",
    "layer_change_gcode",
    "machine_end_gcode",
    "machine_start_gcode",
    "printing_by_object_gcode",
}


@dataclass(frozen=True)
class FilamentInfo:
    index: int
    filament_type: str
    color: str
    used_m: float
    used_g: float
    filament_id: str = ""
    vendor: str = ""
    volume_type: str = "Standard"


@dataclass(frozen=True)
class ObjectInfo:
    identify_id: int
    name: str
    bbox: tuple[float, float, float, float] | None
    area: float | None


@dataclass(frozen=True)
class GcodeMetadata:
    total_layers: int
    prediction_seconds: int
    printer_model: str
    nozzle_diameter: float
    timelapse_type: str
    first_layer_time: float
    filaments: tuple[FilamentInfo, ...]
    objects: tuple[ObjectInfo, ...]
    filament_sequence: tuple[int, ...]
    settings: dict[str, str | list[str]]


def positive_int_arg(raw: str) -> str:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return str(value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bundle an OrcaSlicer/QIDI-flavored G-code file into a QIDI-style print-ready .3mf package.",
    )
    parser.add_argument("gcode", type=Path, help="Input .gcode file produced by OrcaSlicer.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output .3mf path. Defaults to <input>.3mf, e.g. print.gcode.3mf.",
    )
    parser.add_argument(
        "--printer-model",
        default=None,
        help=f"Printer model ID for Metadata/slice_info.config. Defaults to the G-code setting or {DEFAULT_PRINTER_MODEL!r}.",
    )
    parser.add_argument(
        "--client-version",
        default=DEFAULT_CLIENT_VERSION,
        help="X-QDT-Client-Version value for Metadata/slice_info.config.",
    )
    parser.add_argument(
        "--plate-index",
        type=positive_int_arg,
        default="1",
        help="Positive integer plate index used in QIDI metadata paths. Defaults to 1.",
    )
    parser.add_argument(
        "--thumbnail",
        type=Path,
        help="Optional PNG used for the QIDI thumbnail entries. A 1x1 transparent PNG is used when omitted.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    gcode_path = args.gcode
    if not gcode_path.is_file():
        print(f"Input G-code not found: {gcode_path}", file=sys.stderr)
        return 2

    output_path = args.output or gcode_path.with_name(gcode_path.name + ".3mf")
    if output_path.exists() and not args.force:
        print(f"Output already exists: {output_path} (use --force to overwrite)", file=sys.stderr)
        return 2

    gcode_bytes = gcode_path.read_bytes()
    gcode_text = gcode_bytes.decode("utf-8", errors="replace")
    metadata = parse_gcode_metadata(gcode_text, printer_model_override=args.printer_model)
    try:
        thumbnail = read_thumbnail(args.thumbnail) if args.thumbnail else extract_thumbnail_png(gcode_text) or PLACEHOLDER_PNG
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    output_path.parent.mkdir(parents=True, exist_ok=True)
    package_gcode_bytes = qidi_package_gcode_bytes(gcode_bytes, metadata)
    write_qidi_3mf(
        output_path=output_path,
        gcode_bytes=package_gcode_bytes,
        source_name=gcode_path.name,
        metadata=metadata,
        client_version=args.client_version,
        plate_index=args.plate_index,
        thumbnail_png=thumbnail,
    )

    print(f"Wrote {output_path}")
    print(f"prediction={metadata.prediction_seconds}s total_layers={metadata.total_layers} filaments={len(metadata.filaments)} objects={len(metadata.objects)}")
    return 0


def parse_gcode_metadata(gcode: str, printer_model_override: str | None = None) -> GcodeMetadata:
    settings = parse_config_settings(gcode)
    total_layers = parse_total_layers(gcode)
    prediction_seconds = parse_prediction_seconds(gcode)
    printer_model = printer_model_override or normalize_printer_model(first_setting(settings, "printer_model") or DEFAULT_PRINTER_MODEL)
    nozzle_diameter = parse_float(first_setting(settings, "nozzle_diameter"), 0.4)
    timelapse_type = first_setting(settings, "timelapse_type") or "0"
    first_layer_time = parse_first_layer_time(gcode, settings)
    explicit_filament_sequence = parse_filament_sequence(gcode)
    filaments = parse_filaments(settings, used_filament_indices=explicit_filament_sequence)
    filament_sequence = normalize_filament_sequence(explicit_filament_sequence, filaments)
    objects = parse_objects(gcode, settings)
    return GcodeMetadata(
        total_layers=total_layers,
        prediction_seconds=prediction_seconds,
        printer_model=printer_model,
        nozzle_diameter=nozzle_diameter,
        timelapse_type=timelapse_type,
        first_layer_time=first_layer_time,
        filaments=filaments,
        objects=objects,
        filament_sequence=filament_sequence,
        settings=settings,
    )


def parse_config_settings(gcode: str) -> dict[str, str | list[str]]:
    settings: dict[str, str | list[str]] = {}
    in_thumbnail = False
    for line in gcode.splitlines():
        if THUMBNAIL_BEGIN_RE.match(line):
            in_thumbnail = True
            continue
        if in_thumbnail:
            if THUMBNAIL_END_RE.match(line):
                in_thumbnail = False
            continue
        match = SETTING_RE.match(line)
        if not match:
            continue
        key = match.group(1).strip()
        value = match.group(2).strip()
        if not key:
            continue
        settings[key] = parse_setting_value(key, value)
    return settings


def parse_setting_value(key: str, value: str) -> str | list[str]:
    if key.strip().lower() in SEMICOLON_SCALAR_SETTINGS:
        return unquote_setting(value)
    if ";" in value and is_semicolon_list_setting(key, value):
        return parse_delimited_setting(value, ";")
    if "," not in value:
        return unquote_setting(value)
    return parse_delimited_setting(value, ",")


def parse_delimited_setting(value: str, delimiter: str) -> str | list[str]:
    try:
        reader = csv.reader([value], delimiter=delimiter, skipinitialspace=True)
        parts = next(reader)
    except csv.Error:
        return unquote_setting(value)
    return [unquote_setting(part.strip()) for part in parts]


def is_semicolon_list_setting(key: str, value: str) -> bool:
    normalized_key = key.strip().lower()
    if normalized_key in SEMICOLON_SCALAR_SETTINGS:
        return False
    if normalized_key in SEMICOLON_LIST_SETTINGS:
        return True
    if value.startswith('"') and '";"' in value:
        return True
    return normalized_key.startswith("filament_")


def unquote_setting(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def first_setting(settings: dict[str, str | list[str]], key: str, default: str | None = None) -> str | None:
    value = settings.get(key)
    if isinstance(value, list):
        return value[0] if value else default
    return value if value not in (None, "") else default


def split_setting_values(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    raw_values = value if isinstance(value, list) else [value]
    values: list[str] = []
    for raw in raw_values:
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        if ";" in text:
            try:
                parts = next(csv.reader([text], delimiter=";", skipinitialspace=True))
            except csv.Error:
                parts = text.split(";")
            values.extend(unquote_setting(part.strip()) for part in parts if part.strip())
        else:
            values.append(unquote_setting(text))
    return values


def split_setting_list(settings: dict[str, str | list[str]], key: str) -> list[str]:
    return split_setting_values(settings.get(key))


def parse_total_layers(gcode: str) -> int:
    match = TOTAL_LAYER_RE.search(gcode)
    if match:
        return int(match.group(1))
    matches = SET_TOTAL_LAYER_RE.findall(gcode)
    if matches:
        return max(int(value) for value in matches)
    current_layers = re.findall(r"\bSET_PRINT_STATS_INFO\s+[^\n;]*CURRENT_LAYER\s*=\s*(\d+)", gcode, re.IGNORECASE)
    if current_layers:
        return max(int(value) for value in current_layers)
    return 0


def parse_prediction_seconds(gcode: str) -> int:
    match = ESTIMATE_RE.search(gcode)
    if match:
        parsed = parse_duration_seconds(match.group(1))
        if parsed > 0:
            return parsed
    m73_values: list[tuple[float, float]] = []
    for match in M73_LINE_RE.finditer(gcode):
        params = parse_gcode_param_numbers(match.group("params"))
        progress = params.get("P")
        remaining = params.get("R")
        if progress is not None and remaining is not None:
            m73_values.append((progress, remaining))
    if m73_values:
        # M73 R is minutes remaining. Prefer the earliest low-progress value.
        low_progress = [remaining for progress, remaining in m73_values if progress <= 1]
        remaining_minutes = max(low_progress or [remaining for _, remaining in m73_values])
        return int(round(remaining_minutes * 60))
    return 0


def parse_first_layer_time(gcode: str, settings: dict[str, str | list[str]]) -> float:
    configured = parse_float(first_setting(settings, "first_layer_time"), 0.0)
    if configured > 0:
        return configured
    match = FIRST_LAYER_ESTIMATE_RE.search(gcode)
    if match:
        parsed = parse_duration_seconds(match.group(1))
        if parsed > 0:
            return float(parsed)
    return configured


def parse_gcode_param_numbers(raw_params: str) -> dict[str, float]:
    command_params = raw_params.split(";", 1)[0]
    return {key.upper(): float(value) for key, value in GCODE_PARAM_RE.findall(command_params)}


def parse_duration_seconds(raw: str) -> int:
    raw = raw.strip().lower()
    total = 0
    matched = False
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", raw):
        matched = True
        number = float(value)
        if unit == "d":
            total += int(number * 86400)
        elif unit == "h":
            total += int(number * 3600)
        elif unit == "m":
            total += int(number * 60)
        elif unit == "s":
            total += int(number)
    if matched:
        return total
    colon = raw.split(":")
    if all(part.isdigit() for part in colon):
        nums = [int(part) for part in colon]
        if len(nums) == 3:
            return nums[0] * 3600 + nums[1] * 60 + nums[2]
        if len(nums) == 2:
            return nums[0] * 60 + nums[1]
    return 0


def normalize_printer_model(raw: str) -> str:
    model = raw.strip().strip('"')
    for prefix in ("Qidi ", "QIDI "):
        if model.startswith(prefix):
            model = model[len(prefix) :]
    return model or DEFAULT_PRINTER_MODEL


def parse_filaments(
    settings: dict[str, str | list[str]],
    used_filament_indices: Iterable[int] | None = None,
) -> tuple[FilamentInfo, ...]:
    colors = split_setting_list(settings, "filament_colour") or split_setting_list(settings, "extruder_colour") or split_setting_list(settings, "default_filament_colour") or ["#FFFFFF"]
    types = split_setting_list(settings, "filament_type") or ["PLA"]
    filament_ids = split_setting_list(settings, "filament_ids")
    vendors = split_setting_list(settings, "filament_vendor")
    volume_types = split_setting_list(settings, "nozzle_volume_type") or split_setting_list(settings, "default_nozzle_volume_type") or ["Standard"]
    used_m = [parse_float(value, 0.0) / 1000.0 for value in split_setting_list(settings, "filament used [mm]")]
    used_g = [parse_float(value, 0.0) for value in split_setting_list(settings, "filament used [g]")]
    total_used_g = parse_float(first_setting(settings, "total filament used [g]"), 0.0)
    requested_indices = {index for index in (used_filament_indices or ()) if index > 0}
    count = max(
        len(colors),
        len(types),
        len(filament_ids),
        len(vendors),
        len(volume_types),
        len(used_m),
        len(used_g),
        max(requested_indices, default=0),
        1,
    )
    included_indices = used_filament_indices_for_metadata(requested_indices, used_m, used_g, count)
    filaments = []
    for index in range(count):
        if index + 1 not in included_indices:
            continue
        color = get_repeated(colors, index, "#FFFFFF") or "#FFFFFF"
        if not color.startswith("#") or len(color) not in (4, 7, 9):
            color = "#FFFFFF"
        filament_type = get_repeated(types, index, "PLA") or "PLA"
        filament_used_m = get_float_repeated(used_m, index, 0.0)
        if used_g:
            filament_used_g = get_float_repeated(used_g, index, 0.0)
        elif count == 1:
            filament_used_g = total_used_g
        else:
            filament_used_g = 0.0
        filaments.append(
            FilamentInfo(
                index=index + 1,
                filament_type=filament_type,
                color=color,
                used_m=filament_used_m,
                used_g=filament_used_g,
                filament_id=get_repeated(filament_ids, index, ""),
                vendor=get_repeated(vendors, index, ""),
                volume_type=get_repeated(volume_types, index, "Standard") or "Standard",
            )
        )
    return tuple(filaments)


def used_filament_indices_for_metadata(
    requested_indices: set[int],
    used_m: list[float],
    used_g: list[float],
    count: int,
) -> set[int]:
    usage_indices = positive_usage_indices(used_m, used_g, count)
    explicit_indices = {index for index in requested_indices if 1 <= index <= count}
    if explicit_indices:
        return explicit_indices | usage_indices
    return usage_indices or set(range(1, count + 1))


def positive_usage_indices(used_m: list[float], used_g: list[float], count: int) -> set[int]:
    has_per_filament_usage = len(used_m) >= count or len(used_g) >= count
    if not has_per_filament_usage:
        return set()
    return {
        index + 1
        for index in range(count)
        if (used_m[index] if index < len(used_m) else 0.0) > 0
        or (used_g[index] if index < len(used_g) else 0.0) > 0
    }


def get_repeated(values: list[str], index: int, default: str) -> str:
    if not values:
        return default
    if index < len(values):
        return values[index]
    return values[-1]


def get_float_repeated(values: list[float], index: int, default: float) -> float:
    if not values:
        return default
    if index < len(values):
        return values[index]
    return values[-1]


def parse_float(raw: str | None, default: float) -> float:
    fallback = default if math.isfinite(default) else 0.0
    value = parse_optional_float(raw)
    return value if value is not None else fallback


def parse_optional_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        value = float(str(raw).strip().strip('"'))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_gcode_key_values(raw_params: str) -> dict[str, str]:
    params: dict[str, str] = {}
    position = 0
    length = len(raw_params)
    while position < length:
        while position < length and raw_params[position].isspace():
            position += 1
        key_start = position
        while position < length and raw_params[position] not in "= \t\r\n":
            position += 1
        key = raw_params[key_start:position].strip().upper()
        while position < length and raw_params[position].isspace():
            position += 1
        if position >= length or raw_params[position] != "=":
            while position < length and not raw_params[position].isspace():
                position += 1
            continue
        position += 1
        while position < length and raw_params[position].isspace():
            position += 1
        value, position = read_gcode_parameter_value(raw_params, position)
        if key:
            params[key] = value
    return params


def read_gcode_parameter_value(raw_params: str, position: int) -> tuple[str, int]:
    if position >= len(raw_params):
        return "", position
    if raw_params[position] in {'"', "'"}:
        return read_quoted_gcode_parameter(raw_params, position)
    if raw_params[position] == "[":
        return read_bracketed_gcode_parameter(raw_params, position)
    start = position
    while position < len(raw_params) and not raw_params[position].isspace():
        position += 1
    return raw_params[start:position], position


def read_quoted_gcode_parameter(raw_params: str, position: int) -> tuple[str, int]:
    quote = raw_params[position]
    position += 1
    chars: list[str] = []
    while position < len(raw_params):
        char = raw_params[position]
        if char == "\\" and position + 1 < len(raw_params):
            chars.append(raw_params[position + 1])
            position += 2
            continue
        if char == quote:
            return "".join(chars), position + 1
        chars.append(char)
        position += 1
    return "".join(chars), position


def read_bracketed_gcode_parameter(raw_params: str, position: int) -> tuple[str, int]:
    start = position
    depth = 0
    quote: str | None = None
    while position < len(raw_params):
        char = raw_params[position]
        if quote is not None:
            if char == "\\" and position + 1 < len(raw_params):
                position += 2
                continue
            if char == quote:
                quote = None
            position += 1
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                position += 1
                break
        position += 1
    return raw_params[start:position].strip(), position


def parse_objects(gcode: str, settings: dict[str, str | list[str]]) -> tuple[ObjectInfo, ...]:
    objects: list[ObjectInfo] = []
    seen: set[str] = set()
    for match in EXCLUDE_OBJECT_LINE_RE.finditer(gcode):
        params = parse_gcode_key_values(match.group("params"))
        name = params.get("NAME")
        if not name or name in seen:
            continue
        seen.add(name)
        bbox = parse_polygon_bbox(params.get("POLYGON"))
        area = bbox_area(bbox) if bbox else None
        objects.append(ObjectInfo(identify_id=len(objects) + 1, name=name, bbox=bbox, area=area))
    if not objects:
        objects.extend(parse_objects_from_comments(gcode))
    if is_enabled(first_setting(settings, "enable_prime_tower")):
        wipe = parse_wipe_tower_object(settings)
        if wipe is not None and wipe.name not in {obj.name for obj in objects}:
            objects.append(wipe)
    return tuple(objects)


def parse_objects_from_comments(gcode: str) -> list[ObjectInfo]:
    bboxes: dict[str, list[float]] = {}
    current_name: str | None = None
    current_x: float | None = None
    current_y: float | None = None
    for raw_line in gcode.splitlines():
        object_match = PRINTING_OBJECT_RE.match(raw_line)
        if object_match:
            current_name = normalize_object_name(
                object_match.group("name"), object_match.group("object_id"), object_match.group("copy")
            )
            bboxes.setdefault(current_name, [float("inf"), float("inf"), float("-inf"), float("-inf")])
            continue
        if raw_line.lower().startswith("; stop printing object"):
            current_name = None
            continue
        params = parse_xy_params(raw_line)
        if params is None:
            continue
        x, y = params
        if x is not None:
            current_x = x
        if y is not None:
            current_y = y
        if current_name is None or current_x is None or current_y is None:
            continue
        bbox = bboxes[current_name]
        bbox[0] = min(bbox[0], current_x)
        bbox[1] = min(bbox[1], current_y)
        bbox[2] = max(bbox[2], current_x)
        bbox[3] = max(bbox[3], current_y)
    objects: list[ObjectInfo] = []
    for name, bbox_values in bboxes.items():
        bbox = tuple(bbox_values) if bbox_values[0] != float("inf") else None
        objects.append(ObjectInfo(identify_id=len(objects) + 1, name=name, bbox=bbox, area=bbox_area(bbox)))
    return objects


def normalize_object_name(name: str, object_id: str, copy: str) -> str:
    safe_name = re.sub(r"\W+", "_", name.strip()).strip("_") or "object"
    return f"{safe_name}_id_{object_id}_copy_{copy}"


def parse_xy_params(raw_line: str) -> tuple[float | None, float | None] | None:
    command = raw_line.split(";", 1)[0].strip()
    match = XY_MOVE_RE.match(command)
    if not match:
        return None
    params = parse_gcode_param_numbers(match.group("params"))
    if "X" not in params and "Y" not in params:
        return None
    return (params.get("X"), params.get("Y"))


def parse_wipe_tower_object(settings: dict[str, str | list[str]]) -> ObjectInfo | None:
    x = parse_optional_float(first_setting(settings, "wipe_tower_x"))
    y = parse_optional_float(first_setting(settings, "wipe_tower_y"))
    width = parse_float(first_setting(settings, "prime_tower_width"), 0.0)
    if x is None or y is None or width <= 0:
        return None
    bbox = (x, y, x + width, y + width)
    return ObjectInfo(identify_id=WIPE_TOWER_ID, name="wipe_tower", bbox=bbox, area=bbox_area(bbox))


def is_enabled(value: str | None) -> bool:
    return str(value or "0").strip().lower() in {"1", "true", "yes", "on"}


def parse_filament_sequence(gcode: str) -> tuple[int, ...]:
    sequence: list[int] = []
    for match in TOOL_SELECT_RE.finditer(gcode):
        filament = int(match.group("tool")) + 1
        if sequence and sequence[-1] == filament:
            continue
        sequence.append(filament)
    return tuple(sequence)


def normalize_filament_sequence(
    explicit_sequence: Iterable[int],
    filaments: Iterable[FilamentInfo],
) -> tuple[int, ...]:
    sequence = list(explicit_sequence)
    implicit_t0_used = any(filament.index == 1 and filament_has_usage(filament) for filament in filaments)
    if implicit_t0_used and sequence and sequence[0] != 1:
        sequence.insert(0, 1)
    return tuple(sequence)


def filament_has_usage(filament: FilamentInfo) -> bool:
    return filament.used_m > 0 or filament.used_g > 0


def parse_polygon_bbox(raw_polygon: str | None) -> tuple[float, float, float, float] | None:
    if not raw_polygon:
        return None
    try:
        polygon = ast.literal_eval(raw_polygon)
    except (SyntaxError, ValueError):
        return None
    points: list[tuple[float, float]] = []
    for point in polygon:
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            try:
                x = float(point[0])
                y = float(point[1])
            except (TypeError, ValueError):
                continue
            if math.isfinite(x) and math.isfinite(y):
                points.append((x, y))
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (min(xs), min(ys), max(xs), max(ys))


def bbox_area(bbox: tuple[float, float, float, float] | None) -> float | None:
    if bbox is None:
        return None
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def extract_thumbnail_png(gcode: str) -> bytes | None:
    thumbnails: list[tuple[int, int, bytes]] = []
    active: tuple[int, int, list[str]] | None = None
    for line in gcode.splitlines():
        begin = THUMBNAIL_BEGIN_RE.match(line)
        if begin:
            active = (int(begin.group("width")), int(begin.group("height")), [])
            continue
        if active is None:
            continue
        if THUMBNAIL_END_RE.match(line):
            width, height, lines = active
            active = None
            try:
                data = base64.b64decode("".join(lines), validate=True)
            except ValueError:
                continue
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                thumbnails.append((width, height, data))
            continue
        stripped = line.strip()
        if stripped.startswith(";"):
            stripped = stripped[1:].strip()
        if stripped:
            active[2].append(stripped)
    if not thumbnails:
        return None
    return max(thumbnails, key=lambda item: item[0] * item[1])[2]


def read_thumbnail(path: Path) -> bytes:
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(f"Thumbnail is not a PNG: {path}")
    return data


def qidi_package_gcode_bytes(gcode_bytes: bytes, metadata: GcodeMetadata) -> bytes:
    if not metadata.objects or b"EXCLUDE_OBJECT_DEFINE" in gcode_bytes:
        return gcode_bytes
    definitions = exclude_object_define_lines(metadata.objects)
    if not definitions:
        return gcode_bytes
    gcode = gcode_bytes.decode("utf-8", errors="replace")
    insertion = "\n".join(definitions) + "\n"
    marker = "; EXECUTABLE_BLOCK_START"
    marker_index = gcode.find(marker)
    if marker_index >= 0:
        line_end = gcode.find("\n", marker_index)
        if line_end >= 0:
            gcode = gcode[: line_end + 1] + insertion + gcode[line_end + 1 :]
        else:
            gcode = gcode + "\n" + insertion
    else:
        gcode = insertion + gcode
    return gcode.encode("utf-8")


def exclude_object_define_lines(objects: Iterable[ObjectInfo]) -> list[str]:
    lines = []
    seen: set[str] = set()
    for obj in objects:
        if obj.bbox is None:
            continue
        name = exclude_object_define_name(obj)
        if name in seen:
            continue
        seen.add(name)
        min_x, min_y, max_x, max_y = obj.bbox
        if not all(math.isfinite(value) for value in obj.bbox):
            continue
        center_x = (min_x + max_x) / 2.0
        center_y = (min_y + max_y) / 2.0
        polygon = (
            f"[[{format_float(min_x)},{format_float(min_y)}],"
            f"[{format_float(max_x)},{format_float(min_y)}],"
            f"[{format_float(max_x)},{format_float(max_y)}],"
            f"[{format_float(min_x)},{format_float(max_y)}],"
            f"[{format_float(min_x)},{format_float(min_y)}]]"
        )
        lines.append(
            "EXCLUDE_OBJECT_DEFINE "
            f"NAME={name} "
            f"CENTER={format_float(center_x)},{format_float(center_y)} "
            f"POLYGON={polygon}"
        )
    return lines


def exclude_object_define_name(obj: ObjectInfo) -> str:
    if obj.name == "wipe_tower":
        return "wipe_tower_area"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", obj.name).strip("_") or f"object_{obj.identify_id}"


def write_qidi_3mf(
    *,
    output_path: Path,
    gcode_bytes: bytes,
    source_name: str,
    metadata: GcodeMetadata,
    client_version: str,
    plate_index: str,
    thumbnail_png: bytes,
) -> None:
    gcode_member = f"Metadata/plate_{plate_index}.gcode"
    md5_hex = hashlib.md5(gcode_bytes).hexdigest().upper()
    timestamp = datetime.now().timetuple()[:6]
    entries: list[tuple[str, bytes]] = [
        ("[Content_Types].xml", content_types_xml().encode("utf-8")),
        ("_rels/.rels", root_rels_xml(plate_index).encode("utf-8")),
        ("3D/3dmodel.model", model_xml(client_version, plate_index, source_name).encode("utf-8")),
        (f"Metadata/plate_{plate_index}.png", thumbnail_png),
        (f"Metadata/plate_no_light_{plate_index}.png", thumbnail_png),
        (f"Metadata/top_{plate_index}.png", thumbnail_png),
        (f"Metadata/pick_{plate_index}.png", thumbnail_png),
        (f"Metadata/plate_{plate_index}.json", plate_json(metadata).encode("utf-8")),
        ("Metadata/project_settings.config", project_settings_json(metadata).encode("utf-8")),
        (f"Metadata/plate_{plate_index}.gcode.md5", md5_hex.encode("ascii")),
        (gcode_member, gcode_bytes),
        ("Metadata/_rels/model_settings.config.rels", model_settings_rels_xml(gcode_member).encode("utf-8")),
        ("Metadata/model_settings.config", model_settings_xml(plate_index, metadata).encode("utf-8")),
        ("Metadata/cut_information.xml", cut_information_xml(metadata).encode("utf-8")),
        ("Metadata/slice_info.config", slice_info_xml(metadata, client_version, plate_index).encode("utf-8")),
        ("Metadata/filament_sequence.json", filament_sequence_json(metadata, plate_index).encode("utf-8")),
    ]
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=timestamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)


def content_types_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="png" ContentType="image/png"/>
 <Default Extension="gcode" ContentType="text/x.gcode"/>
</Types>
"""


def root_rels_xml(plate_index: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
 <Relationship Target="/Metadata/plate_{escape(plate_index)}.png" Id="rel-2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"/>
 <Relationship Target="/Metadata/plate_{escape(plate_index)}.png" Id="rel-4" Type="http://schemas.qiditech.com/package/2021/cover-thumbnail-middle"/>
 <Relationship Target="/Metadata/plate_{escape(plate_index)}.png" Id="rel-5" Type="http://schemas.qiditech.com/package/2021/cover-thumbnail-small"/>
</Relationships>
"""


def model_xml(client_version: str, plate_index: str, source_name: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:QIDIStudio="http://schemas.qiditech.com/package/2021" xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">
 <metadata name="Application">QIDIStudio-{escape(client_version)}</metadata>
 <metadata name="BambuStudio:3mfVersion">1</metadata>
 <metadata name="CreationDate">{escape(now)}</metadata>
 <metadata name="ModificationDate">{escape(now)}</metadata>
 <metadata name="Origin">gcode</metadata>
 <metadata name="ProfileTitle">{escape(source_name)}</metadata>
 <metadata name="QIDIStudio:3mfVersion">1</metadata>
 <metadata name="Thumbnail_Middle">/Metadata/plate_{escape(plate_index)}.png</metadata>
 <metadata name="Thumbnail_Small">/Metadata/plate_{escape(plate_index)}.png</metadata>
 <metadata name="Title">{escape(source_name)}</metadata>
 <resources>
 </resources>
 <build/>
</model>
"""


def model_settings_xml(plate_index: str, metadata: GcodeMetadata) -> str:
    p = escape(plate_index)
    filament_maps = qidi_filament_maps(metadata)
    filament_volume_maps = " ".join("0" for _ in metadata.filaments) or "0"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="plater_id" value={quoteattr(p)}/>
    <metadata key="plater_name" value=""/>
    <metadata key="locked" value="false"/>
    <metadata key="filament_map_mode" value="Auto For Flush"/>
    <metadata key="filament_maps" value={quoteattr(filament_maps)}/>
    <metadata key="filament_volume_maps" value={quoteattr(filament_volume_maps)}/>
    <metadata key="gcode_file" value="Metadata/plate_{p}.gcode"/>
    <metadata key="thumbnail_file" value="Metadata/plate_{p}.png"/>
    <metadata key="thumbnail_no_light_file" value="Metadata/plate_no_light_{p}.png"/>
    <metadata key="top_file" value="Metadata/top_{p}.png"/>
    <metadata key="pick_file" value="Metadata/pick_{p}.png"/>
    <metadata key="pattern_bbox_file" value="Metadata/plate_{p}.json"/>
  </plate>
</config>
"""


def model_settings_rels_xml(gcode_member: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/{escape(gcode_member)}" Id="rel-1" Type="http://schemas.qiditech.com/package/2021/gcode"/>
</Relationships>
"""


def cut_information_xml(metadata: GcodeMetadata) -> str:
    object_lines = "\n".join(
        f" <object id={quoteattr(str(obj.identify_id))}>\n  <cut_id id=\"0\" check_sum=\"1\" connectors_cnt=\"0\"/>\n </object>"
        for obj in metadata.objects
        if obj.name != "wipe_tower"
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<objects>
{object_lines}
</objects>"""


def filament_sequence_json(metadata: GcodeMetadata, plate_index: str) -> str:
    sequence = list(metadata.filament_sequence)
    if not sequence:
        sequence = [filament.index for filament in metadata.filaments]
    payload = {f"plate_{plate_index}": {"sequence": sequence}}
    return json.dumps(payload, separators=(",", ":"))


def slice_info_xml(metadata: GcodeMetadata, client_version: str, plate_index: str) -> str:
    total_weight = sum(filament.used_g for filament in metadata.filaments)
    object_lines = "\n".join(
        f"    <object identify_id={quoteattr(str(obj.identify_id))} name={quoteattr(obj.name)} skipped=\"false\" />"
        for obj in metadata.objects
        if obj.name != "wipe_tower"
    )
    filament_lines = "\n".join(
        (
            "    <filament id={id} tray_info_idx={tray} type={type} color={color} "
            "used_m={used_m} used_g={used_g} used_for_object=\"true\" used_for_support=\"false\" "
            "group_id=\"0\" nozzle_diameter={nozzle} volume_type={volume}/>")
        .format(
            id=quoteattr(str(filament.index)),
            tray=quoteattr(filament.filament_id),
            type=quoteattr(filament.filament_type),
            color=quoteattr(filament.color),
            used_m=quoteattr(format_float(filament.used_m)),
            used_g=quoteattr(format_float(filament.used_g)),
            nozzle=quoteattr(format_float(metadata.nozzle_diameter)),
            volume=quoteattr(filament.volume_type),
        )
        for filament in metadata.filaments
    )
    filament_maps = qidi_filament_maps(metadata)
    limit_filament_maps = " ".join("0" for _ in metadata.filaments) or "0"
    last_layer = max(metadata.total_layers - 1, 0)
    layer_filaments = " ".join(str(filament.index - 1) for filament in metadata.filaments) or "0"
    label_object_enabled = "true" if metadata.objects else "false"
    layer_list = f"      <layer_filament_list filament_list={quoteattr(layer_filaments)} layer_ranges=\"0 {last_layer}\" />"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<config>
  <header>
    <header_item key="X-QDT-Client-Type" value="slicer"/>
    <header_item key="X-QDT-Client-Version" value={quoteattr(client_version)}/>
  </header>
  <plate>
    <metadata key="index" value={quoteattr(str(plate_index))}/>
    <metadata key="extruder_type" value="0"/>
    <metadata key="nozzle_volume_type" value="0"/>
    <metadata key="printer_model_id" value={quoteattr(metadata.printer_model)}/>
    <metadata key="nozzle_diameters" value={quoteattr(format_float(metadata.nozzle_diameter))}/>
    <metadata key="timelapse_type" value={quoteattr(metadata.timelapse_type)}/>
    <metadata key="prediction" value={quoteattr(str(metadata.prediction_seconds))}/>
    <metadata key="weight" value={quoteattr(format_float(total_weight))}/>
    <metadata key="first_layer_time" value={quoteattr(format_float(metadata.first_layer_time))}/>
    <metadata key="outside" value="false"/>
    <metadata key="support_used" value="false"/>
    <metadata key="label_object_enabled" value={quoteattr(label_object_enabled)}/>
    <metadata key="filament_maps" value={quoteattr(filament_maps)}/>
    <metadata key="limit_filament_maps" value={quoteattr(limit_filament_maps)}/>
{object_lines}
{filament_lines}
    <layer_filament_lists>
{layer_list}
    </layer_filament_lists>
  </plate>
</config>
"""


def plate_json(metadata: GcodeMetadata) -> str:
    bboxes = [obj.bbox for obj in metadata.objects if obj.bbox is not None]
    if bboxes:
        bbox_all = [min(b[0] for b in bboxes), min(b[1] for b in bboxes), max(b[2] for b in bboxes), max(b[3] for b in bboxes)]
    else:
        bbox_all = [0, 0, 0, 0]
    objects = []
    for obj in metadata.objects:
        objects.append(
            {
                "area": obj.area or 0,
                "bbox": list(obj.bbox or (0, 0, 0, 0)),
                "id": obj.identify_id,
                "layer_height": parse_float(first_setting(metadata.settings, "layer_height"), 0.2),
                "name": obj.name,
            }
        )
    payload = {
        "bbox_all": bbox_all,
        "bbox_objects": objects,
        "bed_type": normalize_bed_type(first_setting(metadata.settings, "curr_bed_type") or "textured_plate"),
        "filament_colors": [filament.color for filament in metadata.filaments],
        "filament_ids": [filament.index - 1 for filament in metadata.filaments],
        "first_extruder": first_extruder(metadata),
        "first_layer_time": metadata.first_layer_time,
        "is_seq_print": False,
        "nozzle_diameter": metadata.nozzle_diameter,
        "version": 2,
    }
    return json.dumps(payload, separators=(",", ":"))


def project_settings_json(metadata: GcodeMetadata) -> str:
    return json.dumps(metadata.settings, indent=4, sort_keys=True) + "\n"


def qidi_filament_maps(metadata: GcodeMetadata) -> str:
    maps = split_setting_values(metadata.settings.get("filament_map"))
    if not maps:
        return " ".join("1" for _ in metadata.filaments) or "1"
    selected_maps = []
    for filament in metadata.filaments:
        map_index = filament.index - 1
        selected_maps.append(maps[map_index] if map_index < len(maps) else maps[-1])
    return " ".join(selected_maps) or "1"


def first_extruder(metadata: GcodeMetadata) -> int:
    if metadata.filament_sequence:
        return metadata.filament_sequence[0] - 1
    if metadata.filaments:
        return metadata.filaments[0].index - 1
    return 0


def normalize_bed_type(raw: str) -> str:
    value = raw.strip().lower().replace(" ", "_")
    if "textured" in value:
        return "textured_plate"
    if "smooth" in value:
        return "smooth_plate"
    return value or "textured_plate"


def format_float(value: float) -> str:
    if not math.isfinite(value):
        return "0"
    return f"{value:.6f}".rstrip("0").rstrip(".")


if __name__ == "__main__":
    raise SystemExit(main())
