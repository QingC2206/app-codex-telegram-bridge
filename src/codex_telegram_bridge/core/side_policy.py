"""Verified process/thread policy for Codex's built-in side-chat agent tools.

This does not claim to restrict arbitrary external MCP or plugin capabilities.
"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from ..i18n import tr as _t
from ..platforms.processes import spawn_options

POLICY_VERSION = 'side-no-builtin-agents-v1'
OVERRIDES = {'agents.enabled': False, 'features.multi_agent': False}
_SCHEMA_VERIFIED = set()
_SCHEMA_LOCK = threading.Lock()


class SidePolicyError(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(_t('无法确认侧聊已禁用 Codex 内置子代理，本次问题未提交。请更新 Codex 后重新创建侧聊；主任务不受影响。'))


def server_arguments(executable, supplied=None):
    arguments = list(supplied or [str(executable), '-c', 'notify=[]', 'app-server'])
    if (not arguments or any(not isinstance(value, str) for value in arguments)
            or Path(arguments[0]).resolve() != Path(executable).resolve()
            or arguments[-1] != 'app-server'
            or any(value == '--enable' or value.startswith('--enable=') for value in arguments)):
        raise SidePolicyError('unsupported-side-server-command')
    return [*arguments[:-1], '--strict-config', '-c', 'agents.enabled=false',
            '-c', 'features.multi_agent=false', 'app-server']


def validate_startup(arguments, environment):
    """Validate known configuration keys without running a server or model."""
    try:
        result = subprocess.run([*arguments, '--print-startup-config'], env=environment,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                encoding='utf-8', errors='replace',
                                timeout=15, check=False, **spawn_options())
        response = json.loads(result.stdout) if result.returncode == 0 else None
        if (not isinstance(response, dict) or not isinstance(response.get('appServerVersion'), str)
                or not response['appServerVersion'].strip()):
            raise ValueError('unrecognized startup response')
        validate_schema(arguments[0], environment, response['appServerVersion'])
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # CLI diagnostics can contain local config; never include them in UI.
        raise SidePolicyError('unsupported-strict-side-configuration') from exc


def validate_schema(executable, environment, version):
    """Reject older servers that silently ignore the threadId feature scope."""
    try:
        binary = Path(executable).resolve()
        stat = binary.stat()
        signature = (str(binary), stat.st_mtime_ns, stat.st_size, version, POLICY_VERSION)
        with _SCHEMA_LOCK:
            if signature in _SCHEMA_VERIFIED:
                return
            with tempfile.TemporaryDirectory(prefix='codex-side-schema-') as directory:
                result = subprocess.run([str(binary), 'app-server', 'generate-json-schema',
                                         '--experimental', '--out', directory], env=environment,
                                        stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                        encoding='utf-8', errors='replace',
                                        timeout=15, check=False, **spawn_options())
                if result.returncode != 0:
                    raise ValueError('schema generation unavailable')
                folder = Path(directory) / 'v2'
                feature = json.loads((folder / 'ExperimentalFeatureListParams.json').read_text(encoding='utf-8'))
                fork = json.loads((folder / 'ThreadForkParams.json').read_text(encoding='utf-8'))
                thread = feature.get('properties', {}).get('threadId', {})
                config = fork.get('properties', {}).get('config', {})
                def allows_type(field, expected):
                    types = field.get('type')
                    return types == expected or isinstance(types, list) and expected in types
                if (not allows_type(thread, 'string') or not allows_type(config, 'object')
                        or config.get('additionalProperties') is not True):
                    raise ValueError('thread feature scope or fork overrides unsupported')
            # Keep cache bounded and only after a complete capability check.
            _SCHEMA_VERIFIED.clear()
            _SCHEMA_VERIFIED.add(signature)
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
        raise SidePolicyError('unsupported-thread-policy-capabilities') from exc


def verify_features(request, thread_id=None):
    """Read server-effective feature state, including refreshed thread config."""
    cursor = None
    cursors = set()
    found = False
    names = set()
    deadline = time.monotonic() + 15
    try:
        for _ in range(12):
            params = {'limit': 100}
            if thread_id:
                params['threadId'] = thread_id
            if cursor:
                params['cursor'] = cursor
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('feature verification deadline exceeded')
            result = request('experimentalFeature/list', params, timeout=remaining, ensure=False)
            if not isinstance(result, dict) or 'nextCursor' not in result:
                raise ValueError('feature page completeness unconfirmed')
            data = result.get('data')
            if not isinstance(data, list):
                raise ValueError('missing feature list')
            for feature in data:
                if not isinstance(feature, dict) or not isinstance(feature.get('name'), str):
                    raise ValueError('invalid feature record')
                name = feature['name']
                if name in names:
                    raise ValueError('duplicate feature record')
                names.add(name)
                if name == 'multi_agent':
                    found = feature.get('enabled') is False and feature.get('stage') in {
                        'stable', 'beta', 'underDevelopment'}
                    if not found:
                        raise ValueError('multi-agent feature not verifiably disabled')
                elif name.startswith('multi_agent') and feature.get('enabled') is not False:
                    raise ValueError('unverified alternate multi-agent feature')
            cursor = result.get('nextCursor')
            if cursor is None:
                if not found:
                    raise ValueError('multi-agent feature absent')
                return
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise ValueError('invalid feature cursor')
            cursors.add(cursor)
        raise ValueError('feature list incomplete')
    except SidePolicyError:
        raise
    except Exception as exc:
        raise SidePolicyError('effective-side-features-unverified') from exc


def fork_options(params):
    result = deepcopy(params)
    result.update(ephemeral=True, excludeTurns=True, sandbox='read-only', approvalPolicy='never')
    config = result.get('config')
    if config is not None and not isinstance(config, dict):
        raise SidePolicyError('invalid-side-fork-config')
    result['config'] = {**(config or {}), **OVERRIDES}
    return result


def verify_fork(result, parent):
    if not isinstance(result, dict):
        raise SidePolicyError('side-fork-policy-not-confirmed')
    thread = result.get('thread') or {}
    sandbox = result.get('sandbox') or {}
    if not isinstance(thread, dict) or not isinstance(sandbox, dict):
        raise SidePolicyError('side-fork-policy-not-confirmed')
    thread_id = thread.get('id')
    if (not isinstance(thread_id, str) or not thread_id or thread_id == parent
            or thread.get('ephemeral') is not True or result.get('approvalPolicy') != 'never'
            or sandbox.get('type') != 'readOnly'):
        raise SidePolicyError('side-fork-policy-not-confirmed')
    return thread_id
