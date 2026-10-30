"""Wheatley Config Editor - Safe Round-Trip YAML Check Configuration Modification"""

from __future__ import annotations

import difflib
import io
import logging
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any, List, Optional, Tuple
from ruamel.yaml import YAML
import yaml as pyyaml

from wheatley.config import CheckConfig

logger = logging.getLogger("wheatley.tools.config_editor")


def get_yaml_parser(text: Optional[str] = None) -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    if text and ("\n  - " in text or "\n  -" in text):
        y.indent(mapping=2, sequence=4, offset=2)
    else:
        y.indent(mapping=2, sequence=2, offset=0)
    return y


def modify_check_config(
    file_path: Path,
    check_id: str,
    action: str,
    key: Optional[str] = None,
    values: Optional[List[str]] = None,
    value: Any = None,
) -> Tuple[str, str, List[CheckConfig]]:
    """Modify a check in checks.yaml with comment preservation and Pydantic validation.

    Returns:
        (new_yaml_str, unified_diff_str, list_of_validated_check_configs)
    """
    if not file_path.is_file():
        raise FileNotFoundError(f"Checks configuration file not found: {file_path}")

    old_yaml_text = file_path.read_text(encoding="utf-8")
    parser = get_yaml_parser(old_yaml_text)
    doc = parser.load(old_yaml_text)

    # Detect list vs dictionary format
    if isinstance(doc, list):
        check_list = doc
    elif isinstance(doc, dict) and "checks" in doc and isinstance(doc["checks"], list):
        check_list = doc["checks"]
    else:
        raise ValueError(f"Unexpected checks.yaml format: expected list or mapping with 'checks' key in {file_path}")

    # Find the target check
    target_check = None
    for item in check_list:
        if isinstance(item, dict) and item.get("id") == check_id:
            target_check = item
            break

    if target_check is None:
        raise ValueError(f"Check with ID '{check_id}' not found in {file_path}")

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
            f"Unsupported action '{action}'. Supported: 'disable_check', 'enable_check', 'add_exclude', 'remove_exclude', 'set_param'"
        )

    # Dump modified document to string
    stream = io.StringIO()
    parser.dump(doc, stream)
    new_yaml_text = stream.getvalue()

    # Pre-validate resulting YAML against Pydantic CheckConfig schema
    raw_validated = pyyaml.safe_load(new_yaml_text)
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
