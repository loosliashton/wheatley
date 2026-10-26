"""Unit tests for ApprovalStore and Dynamic Allowlist System"""

import os
from datetime import datetime, timedelta, timezone
import pytest
from wheatley.approvals import ApprovalStore


@pytest.fixture
def store(tmp_path):
    db_file = tmp_path / "test_approvals.db"
    return ApprovalStore(str(db_file))


def test_create_and_get_proposal(store):
    prop = store.create_proposal(
        proposal_type="ALLOWLIST_RULE",
        target="cave/101",
        command="snapraid status",
        summary="Check snapraid parity state",
        risk="LOW",
        pattern="snapraid status *",
        expires_minutes=15,
    )
    assert prop.id.startswith("prop_")
    assert prop.status == "PENDING"
    assert prop.is_pending is True
    assert prop.is_expired is False
    assert prop.hash is not None and len(prop.hash) == 64

    fetched = store.get_proposal(prop.id)
    assert fetched is not None
    assert fetched.id == prop.id
    assert fetched.hash == prop.hash
    assert fetched.command == "snapraid status"
    assert fetched.target == "cave/101"
    assert fetched.pattern == "snapraid status *"


def test_proposal_expiration(store):
    # Create proposal with negative expiration (already expired)
    prop = store.create_proposal(
        proposal_type="ACTION",
        target="cave",
        command="systemctl restart frigate",
        summary="Restart frigate",
        expires_minutes=-5,
    )
    # When fetched, it should be auto-marked EXPIRED
    fetched = store.get_proposal(prop.id)
    assert fetched is not None
    assert fetched.status == "EXPIRED"
    assert fetched.is_pending is False

    # Attempting to resolve expired proposal should fail
    ok, msg = store.resolve_proposal(prop.id, "APPROVE", user_id=123)
    assert ok is False
    assert "expired" in msg.lower()


def test_single_use_resolution(store):
    prop = store.create_proposal(
        proposal_type="ACTION",
        target="cave",
        command="systemctl restart frigate",
        summary="Restart frigate",
    )
    ok, msg = store.resolve_proposal(prop.id, "APPROVE", user_id=123)
    assert ok is True

    # Re-resolving must fail
    ok2, msg2 = store.resolve_proposal(prop.id, "APPROVE", user_id=123)
    assert ok2 is False
    assert "already approved" in msg2.lower()

    ok3, msg3 = store.resolve_proposal(prop.id, "DENY", user_id=123)
    assert ok3 is False


def test_allow_once_workflow(store):
    prop = store.create_proposal(
        proposal_type="ALLOWLIST_RULE",
        target="cave",
        command="smartctl -a /dev/sda",
        summary="Check disk smart status",
        pattern="smartctl -a *",
    )
    ok, msg = store.resolve_proposal(prop.id, "ALLOW_ONCE", user_id=123)
    assert ok is True
    assert "one-time" in msg.lower()

    # Verify no persistent rule was created in user_allowlist
    active = store.get_active_allowlist("cave")
    assert len(active) == 0

    # Record execution
    store.record_execution(prop.id, "Execution completed successfully with exit code 0")
    updated = store.get_proposal(prop.id)
    assert updated.executed_at is not None
    assert "exit code 0" in updated.execution_result


def test_allow_always_target_scoping(store):
    prop = store.create_proposal(
        proposal_type="ALLOWLIST_RULE",
        target="cave/101",
        command="snapraid status",
        summary="Allow snapraid status on cave/101",
        pattern="snapraid status *",
    )
    ok, msg = store.resolve_proposal(prop.id, "ALLOW_ALWAYS", user_id=123)
    assert ok is True

    # Verify rule is present for target cave/101
    active_101 = store.get_active_allowlist("cave/101")
    assert "snapraid status *" in active_101

    # Verify rule is NOT active for other targets
    assert "snapraid status *" not in store.get_active_allowlist("cave/102")
    assert "snapraid status *" not in store.get_active_allowlist("cave")
    assert "snapraid status *" not in store.get_active_allowlist("doug")


def test_list_and_revoke_rules(store):
    prop1 = store.create_proposal(
        proposal_type="ALLOWLIST_RULE",
        target="cave",
        command="smartctl -a /dev/sda",
        summary="smartctl rule",
        pattern="smartctl -a *",
    )
    store.resolve_proposal(prop1.id, "ALLOW_ALWAYS", user_id=456)

    rules = store.list_user_rules()
    assert len(rules) == 1
    rule_id = rules[0].id
    assert rules[0].target == "cave"
    assert rules[0].pattern == "smartctl -a *"
    assert rules[0].created_by == 456

    # Revoke rule
    assert store.revoke_user_rule(rule_id) is True
    assert len(store.list_user_rules()) == 0
    assert "smartctl -a *" not in store.get_active_allowlist("cave")

    # Revoke non-existent rule
    assert store.revoke_user_rule(9999) is False


def test_get_active_proposals(store):
    p1 = store.create_proposal("ALLOWLIST_RULE", "cave", "smartctl --scan", "Scan disks", expires_minutes=15)
    p2 = store.create_proposal("ALLOWLIST_RULE", "cave", "pvesm status", "Storage status", expires_minutes=-5)

    active = store.get_active_proposals()
    assert len(active) == 1
    assert active[0].id == p1.id

    # Resolve p1
    store.resolve_proposal(p1.id, "ALLOW_ONCE", user_id=123)
    assert len(store.get_active_proposals()) == 0
