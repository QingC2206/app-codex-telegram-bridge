"""Quota namespace ownership shared by storage and quota accessors."""
ROOT = 'quota_accounts'


def legacy_quota_key(key):
    return key.startswith('quota_') and key not in {ROOT, 'quota_monitor_health'}


def migrated(state):
    return isinstance(state.get(ROOT), dict) and state[ROOT].get('version') == 1


def validate(state):
    if migrated(state) and any(legacy_quota_key(key) for key in state):
        raise ValueError('Account-scoped quota state cannot contain legacy root quota keys.')
