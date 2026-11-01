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
      guest: "pve-node-01/100"
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
        values=["backup-cron", "log-rotator"],
    )

    # 1. Verify content added
    assert "backup-cron" in new_yaml
    assert "log-rotator" in new_yaml
    assert "temp-worker" in new_yaml

    # 2. Verify comments preserved
    assert "# Header comment" in new_yaml
    assert "# Docker containers" in new_yaml

    # 3. Verify diff generated
    assert "+        - backup-cron" in diff or "+        - \"backup-cron\"" in diff
    assert "--- a/checks.yaml" in diff

    # 4. Verify Pydantic validation passed
    assert len(configs) == 2
    docker_cfg = next(c for c in configs if c.id == "docker-containers")
    assert "backup-cron" in docker_cfg.params["exclude"]
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


def test_disable_check(tmp_path: Path):
    yaml_file = tmp_path / "checks.yaml"
    yaml_file.write_text(SAMPLE_CHECKS_YAML, encoding="utf-8")

    new_yaml, diff, configs = modify_check_config(
        file_path=yaml_file,
        check_id="docker-containers",
        action="disable_check",
    )

    assert "-  enabled: true" in diff or "-    enabled: true" in diff
    assert "+  enabled: false" in diff or "+    enabled: false" in diff
    docker_cfg = next(c for c in configs if c.id == "docker-containers")
    assert docker_cfg.enabled is False


def test_enable_check(tmp_path: Path):
    disabled_yaml = SAMPLE_CHECKS_YAML.replace("enabled: true", "enabled: false")
    yaml_file = tmp_path / "checks.yaml"
    yaml_file.write_text(disabled_yaml, encoding="utf-8")

    new_yaml, diff, configs = modify_check_config(
        file_path=yaml_file,
        check_id="docker-containers",
        action="enable_check",
    )

    assert "+  enabled: true" in diff or "+    enabled: true" in diff
    docker_cfg = next(c for c in configs if c.id == "docker-containers")
    assert docker_cfg.enabled is True


def test_set_param_fallback_for_enabled(tmp_path: Path):
    yaml_file = tmp_path / "checks.yaml"
    yaml_file.write_text(SAMPLE_CHECKS_YAML, encoding="utf-8")

    new_yaml, diff, configs = modify_check_config(
        file_path=yaml_file,
        check_id="docker-containers",
        action="set_param",
        key="enabled",
        value="false",
    )

    docker_cfg = next(c for c in configs if c.id == "docker-containers")
    assert docker_cfg.enabled is False
    assert "enabled" not in docker_cfg.params


def test_adaptive_yaml_indentation(tmp_path: Path):
    # PyYAML style: sequence indent 2, offset 0 (no extra spaces before '- id:')
    pyyaml_style = "checks:\n- id: node-health\n  type: node_metrics\n  enabled: true\n  interval_seconds: 60\n"
    yaml_file = tmp_path / "checks.yaml"
    yaml_file.write_text(pyyaml_style, encoding="utf-8")

    new_yaml, diff, configs = modify_check_config(
        file_path=yaml_file,
        check_id="node-health",
        action="disable_check",
    )

    # Verify only the enabled line was changed, not the sequence formatting
    assert "-  enabled: true" in diff
    assert "+  enabled: false" in diff
    assert "- id: node-health" in new_yaml
    assert "--- a/checks.yaml" in diff


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


def test_apply_checks_file_update_readonly_parent(tmp_path: Path):
    # Simulate /etc/wheatley where directory is read-only but file is writable
    sub_dir = tmp_path / "readonly_etc"
    sub_dir.mkdir()
    yaml_file = sub_dir / "checks.yaml"
    yaml_file.write_text(SAMPLE_CHECKS_YAML, encoding="utf-8")

    # Designated backup dir outside the readonly dir
    backup_dir = tmp_path / "var_lib_backups"

    # Make parent directory read-only
    sub_dir.chmod(0o555)
    try:
        new_content = SAMPLE_CHECKS_YAML + "\n# In-place write\n"
        backup_path = apply_checks_file_update(yaml_file, new_content, backup_dir=backup_dir)

        assert yaml_file.read_text(encoding="utf-8") == new_content
        assert backup_path.is_file()
        assert backup_path.parent == backup_dir
        assert backup_path.read_text(encoding="utf-8") == SAMPLE_CHECKS_YAML
    finally:
        sub_dir.chmod(0o755)


def test_per_check_schedule_actions(tmp_path: Path):
    yaml_file = tmp_path / "checks.yaml"
    yaml_file.write_text(SAMPLE_CHECKS_YAML, encoding="utf-8")

    # 1. Add a schedule window to docker-containers
    new_yaml, diff, configs = modify_check_config(
        file_path=yaml_file,
        check_id="docker-containers",
        action="add_schedule",
        start="02:00",
        end="04:00",
        days=["sun"],
        reason="Weekly backup",
    )
    assert "+    schedule:" in diff or "+  schedule:" in diff
    docker_cfg = next(c for c in configs if c.id == "docker-containers")
    assert len(docker_cfg.schedule) == 1
    assert docker_cfg.schedule[0].start == "02:00"
    assert docker_cfg.schedule[0].end == "04:00"
    assert docker_cfg.schedule[0].days == ["sun"]
    assert docker_cfg.schedule[0].reason == "Weekly backup"

    # 2. Remove the schedule window
    yaml_file.write_text(new_yaml, encoding="utf-8")
    new_yaml2, diff2, configs2 = modify_check_config(
        file_path=yaml_file,
        check_id="docker-containers",
        action="remove_schedule",
        start="02:00",
        end="04:00",
    )
    docker_cfg2 = next(c for c in configs2 if c.id == "docker-containers")
    assert len(docker_cfg2.schedule) == 0


def test_global_schedule_actions(tmp_path: Path):
    yaml_file = tmp_path / "checks.yaml"
    yaml_file.write_text(SAMPLE_CHECKS_YAML, encoding="utf-8")

    # 1. Add a global schedule window via check_id="*"
    new_yaml, diff, configs = modify_check_config(
        file_path=yaml_file,
        check_id="*",
        action="add_schedule",
        start="23:00",
        end="05:00",
        days="weekdays",
        reason="Overnight quiet hours",
    )
    assert "schedule:" in new_yaml
    assert "23:00" in new_yaml
    assert "05:00" in new_yaml
    assert "# Header comment" in new_yaml

    # 2. Clear the global schedule
    yaml_file.write_text(new_yaml, encoding="utf-8")
    new_yaml2, diff2, _ = modify_check_config(
        file_path=yaml_file,
        check_id="global",
        action="clear_schedule",
    )
    assert "23:00" not in new_yaml2


