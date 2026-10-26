"""Wheatley Config Editor - Safe Round-Trip YAML Check Configuration Modification"""

from __future__ import annotations

import difflib
import io
import logging
from pathlib import Path
import shutil
import time
from typing import Any, List, Optional, Tuple
from ruamel.yaml import YAML
import yaml as pyyaml

from wheatley.config import CheckConfig

logger = logging.getLogger("wheatley.tools.config_editor")


def get_yaml_parser() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
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
    parser = get_yaml_parser()
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

    if action == "add_exclude":
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
        params[key] = value

    else:
        raise ValueError(f"Unsupported action '{action}'. Supported: 'add_exclude', 'remove_exclude', 'set_param'")

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


def apply_checks_file_update(file_path: Path, new_yaml_text: str) -> Path:
    """Atomically writes new YAML to file_path after creating a timestamped backup."""
    backup_path = file_path.with_suffix(f".bak.{int(time.time())}")
    if file_path.is_file():
        shutil.copy2(file_path, backup_path)

    tmp_path = file_path.with_suffix(".tmp")
    tmp_path.write_text(new_yaml_text, encoding="utf-8")
    tmp_path.replace(file_path)
    logger.info("Successfully applied checks configuration update to %s (backup: %s)", file_path, backup_path)
    return backup_path
