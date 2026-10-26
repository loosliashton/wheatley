"""Unit tests for CheckConfigEditor and roundtrip checks.yaml modification"""

import pytest
from pathlib import Path
from wheatley.tools.config_editor import modify_check_config, apply_checks_file_update


SAMPLE_CHECKS_YAML = """# Header comment
checks:
  # Cluster health
  - id: node-health
    type: node_metrics
    enabled: true
    interval_seconds: 60
    params:
      nodes: "*"

  # Docker containers
  - id: docker-containers
    type: docker
    enabled: true
    interval_seconds: 60
    params:
      guest: "cave/100"
      containers: "*"
      exclude:
        - "temp-worker"
      alert_on:
        - exited
"""


def test_add_exclude_preserves_comments(tmp_path: Path):
    yaml_file = tmp_path / "checks.yaml"
    yaml_file.write_text(SAMPLE_CHECKS_YAML, encoding="utf-8")

    new_yaml, diff, configs = modify_check_config(
        file_path=yaml_file,
        check_id="docker-containers",
        action="add_exclude",
        values=["seedboxapi", "floatplane-downloader"],
    )

    # 1. Verify content added
    assert "seedboxapi" in new_yaml
    assert "floatplane-downloader" in new_yaml
    assert "temp-worker" in new_yaml

    # 2. Verify comments preserved
    assert "# Header comment" in new_yaml
    assert "# Docker containers" in new_yaml

    # 3. Verify diff generated
    assert "+        - seedboxapi" in diff or "+        - \"seedboxapi\"" in diff
    assert "--- a/checks.yaml" in diff

    # 4. Verify Pydantic validation passed
    assert len(configs) == 2
    docker_cfg = next(c for c in configs if c.id == "docker-containers")
    assert "seedboxapi" in docker_cfg.params["exclude"]
    assert "temp-worker" in docker_cfg.params["exclude"]


def test_remove_exclude(tmp_path: Path):
    yaml_file = tmp_path / "checks.yaml"
    yaml_file.write_text(SAMPLE_CHECKS_YAML, encoding="utf-8")

    new_yaml, diff, configs = modify_check_config(
        file_path=yaml_file,
        check_id="docker-containers",
        action="remove_exclude",
        values=["temp-worker"],
    )

    assert "temp-worker" not in new_yaml
    docker_cfg = next(c for c in configs if c.id == "docker-containers")
    assert "temp-worker" not in docker_cfg.params["exclude"]


def test_set_param(tmp_path: Path):
    yaml_file = tmp_path / "checks.yaml"
    yaml_file.write_text(SAMPLE_CHECKS_YAML, encoding="utf-8")

    new_yaml, diff, configs = modify_check_config(
        file_path=yaml_file,
        check_id="docker-containers",
        action="set_param",
        key="ignore_exited_0",
        value=True,
    )

    docker_cfg = next(c for c in configs if c.id == "docker-containers")
    assert docker_cfg.params["ignore_exited_0"] is True


def test_validation_error_on_invalid_check(tmp_path: Path):
    yaml_file = tmp_path / "checks.yaml"
    yaml_file.write_text(SAMPLE_CHECKS_YAML, encoding="utf-8")

    with pytest.raises(ValueError, match="Check with ID 'nonexistent' not found"):
        modify_check_config(
            file_path=yaml_file,
            check_id="nonexistent",
            action="add_exclude",
            values=["foo"],
        )


def test_apply_checks_file_update(tmp_path: Path):
    yaml_file = tmp_path / "checks.yaml"
    yaml_file.write_text(SAMPLE_CHECKS_YAML, encoding="utf-8")

    new_content = SAMPLE_CHECKS_YAML + "\n# Extra appended\n"
    backup_path = apply_checks_file_update(yaml_file, new_content)

    assert yaml_file.read_text(encoding="utf-8") == new_content
    assert backup_path.is_file()
    assert backup_path.read_text(encoding="utf-8") == SAMPLE_CHECKS_YAML
