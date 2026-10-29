"""Durable, profile-local ownership and at-most-once task submission."""
import json
import secrets
import hashlib
import sqlite3
import threading
import time


class BridgeState:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.outbox_event = threading.Event()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=10000')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY, thread_id TEXT, cwd TEXT NOT NULL, title TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                update_id INTEGER PRIMARY KEY, conversation TEXT NOT NULL, prompt TEXT NOT NULL,
                status TEXT NOT NULL, turn_id TEXT, result TEXT, delivery TEXT);
            CREATE TABLE IF NOT EXISTS inbox (id INTEGER PRIMARY KEY, payload TEXT NOT NULL, handled INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS message_routes (message_id INTEGER PRIMARY KEY, route TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, event_key TEXT UNIQUE, method TEXT NOT NULL,
                payload TEXT NOT NULL, route TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                due REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, error TEXT, message_id INTEGER);
            CREATE TABLE IF NOT EXISTS telemetry (conversation TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS rejected_updates (id INTEGER PRIMARY KEY, error TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS hook_events (event_key TEXT PRIMARY KEY, payload TEXT NOT NULL, created REAL);
            CREATE TABLE IF NOT EXISTS reply_views (id TEXT PRIMARY KEY, conversation TEXT NOT NULL,
                text TEXT NOT NULL, created REAL NOT NULL, turn_id TEXT);
            CREATE TABLE IF NOT EXISTS feedback (id TEXT PRIMARY KEY, message INTEGER NOT NULL,
                created REAL NOT NULL, ready INTEGER NOT NULL DEFAULT 0,
                outcome TEXT NOT NULL DEFAULT '', binding TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS feedback_outbox (feedback TEXT, outbox INTEGER,
                PRIMARY KEY(feedback,outbox));
            CREATE TABLE IF NOT EXISTS outbox_uploads (outbox INTEGER PRIMARY KEY,
                filename TEXT NOT NULL, content_type TEXT NOT NULL, content BLOB NOT NULL);
        ''')
        for table, fields in {'conversations': {'origin': "TEXT NOT NULL DEFAULT 'app-server'"},
                'reply_views': {'kind': "TEXT NOT NULL DEFAULT 'answer'"},
                'outbox': {'progress_key': 'TEXT'},
                'jobs': {'input': 'TEXT', 'kind': "TEXT NOT NULL DEFAULT 'turn'", 'cancel_requested': 'INTEGER NOT NULL DEFAULT 0'}}.items():
            known = {r[1] for r in self.db.execute('PRAGMA table_info('+table+')')}
            for column, definition in fields.items():
                if column not in known:
                    self.db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        self.db.commit()
        if self.get('accept_after') is None:
            self.set('accept_after', int(time.time()))

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))

    def receive(self, update):
        update_id = int(update['update_id'])
        with self.lock, self.db:
            if update_id < self.get('offset', 0):
                return False
            self.db.execute('INSERT OR IGNORE INTO inbox(id,payload) VALUES (?,?)', (update_id, json.dumps(update)))
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('offset', json.dumps(update_id+1)))
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('last_update_at', json.dumps(time.time())))
            return True

    def reject_update(self, update_id, error):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO rejected_updates VALUES (?,?,?)', (update_id, error, time.time()))
            self.db.execute('DELETE FROM inbox WHERE id=?', (update_id,))

    def pending_updates(self):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM inbox WHERE handled=0 ORDER BY id')]

    def handled(self, update_id):
        with self.lock, self.db:
            self.db.execute('DELETE FROM inbox WHERE id=?', (update_id,))

    def job_exists(self, update_id):
        with self.lock:
            return self.db.execute('SELECT 1 FROM jobs WHERE update_id=?', (update_id,)).fetchone() is not None

    def conversation(self, identifier=None):
        identifier = identifier or self.get('selected')
        with self.lock:
            row = self.db.execute('SELECT * FROM conversations WHERE id=?', (identifier,)).fetchone()
            return dict(row) if row else None

    def new_conversation(self, cwd, title='新对话'):
        identifier = secrets.token_hex(4)
        with self.lock, self.db:
            self.db.execute('INSERT INTO conversations(id,thread_id,cwd,title) VALUES (?,NULL,?,?)', (identifier, cwd, title[:100]))
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('selected', json.dumps(identifier)))
        return self.conversation(identifier)

    def conversations(self):
        with self.lock:
            return [dict(r) for r in self.db.execute('SELECT * FROM conversations ORDER BY rowid DESC LIMIT 100')]

    def by_thread(self, thread_id):
        with self.lock:
            row = self.db.execute('SELECT * FROM conversations WHERE thread_id=?', (thread_id,)).fetchone()
            return dict(row) if row else None

    def rename(self, conversation, title):
        with self.lock, self.db:
            self.db.execute('UPDATE conversations SET title=? WHERE id=?', (title[:180], conversation))

    def save_view(self, conversation, text, turn_id='', key=None, kind='answer'):
        identifier = hashlib.sha256((key or conversation+'\0'+turn_id+'\0'+text).encode()).hexdigest()[:16]
        with self.lock, self.db:
            self.db.execute('INSERT OR IGNORE INTO reply_views(id,conversation,text,created,turn_id,kind) VALUES (?,?,?,?,?,?)', (identifier, conversation, text, time.time(), turn_id,kind))
        return identifier

    def view(self, identifier):
        with self.lock:
            row = self.db.execute('SELECT * FROM reply_views WHERE id=?', (identifier,)).fetchone()
            return dict(row) if row else None

    def last_view(self, conversation):
        with self.lock:
            row = self.db.execute("SELECT * FROM reply_views WHERE conversation=? AND kind='answer' ORDER BY created DESC LIMIT 1", (conversation,)).fetchone()
            return dict(row) if row else None

    def turn_view(self, conversation, turn_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM reply_views WHERE conversation=? AND turn_id=? "
                                  "AND kind='answer' ORDER BY created DESC LIMIT 1", (conversation, turn_id)).fetchone()
            return dict(row) if row else None

    def legacy_cards(self):
        with self.lock:
            rows=self.db.execute("SELECT * FROM outbox WHERE method='sendMessage' AND status='sent' AND message_id IS NOT NULL ORDER BY id DESC LIMIT 20").fetchall()
            cards=[]
            for row in rows:
                payload=json.loads(row['payload'])
                text=payload.get('text','')
                if not payload.get('_markdown') and (text.startswith(('Desktop 完成 · ','对话 ')) or 'missing-code-signing-identity' in text):
                    cards.append({**dict(row),'payload':payload,'route':json.loads(row['route'])})
            return list(reversed(cards[:3]))

    def hook_event(self, key):
        with self.lock:
            row=self.db.execute('SELECT payload,created FROM hook_events WHERE event_key=?',(key,)).fetchone()
            return {'payload':json.loads(row['payload']),'created':row['created']} if row else None

    def desktop_failure_cards(self):
        with self.lock:
            cards=[]
            for row in self.db.execute("SELECT * FROM outbox WHERE status='sent' AND message_id IS NOT NULL "
                                       "AND method IN ('sendMessage','sendRichMessage') ORDER BY id DESC LIMIT 30"):
                payload=json.loads(row['payload']);route=json.loads(row['route'])
                if route.get('origin')=='desktop' and ('ux|continue|' in json.dumps(payload.get('reply_markup',{}))
                        or 'missing-code-signing-identity' in payload.get('text','')):
                    cards.append({**dict(row),'payload':payload,'route':route})
            return list(reversed(cards[:5]))

    def sent_message(self, event_key):
        with self.lock:
            row = self.db.execute("SELECT message_id FROM outbox WHERE event_key=? AND status='sent'", (event_key+':0',)).fetchone()
            return row[0] if row else None

    def claim_desktop_input(self, identifier, *, retry=False):
        """Only an explicitly failed, unsent draft may be manually retried."""
        with self.lock, self.db:
            return self.db.execute("UPDATE jobs SET status='submitting' WHERE update_id=? "
                                   "AND kind='desktop' AND status=? AND cancel_requested=0",
                                   (identifier, 'failed' if retry else 'queued')).rowcount == 1

    def complete_desktop_jobs(self, conversation, turn_id, text):
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET status='completed',result=? WHERE conversation=? "
                            "AND kind='desktop' AND turn_id=? AND status IN ('submitted','uncertain')",
                            (text, conversation, turn_id))

    def bind(self, identifier, thread_id):
        with self.lock, self.db:
            self.db.execute('UPDATE conversations SET thread_id=? WHERE id=?', (thread_id, identifier))

    def enqueue(self, update_id, conversation, prompt, inputs=None, kind='turn'):
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO jobs(update_id,conversation,prompt,status,input,kind) VALUES (?,?,?,'queued',?,?)",
                            (update_id, conversation, prompt, json.dumps(inputs) if inputs else None, kind))

    def job(self, update_id):
        with self.lock:
            row = self.db.execute('SELECT * FROM jobs WHERE update_id=?', (update_id,)).fetchone()
            return dict(row) if row else None

    def submit(self, update_id):
        with self.lock, self.db:
            return self.db.execute("UPDATE jobs SET status='submitting' WHERE update_id=? AND status='dispatching' AND cancel_requested=0",
                                   (update_id,)).rowcount == 1

    def take(self):
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM jobs WHERE status='queued' AND kind='turn' ORDER BY update_id LIMIT 1").fetchone()
            if not row:
                return None
            self.db.execute("UPDATE jobs SET status='dispatching' WHERE update_id=?", (row['update_id'],))
            return dict(row)

    def update_job(self, update_id, **fields):
        if not fields or set(fields) - {'status', 'turn_id', 'result', 'delivery'}:
            raise ValueError('Invalid job fields')
        with self.lock, self.db:
            self.db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in fields)+' WHERE update_id=?',
                            (*fields.values(), update_id))

    def recover(self):
        with self.lock, self.db:
            rows = self.db.execute("SELECT update_id FROM jobs WHERE status IN ('dispatching','submitting','running')").fetchall()
            self.db.execute("UPDATE jobs SET status='uncertain', result='服务中断，执行结果未确认；未自动重试。' "
                            "WHERE status IN ('dispatching','submitting','running')")
            self.db.execute("UPDATE outbox SET status='unknown',error='服务中断，投递结果未确认' WHERE status='sending'")
            return len(rows)

    def last(self, conversation):
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE conversation=? AND kind!='steer' ORDER BY update_id DESC LIMIT 1",
                                  (conversation,)).fetchone()
            return dict(row) if row else None

    def cancel_queued(self, conversation=None):
        with self.lock, self.db:
            scope = ' AND conversation=?' if conversation else ''
            params = (conversation,) if conversation else ()
            self.db.execute("UPDATE jobs SET cancel_requested=1 WHERE status IN ('queued','dispatching','submitting','running')"+scope, params)
            return self.db.execute("UPDATE jobs SET status='cancelled',result='任务已取消，尚未提交。' WHERE status IN ('queued','dispatching')"+scope, params).rowcount

    def route(self, message_id):
        with self.lock:
            row = self.db.execute('SELECT route FROM message_routes WHERE message_id=?', (message_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def queue_message(self, method, payload, route=None, event_key=None, *, progress_key=None, upload=None):
        with self.lock, self.db:
            row = None
            if progress_key is not None and method == 'editMessageText':
                row = self.db.execute("SELECT id FROM outbox WHERE progress_key=? AND status IN ('pending','retry') ORDER BY id LIMIT 1",
                                      (progress_key,)).fetchone()
            if row:
                identifier = row['id']
                # Explicit 429 retries keep their deadline and attempt history.
                # In-flight and ambiguous deliveries are never rewritten.
                self.db.execute('UPDATE outbox SET payload=?,route=? WHERE id=?',
                                (json.dumps(payload), json.dumps(route), identifier))
            else:
                cursor = self.db.execute('INSERT OR IGNORE INTO outbox(event_key,method,payload,route,progress_key) VALUES (?,?,?,?,?)',
                    (event_key, method, json.dumps(payload), json.dumps(route or {'origin': 'system'}), progress_key))
                identifier = cursor.lastrowid if cursor.rowcount else self.db.execute(
                    'SELECT id FROM outbox WHERE event_key=?', (event_key,)).fetchone()[0]
                if upload is not None and cursor.rowcount:
                    self.db.execute('INSERT INTO outbox_uploads VALUES (?,?,?,?)',
                                    (identifier, upload['filename'], upload['content_type'], upload['content']))
            if (route or {}).get('_feedback'):
                self.db.execute('INSERT OR IGNORE INTO feedback_outbox SELECT ?,? WHERE EXISTS (SELECT 1 FROM feedback WHERE id=?)',
                                (route['_feedback'],identifier,route['_feedback']))
        self.outbox_event.set()
        return identifier

    def message_upload(self, identifier):
        with self.lock:
            row = self.db.execute('SELECT filename,content_type,content FROM outbox_uploads WHERE outbox=?', (identifier,)).fetchone()
            return dict(row) if row else None

    def delivery_wait(self, maximum=2):
        with self.lock:
            due = self.db.execute("SELECT MIN(due) FROM outbox WHERE status IN ('pending','retry')").fetchone()[0]
            if due is None:
                return maximum
            return min(maximum, max(0, max(due, self.get('telegram_retry_at', 0))-time.time()))

    def start_feedback(self, identifier, message):
        with self.lock, self.db:
            self.db.execute('DELETE FROM feedback_outbox WHERE feedback IN (SELECT id FROM feedback WHERE message=? AND id!=?)',(message,identifier))
            self.db.execute('DELETE FROM feedback WHERE message=? AND id!=?',(message,identifier))
            self.db.execute('INSERT OR IGNORE INTO feedback(id,message,created) VALUES (?,?,?)',
                            (identifier,message,time.time()))

    def update_feedback(self, identifier, *, ready=None, outcome=None, binding=None):
        with self.lock, self.db:
            for key,value in [('ready',ready),('outcome',outcome),('binding',json.dumps(binding) if binding is not None else None)]:
                if value is not None:
                    self.db.execute('UPDATE feedback SET '+key+'=? WHERE id=?',(value,identifier))

    def feedback_records(self, identifier=None):
        with self.lock:
            rows=self.db.execute('SELECT * FROM feedback'+(' WHERE id=?' if identifier else ''),
                                 (identifier,) if identifier else ()).fetchall()
            return [{**dict(row),'binding':json.loads(row['binding'])} for row in rows]

    def feedback_delivery(self, identifier):
        with self.lock:
            statuses={r[0] for r in self.db.execute('SELECT o.status FROM outbox o JOIN feedback_outbox f ON o.id=f.outbox WHERE f.feedback=?',(identifier,))}
            for status in ('unknown','failed','expired','sending','retry','pending'):
                if status in statuses:return status
            return 'sent' if statuses else 'missing'

    def finish_feedback(self, identifier):
        with self.lock, self.db:
            self.db.execute('DELETE FROM feedback WHERE id=?',(identifier,))
            self.db.execute('DELETE FROM feedback_outbox WHERE feedback=?',(identifier,))

    def take_message(self, identifier=None):
        with self.lock, self.db:
            query = "SELECT * FROM outbox WHERE status IN ('pending','retry') AND due<=?"
            params = [time.time()]
            if identifier is not None:
                query += ' AND id=?'; params.append(identifier)
            # Progress is replaceable presentation. Preserve FIFO among all
            # other outputs, so old progress cannot hold up a question/final.
            row = self.db.execute(query+' ORDER BY (progress_key IS NOT NULL),id LIMIT 1', params).fetchone()
            if not row:
                return None
            self.db.execute("UPDATE outbox SET status='sending',attempts=attempts+1 WHERE id=?", (row['id'],))
            return {**dict(row), 'payload': json.loads(row['payload']), 'route': json.loads(row['route'])}

    def message_done(self, identifier, status, *, message_id=None, due=0, error=None):
        with self.lock, self.db:
            self.db.execute('UPDATE outbox SET status=?,message_id=?,due=?,error=? WHERE id=?',
                            (status, message_id, due, error, identifier))
            if status in {'sent', 'failed', 'expired', 'unknown'}:
                # Unknown is never replayed; its outcome record survives, but
                # no attachment bytes are needed for an automatic retry.
                self.db.execute('DELETE FROM outbox_uploads WHERE outbox=?', (identifier,))
            if message_id is not None:
                self.db.execute('INSERT OR REPLACE INTO message_routes SELECT ?,route FROM outbox WHERE id=?', (message_id, identifier))
            row = self.db.execute('SELECT route FROM outbox WHERE id=?', (identifier,)).fetchone()
            route = json.loads(row[0])
            if route.get('job') is not None and route.get('delivery_key'):
                self.db.execute('UPDATE jobs SET delivery=? WHERE update_id=?',
                    (self.delivery_for(route['delivery_key']), route['job']))

    def message_status(self, identifier):
        with self.lock:
            return self.db.execute('SELECT status FROM outbox WHERE id=?', (identifier,)).fetchone()[0]

    def delivery_counts(self):
        with self.lock:
            return dict(self.db.execute('SELECT status,count(*) FROM outbox GROUP BY status'))

    def delivery_for(self, event_key):
        with self.lock:
            rows = self.db.execute('SELECT status FROM outbox WHERE event_key=? OR substr(event_key,1,?)=?',
                (event_key, len(event_key)+1, event_key+':')).fetchall()
            states = {r[0] for r in rows}
            for status in ('unknown', 'failed', 'expired', 'sending', 'retry', 'pending'):
                if status in states:
                    return status
            return 'sent' if states else 'missing'

    def save_telemetry(self, conversation, **changes):
        with self.lock, self.db:
            value = self.read_telemetry(conversation)
            value.update(changes, updated_at=time.time())
            self.db.execute('INSERT OR REPLACE INTO telemetry VALUES (?,?)', (conversation, json.dumps(value)))

    def read_telemetry(self, conversation):
        with self.lock:
            row = self.db.execute('SELECT snapshot FROM telemetry WHERE conversation=?', (conversation,)).fetchone()
            return json.loads(row[0]) if row else {}

    def desktop_conversation(self, thread_id, cwd, title):
        identifier = 'd_'+hashlib.sha256(thread_id.encode()).hexdigest()[:10]
        with self.lock, self.db:
            self.db.execute("INSERT INTO conversations(id,thread_id,cwd,title,origin) VALUES (?,?,?,?,'desktop') "
                'ON CONFLICT(id) DO UPDATE SET cwd=excluded.cwd,title=excluded.title', (identifier, thread_id, cwd, title[:100]))
        return self.conversation(identifier)

    def owns_thread(self, thread_id):
        with self.lock:
            return self.db.execute("SELECT 1 FROM conversations WHERE thread_id=? AND origin='app-server'", (thread_id,)).fetchone() is not None

    def receive_hook(self, event_key, payload):
        with self.lock, self.db:
            return self.db.execute('INSERT OR IGNORE INTO hook_events VALUES (?,?,?)',
                (event_key, json.dumps(payload), time.time())).rowcount == 1

    def pending_hooks(self):
        with self.lock:
            return [(r[0], json.loads(r[1])) for r in self.db.execute('SELECT event_key,payload FROM hook_events ORDER BY created')]

    def pending_results(self):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM jobs WHERE result IS NOT NULL AND turn_id IS NOT NULL "
                "AND delivery IN ('pending','retry','sending') AND status NOT IN ('running','submitting','uncertain')")]

    def hook_handled(self, event_key):
        with self.lock, self.db:
            self.db.execute('DELETE FROM hook_events WHERE event_key=?', (event_key,))

    def close(self):
        self.db.close()
