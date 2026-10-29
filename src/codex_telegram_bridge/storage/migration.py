"""One-way import of the early app-server UI, without replaying any input.

The old database remains the read-only compatibility source for its buttons
and drafts. New conversation ownership and preferences have one authority:
StateStore. A crash during import can only repeat idempotent local writes.
"""
import json
import re
import time

from ..telegram import notifications as notify


def migrate_controller(bot, legacy, settings):
    with legacy.lock:
        conversations = {row['id']: dict(row) for row in legacy.db.execute('SELECT * FROM conversations')}
    # Pending pre-upgrade outputs also need the common mute, routing and
    # delivery-confirmation rules. This is independent of preference import.
    with legacy.lock, legacy.db:
        for saved in legacy.db.execute("SELECT id,route FROM outbox WHERE status IN ('pending','retry','sending')").fetchall():
            route = json.loads(saved['route'])
            if route.get('controller') or route.get('request'):
                continue
            row = conversations.get(route.get('conversation'), {})
            tid = route.get('thread_id') or row.get('thread_id')
            route.update(controller=True, background=bool(route.get('notice')),
                         quiet_generation=0)
            if tid and route.get('turn_id') and route.get('notice'):
                route['completion'] = [tid, route['turn_id']]
                if route.get('delivery_key'):
                    route['completion_group'] = route['delivery_key']
                job = legacy.job(route.get('job')) if route.get('job') is not None else None
                route['outcome'] = '😢' if (job or {}).get('status') in {'failed','interrupted','cancelled'} else '🎉'
            if tid:
                route['legacy_thread'] = tid
            legacy.db.execute('UPDATE outbox SET route=? WHERE id=?', (json.dumps(route), saved['id']))
    if bot.state.snapshot().get('controller_schema') == 2:
        return
    def apply(state):
        managed = state.setdefault('managed_threads', {})
        for row in conversations.values():
            tid = row.get('thread_id')
            if tid and row.get('origin') == 'app-server':
                managed.setdefault(tid, {'origin': 'bot_created', 'cwd': row['cwd'],
                                         'created_at': int(time.time())})
        state['default_cwd'] = legacy.get('cwd', settings['cwd'])
        state['muted'] = not legacy.get('completion_notifications', True)
        state['update_offset'] = max(state.get('update_offset', 0), legacy.get('offset', 0))
        # Preserve selection for old links; ordinary text still follows the
        # canonical explicit-target rule, never this historical selection.
        state['legacy_selection'] = legacy.get('selected')
        state['runtime_model'] = legacy.get('model', settings.get('model'))
        state['runtime_effort'] = legacy.get('effort', settings.get('effort'))
    bot.state.mutate(apply)
    with legacy.lock:
        routes = list(legacy.db.execute('SELECT message_id,route FROM message_routes'))
        views = list(legacy.db.execute('SELECT * FROM reply_views'))
    for message_id, raw in routes:
        route = json.loads(raw)
        row = conversations.get(route.get('conversation'), {})
        tid = route.get('thread_id') or row.get('thread_id')
        if not isinstance(tid, str) or not re.fullmatch(r'[A-Za-z0-9-]{6,64}', tid):
            continue
        notify.remember_task_message(bot.token, {'chat_id': bot.chat_id,
            'reply_markup': {'inline_keyboard': [[{'text': 'Reply', 'callback_data': 'direct|'+tid}]]}},
            {'message_id': message_id})
    cache_map = dict(bot.state.snapshot().get('legacy_reply_cache', {}))
    for view in views:
        row = conversations.get(view['conversation'], {})
        if view['id'] not in cache_map and row.get('thread_id'):
            cached = notify.cache_full_reply({'last_assistant_message': view['text'], 'cwd': row['cwd']},
                                             row['thread_id'], force=True)
            if cached:
                cache_map[view['id']] = cached
    with legacy.lock:
        pending = [dict(r) for r in legacy.db.execute("SELECT * FROM jobs WHERE status IN ('submitted','running','dispatching','uncertain')")]
    for job in pending:
        source = legacy.get('input_message:'+str(job['update_id']))
        row = conversations.get(job['conversation'], {})
        if isinstance(source, int) and source > 0 and row.get('thread_id'):
            key = 'update:'+str(job['update_id'])
            legacy.start_feedback(key, source)
            legacy.update_feedback(key, ready=True, binding={'thread_id': row['thread_id'],
                'turn_id': job.get('turn_id') or '', 'source': source},
                outcome='uncertain' if job['status'] in {'dispatching', 'uncertain'} else '')
    for record in legacy.feedback_records():
        binding = record['binding']
        job = legacy.job(binding['job']) if binding.get('job') is not None else None
        row = conversations.get((job or {}).get('conversation') or binding.get('conversation'), {})
        tid = binding.get('thread_id') or row.get('thread_id')
        if tid:
            legacy.update_feedback(record['id'], binding={'thread_id': tid,
                'turn_id': binding.get('turn_id') or (job or {}).get('turn_id') or '', 'source': record['message']})
    bot.state.update(legacy_reply_cache=cache_map, controller_schema=2)
