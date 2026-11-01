"""Wheatley Config Editor - Safe Round-Trip YAML Check Configuration Modification"""

from __future__ import annotations

import difflib
import io
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString
import yaml as pyyaml

from wheatley.config import CheckConfig, ScheduleWindow, _parse_hhmm

logger = logging.getLogger("wheatley.tools.config_editor")


def get_yaml_parser(text: Optional[str] = None) -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    if text and ("\n  - " in text or "\n  -" in text):
        y.indent(mapping=2, sequence=4, offset=2)
    else:
        y.indent(mapping=2, sequence=2, offset=0)
    return y


SCHEDULE_ACTIONS = (
    "add_schedule",
    "add_pause_window",
    "set_schedule",
    "remove_schedule",
    "remove_pause_window",
    "clear_schedule",
)


def _extract_start_end(
    start: Optional[str] = None,
    end: Optional[str] = None,
    value: Any = None,
    values: Optional[List[str]] = None,
) -> Tuple[Optional[str], Optional[str], Optional[Any], Optional[str]]:
    """Extract (start, end, days, reason) from explicit args or fallback value/values."""
    s_out, e_out, d_out, r_out = start, end, None, None
    if isinstance(value, dict):
        s_out = s_out or value.get("start")
        e_out = e_out or value.get("end")
        d_out = value.get("days")
        r_out = value.get("reason")
    elif isinstance(value, str):
        val_str = value.strip()
        if val_str.startswith("{") and val_str.endswith("}"):
            try:
                parsed = json.loads(val_str)
                if isinstance(parsed, dict):
                    s_out = s_out or parsed.get("start")
                    e_out = e_out or parsed.get("end")
                    d_out = parsed.get("days")
                    r_out = parsed.get("reason")
            except Exception:
                pass
        elif "-" in val_str and not (s_out and e_out):
            parts = [p.strip() for p in val_str.split("-", 1)]
            if len(parts) == 2:
                s_out = s_out or parts[0]
                e_out = e_out or parts[1]
    if not (s_out and e_out) and values and len(values) >= 2:
        try:
            _parse_hhmm(values[0])
            _parse_hhmm(values[1])
            s_out = s_out or values[0]
            e_out = e_out or values[1]
        except ValueError:
            pass
    return s_out, e_out, d_out, r_out


def _apply_schedule_action(
    target_map: Any,
    action: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    days: Optional[Any] = None,
    reason: Optional[str] = None,
    value: Any = None,
    values: Optional[List[str]] = None,
    insert_at_top: bool = False,
) -> None:
    """Apply a schedule modification to either the root checks.yaml mapping or a single check mapping."""
    sched_key = "schedule"
    for alias in ("schedule", "pause_windows", "maintenance_windows", "quiet_hours"):
        if alias in target_map:
            sched_key = alias
            break

    s_val, e_val, d_fallback, r_fallback = _extract_start_end(start=start, end=end, value=value, values=values)
    eff_days = days if days is not None else d_fallback
    eff_reason = reason if reason is not None else r_fallback

    if action in ("add_schedule", "add_pause_window", "set_schedule"):
        if not s_val or not e_val:
            raise ValueError("Both 'start' and 'end' times (HH:MM) are required to configure a schedule window")
        win = ScheduleWindow(start=s_val, end=e_val, days=eff_days, reason=eff_reason)
        win_dict: Dict[str, Any] = {
            "start": DoubleQuotedScalarString(win.start),
            "end": DoubleQuotedScalarString(win.end),
        }
        if win.days:
            win_dict["days"] = list(win.days)
        if win.reason:
            win_dict["reason"] = win.reason

        if action == "set_schedule":
            if sched_key not in target_map and insert_at_top and hasattr(target_map, "insert"):
                target_map.insert(0, sched_key, [win_dict])
            else:
                target_map[sched_key] = [win_dict]
            return

        if sched_key not in target_map or not isinstance(target_map[sched_key], list):
            if insert_at_top and hasattr(target_map, "insert") and sched_key not in target_map:
                target_map.insert(0, sched_key, [])
            else:
                target_map[sched_key] = []

        sched_list = target_map[sched_key]
        for idx, existing in enumerate(sched_list):
            if isinstance(existing, dict):
                try:
                    ex_s = ScheduleWindow.normalize_time(existing.get("start", ""))
                    ex_e = ScheduleWindow.normalize_time(existing.get("end", ""))
                    if ex_s == win.start and ex_e == win.end:
                        sched_list[idx] = win_dict
                        return
                except Exception:
                    pass
        sched_list.append(win_dict)

    elif action in ("remove_schedule", "remove_pause_window", "clear_schedule"):
        if sched_key not in target_map or not isinstance(target_map[sched_key], list):
            return
        if action == "clear_schedule" or (not s_val and not e_val):
            del target_map[sched_key]
            return

        norm_s = ScheduleWindow.normalize_time(s_val) if s_val else None
        norm_e = ScheduleWindow.normalize_time(e_val) if e_val else None
        sched_list = target_map[sched_key]
        kept = []
        for existing in sched_list:
            if not isinstance(existing, dict):
                kept.append(existing)
                continue
            try:
                ex_s = ScheduleWindow.normalize_time(existing.get("start", ""))
                ex_e = ScheduleWindow.normalize_time(existing.get("end", ""))
                match_s = norm_s is None or ex_s == norm_s
                match_e = norm_e is None or ex_e == norm_e
                if match_s and match_e:
                    continue
            except Exception:
                pass
            kept.append(existing)

        if kept:
            target_map[sched_key] = kept
        else:
            del target_map[sched_key]


