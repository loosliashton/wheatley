"""Unit tests for Wheatley Auto-Discovery Module"""

import pytest
from wheatley.config import WheatleyConfig, TelegramConfig, ChatConfig
from wheatley.discovery.discover import InventoryDiscovery, DiscoveredGuest, DiscoveredService


@pytest.fixture
def dummy_config():
    return WheatleyConfig(
        chat=ChatConfig(telegram=TelegramConfig(bot_token="dummy:token", allowed_user_ids=[123456])),
        allowlist=["uptime"],
        self_guest="pve1/110",
    )


def test_services_catalog_loading(dummy_config):
    discovery = InventoryDiscovery(dummy_config)
    services = discovery.services_catalog
    assert len(services) >= 10
    ids = [s["id"] for s in services]
    assert "plex" in ids
    assert "frigate" in ids
    assert "crafty" in ids
    assert "home-assistant" in ids
    assert "omv" in ids


def test_generate_draft_config(dummy_config):
    discovery = InventoryDiscovery(dummy_config)

    guests = [
        DiscoveredGuest(
            node="pve1",
            vmid=102,
            name="plex-ct",
            type="lxc",
            status="running",
            onboot=True,
            ips=["192.168.1.102"],
            services=[
                DiscoveredService(
                    service_id="plex",
                    name="Plex Media Server",
                    category="media",
                    port=32400,
                    protocol="http",
                    path="/identity",
                    url_template="http://{guest:pve1/102}:32400/identity",
                    ip="192.168.1.102",
                )
            ],
        ),
        DiscoveredGuest(
            node="pve1",
            vmid=100,
            name="Docker-VM",
            type="qemu",
            status="running",
            onboot=True,
            ips=["192.168.1.100"],
            has_docker=True,
        ),
        DiscoveredGuest(
            node="pve1",
            vmid=103,
            name="HA-clone",
            type="qemu",
            status="stopped",
            onboot=False,
            ips=[],
        ),
        DiscoveredGuest(
            node="pve1",
            vmid=110,
            name="wheatley",
            type="lxc",
            status="running",
            onboot=True,
            ips=["10.0.0.110"],
            is_self=True,
        ),
    ]

    draft = discovery.generate_draft_config(guests)

    # Validate structure
    assert draft["self_guest"] == "pve1/110"
    checks = draft["checks"]
    check_ids = [c["id"] for c in checks]

    # Verify standard system checks
    assert "node-health" in check_ids
    assert "storage-health" in check_ids
    assert "guests-up" in check_ids

    # Verify stopped clone excluded from guests-up
    guest_check = next(c for c in checks if c["id"] == "guests-up")
    assert "pve1/103" in guest_check["params"]["exclude"]

    # Verify discovered service check
    assert "plex-102" in check_ids
    plex_check = next(c for c in checks if c["id"] == "plex-102")
    assert plex_check["params"]["url"] == "http://{guest:pve1/102}:32400/identity"

    # Verify Docker check
    assert "docker-100" in check_ids
    docker_check = next(c for c in checks if c["id"] == "docker-100")
    assert docker_check["params"]["guest"] == "pve1/100"
    assert docker_check["params"]["containers"] == "*"


def test_format_summary_markdown(dummy_config):
    discovery = InventoryDiscovery(dummy_config)
    guests = [
        DiscoveredGuest(
            node="pve1",
            vmid=102,
            name="plex",
            type="lxc",
            status="running",
            ips=["192.168.1.102"],
            services=[
                DiscoveredService(
                    service_id="plex",
                    name="Plex Media Server",
                    category="media",
                    port=32400,
                    protocol="http",
                    path="/identity",
                    url_template="http://{guest:pve1/102}:32400/identity",
                    ip="192.168.1.102",
                )
            ],
        )
    ]
    summary = discovery.format_summary_markdown(guests)
    assert "Plex Media Server" in summary
    assert "32400" in summary
    assert "pve1" in summary


def test_cmd_discover_output_overwrite_protection(tmp_path, monkeypatch):
    import argparse
    from unittest.mock import AsyncMock, patch
    from wheatley.cli import cmd_discover

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("nodes: []\nchat:\n  telegram:\n    bot_token: 't'\n    allowed_user_ids: [1]\n")
    sec_file = tmp_path / "secrets.env"
    sec_file.write_text("")

    out_file = tmp_path / "checks.yaml"
    out_file.write_text("# existing customized checks\n")

    # Mock scan_all to return empty guests
    with patch("wheatley.discovery.discover.InventoryDiscovery.scan_all", new_callable=AsyncMock) as mock_scan:
        mock_scan.return_value = []

        # Running without --force when file exists should exit with code 1
        args_no_force = argparse.Namespace(
            config=str(cfg_file),
            secrets=str(sec_file),
            output=str(out_file),
            force=False,
            json=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            cmd_discover(args_no_force)
        assert exc_info.value.code == 1
        assert out_file.read_text() == "# existing customized checks\n"

        # Running with --force should succeed, create a backup, and write new checks
        args_force = argparse.Namespace(
            config=str(cfg_file),
            secrets=str(sec_file),
            output=str(out_file),
            force=True,
            json=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            cmd_discover(args_force)
        assert exc_info.value.code == 0
        bak_file = tmp_path / "checks.yaml.bak"
        assert bak_file.exists()
        assert bak_file.read_text() == "# existing customized checks\n"
        assert "checks:" in out_file.read_text()
