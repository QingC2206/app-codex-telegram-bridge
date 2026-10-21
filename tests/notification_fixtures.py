"""Delivery/rendering test double; real provenance is covered in test_completion_routing."""
from codex_telegram_bridge.telegram.completion_routing import Decision


def verified_completion(payload):
    return Decision('conversation', 'verified-test-fixture',
                    str(payload.get('thread-id') or payload.get('thread_id') or payload.get('session_id') or 'fixture-task'))