def modify_check_config(
    file_path: Path,
    check_id: str,
    action: str,
    key: Optional[str] = None,
    values: Optional[List[str]] = None,
    value: Any = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    days: Optional[Any] = None,
    reason: Optional[str] = None,
) -> Tuple[str, str, List[CheckConfig]]:
    """Modify a check or monitoring schedule in checks.yaml with comment preservation and Pydantic validation.

    Returns:
        (new_yaml_str, unified_diff_str, list_of_validated_check_configs)
    """
    if not file_path.is_file():
        raise FileNotFoundError(f"Checks configuration file not found: {file_path}")

    old_yaml_text = file_path.read_text(encoding="utf-8")
    parser = get_yaml_parser(old_yaml_text)
    doc = parser.load(old_yaml_text)

    is_global_target = str(check_id).strip().lower() in ("*", "global", "all")

    # Detect list vs dictionary format
    if isinstance(doc, list):
        if is_global_target:
            doc = {"schedule": [], "checks": doc}
            check_list = doc["checks"]
        else:
            check_list = doc
    elif isinstance(doc, dict) and "checks" in doc and isinstance(doc["checks"], list):
        check_list = doc["checks"]
    else:
        raise ValueError(f"Unexpected checks.yaml format: expected list or mapping with 'checks' key in {file_path}")

    if is_global_target:
        if action not in SCHEDULE_ACTIONS:
            raise ValueError(
                f"Action '{action}' is not valid for global target '{check_id}'. "
                f"Supported global schedule actions: {', '.join(SCHEDULE_ACTIONS)}"
            )
        _apply_schedule_action(
            target_map=doc,
            action=action,
            start=start,
            end=end,
            days=days,
            reason=reason,
            value=value,
            values=values,
            insert_at_top=True,
        )
    else:
        # Find the target check
        target_check = None
        for item in check_list:
            if isinstance(item, dict) and item.get("id") == check_id:
                target_check = item
                break

        if target_check is None:
            raise ValueError(f"Check with ID '{check_id}' not found in {file_path}")

        if action in SCHEDULE_ACTIONS:
            _apply_schedule_action(
                target_check,
                action=action,
                start=start,
                end=end,
                days=days,
                reason=reason,
                value=value,
                values=values,
                insert_at_top=False,
            )
        else:
            if "params" not in target_check or not isinstance(target_check["params"], dict):
                target_check["params"] = {}

            params = target_check["params"]

            if action in ("disable_check", "disable"):
                target_check["enabled"] = False
                if "enabled" in params:
                    del params["enabled"]

            elif action in ("enable_check", "enable"):
                target_check["enabled"] = True
                if "enabled" in params:
                    del params["enabled"]

            elif action == "set_enabled":
                val = value if value is not None else True
                if isinstance(val, str):
                    target_check["enabled"] = val.lower() in ("true", "1", "yes")
                else:
                    target_check["enabled"] = bool(val)
                if "enabled" in params:
                    del params["enabled"]

            elif action == "add_exclude":
                items_to_add = values or ([str(value)] if value is not None else [])
                if not items_to_add:
                    raise ValueError("No items provided to add to exclude list")
                if "exclude" not in params or not isinstance(params["exclude"], list):
                    params["exclude"] = []
                for val in items_to_add:
                    val_str = str(val).strip()
                    if val_str and val_str not in params["exclude"]:
                        params["exclude"].append(val_str)

            elif action == "remove_exclude":
                items_to_remove = values or ([str(value)] if value is not None else [])
                if "exclude" in params and isinstance(params["exclude"], list):
                    for val in items_to_remove:
                        val_str = str(val).strip()
                        if val_str in params["exclude"]:
                            params["exclude"].remove(val_str)

            elif action == "set_param":
                if not key:
                    raise ValueError("Parameter key is required for action 'set_param'")
                if key == "enabled":
                    if isinstance(value, str):
                        target_check["enabled"] = value.lower() in ("true", "1", "yes")
                    else:
                        target_check["enabled"] = bool(value)
                    if "enabled" in params:
                        del params["enabled"]
                elif key == "interval_seconds":
                    target_check["interval_seconds"] = int(value)
                else:
                    params[key] = value

            else:
                raise ValueError(
                    f"Unsupported action '{action}'. Supported: 'disable_check', 'enable_check', 'add_exclude', "
                    f"'remove_exclude', 'set_param', 'add_schedule', 'set_schedule', 'remove_schedule', 'clear_schedule'"
                )

    # Dump modified document to string
    stream = io.StringIO()
    parser.dump(doc, stream)
    new_yaml_text = stream.getvalue()

    # Pre-validate resulting YAML against Pydantic CheckConfig & ScheduleWindow schemas
    raw_validated = pyyaml.safe_load(new_yaml_text)
    if isinstance(raw_validated, dict):
        for sched_key in ("schedule", "pause_windows", "maintenance_windows", "quiet_hours"):
            if sched_key in raw_validated and raw_validated[sched_key] is not None:
                for win_item in raw_validated[sched_key]:
                    ScheduleWindow.model_validate(win_item)
    raw_items = raw_validated if isinstance(raw_validated, list) else raw_validated.get("checks", [])
    validated_configs = [CheckConfig.model_validate(item) for item in raw_items]

    # Generate unified diff
    diff = "".join(
        difflib.unified_diff(
            old_yaml_text.splitlines(keepends=True),
            new_yaml_text.splitlines(keepends=True),
            fromfile=f"a/{file_path.name}",
            tofile=f"b/{file_path.name}",
        )
    )

    return new_yaml_text, diff, validated_configs


