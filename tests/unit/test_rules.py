from finops_sentinel.domain.models import ResourceType
from finops_sentinel.domain.rules import PLAYBOOK_ALLOWLIST, is_protected


def test_is_protected_dict_form():
    assert is_protected({"finops:protected": "true"}) is True
    assert is_protected({"finops:protected": "True"}) is True
    assert is_protected({"finops:protected": "false"}) is False
    assert is_protected({"env": "prod"}) is False
    assert is_protected({}) is False
    assert is_protected(None) is False


def test_is_protected_aws_list_form():
    assert is_protected([{"Key": "finops:protected", "Value": "true"}]) is True
    assert is_protected([{"Key": "finops:protected", "Value": "false"}]) is False
    assert is_protected([{"Key": "env", "Value": "prod"}]) is False
    assert is_protected([]) is False


def test_playbook_allowlist_covers_remediable_types_only():
    assert PLAYBOOK_ALLOWLIST[ResourceType.EBS_VOLUME] == "snapshot_then_delete_volume"
    assert PLAYBOOK_ALLOWLIST[ResourceType.ELASTIC_IP] == "release_eip"
    assert PLAYBOOK_ALLOWLIST[ResourceType.EC2_INSTANCE] == "terminate_stopped_instance"
    # Snapshots are inventory-only in v1 — no playbook, so never remediable.
    assert ResourceType.EBS_SNAPSHOT not in PLAYBOOK_ALLOWLIST