def _get_backup_dir(file_path: Path, backup_dir: Optional[Path] = None) -> Path:
    """Determine the safest writable directory for configuration backups."""
    if backup_dir is not None:
        target_dir = backup_dir
    elif Path("/var/lib/wheatley").is_dir() and str(file_path).startswith("/etc/"):
        target_dir = Path("/var/lib/wheatley/backups")
    elif os.access(file_path.parent, os.W_OK):
        target_dir = file_path.parent
    elif Path("/var/lib/wheatley").is_dir():
        target_dir = Path("/var/lib/wheatley/backups")
    else:
        target_dir = Path(tempfile.gettempdir()) / "wheatley_backups"

    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir


def _write_file_content(file_path: Path, new_content: str) -> None:
    """Write content to file_path, trying atomic replace if parent is writable, or in-place write if parent is read-only."""
    if os.access(file_path.parent, os.W_OK):
        try:
            tmp_path = file_path.with_suffix(f".tmp.{os.getpid()}")
            tmp_path.write_text(new_content, encoding="utf-8")
            tmp_path.replace(file_path)
            return
        except OSError:
            pass

    # In-place write for environments where parent directory is read-only (e.g. systemd ProtectSystem=full)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(new_content)
        f.flush()
        os.fsync(f.fileno())


def apply_checks_file_update(
    file_path: Path,
    new_yaml_text: str,
    backup_dir: Optional[Path] = None,
) -> Path:
    """Safely writes new YAML to file_path after creating a timestamped backup."""
    target_backup_dir = _get_backup_dir(file_path, backup_dir=backup_dir)
    backup_path = target_backup_dir / f"{file_path.name}.bak.{int(time.time())}"

    if file_path.is_file():
        shutil.copy2(file_path, backup_path)

    _write_file_content(file_path, new_yaml_text)
    logger.info("Successfully applied checks configuration update to %s (backup: %s)", file_path, backup_path)
    return backup_path
