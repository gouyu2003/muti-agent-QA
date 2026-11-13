"""Conversation persistence in the runtime SQLite store; Runs own execution state."""
from copy import deepcopy
import json
from uuid import uuid4

from src.observability import utcnow
from src.runtime.chat_context import POLICY_VERSION, bounded_history, context_config
from src.runtime.chat_messages import message_to_dict
from src.runtime.interactive import ExecutionFailure, request_fingerprint
from src.runtime.models import RunContext
from src.runtime.version import code_version


def sync_chat_message(db, record):
    """Called in the Run terminal transaction; never creates/migrates archive tables."""
    if not record.context.config_snapshot.get('_chat'):
        return
    state = record.final_state or {}
    now = utcnow()
    db.execute('UPDATE messages SET status=?,content=?,context_json=?,result_json=?,error=?,finished_at=? '
               'WHERE run_id=? AND role=\'assistant\'',
               (record.status, state.get('answer', ''), json.dumps(state.get('chat_context', {}), ensure_ascii=False),
                json.dumps(state, ensure_ascii=False), record.error.message if record.error else None,
                now, record.context.run_id))
    db.execute('UPDATE messages SET status=? WHERE run_id=? AND role=\'user\'', (record.status, record.context.run_id))
    db.execute('UPDATE conversations SET updated_at=? WHERE conversation_id=(SELECT conversation_id FROM messages '
               'WHERE run_id=? LIMIT 1)', (now, record.context.run_id))


class ChatStore:
    def __init__(self, repository, interactive, systems):
        self.repository, self.interactive, self.systems = repository, interactive, systems

    def initialize(self):
        with self.repository._connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                    request_hash TEXT NOT NULL, title TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL, context_config_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL,
                    turn_index INTEGER NOT NULL, sequence INTEGER NOT NULL,
                    request_key TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES runs(run_id),
                    status TEXT NOT NULL, context_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}', error TEXT, created_at TEXT NOT NULL, finished_at TEXT,
                    UNIQUE(conversation_id,sequence), UNIQUE(conversation_id,request_key,role));
                CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_messages_run ON messages(run_id);
            ''')

    def _row(self, db, conversation_id):
        row = db.execute('SELECT * FROM conversations WHERE conversation_id=?', (conversation_id,)).fetchone()
        if row is None:
            raise ExecutionFailure('会话不存在或已删除。', 404)
        return row

    def _document(self, db, row, full=True):
        active = db.execute("SELECT run_id FROM messages WHERE conversation_id=? AND role='assistant' AND status='running'",
                            (row['conversation_id'],)).fetchone()
        last = db.execute("SELECT content FROM messages WHERE conversation_id=? AND role='user' ORDER BY sequence DESC LIMIT 1",
                          (row['conversation_id'],)).fetchone()
        snapshot = json.loads(row['snapshot_json'])
        result = {key: row[key] for key in ('conversation_id', 'title', 'created_at', 'updated_at')}
        result.update(system_ref=snapshot['system_ref'], active_run_id=active['run_id'] if active else None,
                      preview=last['content'][:80] if last else '')
        if full:
            result.update(config_snapshot=snapshot, context_config=json.loads(row['context_config_json']),
                          chat_policy_version=POLICY_VERSION)
        return result

    def create(self, selection, request_key):
        digest = request_fingerprint(selection)
        with self.repository._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM conversations WHERE request_key=?', (request_key,)).fetchone()
            if row:
                if row['request_hash'] != digest:
                    raise ExecutionFailure('同一创建编号对应的系统选择已改变。')
                return self._document(db, row)
            snapshot = self.systems.run_snapshot(selection['system_id'], selection.get('system_version'), selection.get('draft_revision'))
            config = context_config(snapshot)
            key, now = uuid4().hex, utcnow()
            db.execute('INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?)',
                       (key, request_key, digest, '新聊天', json.dumps(snapshot, ensure_ascii=False),
                        json.dumps(config, ensure_ascii=False), now, now))
            return self._document(db, self._row(db, key))

    def list(self):
        self.interactive.reconcile_all()
        with self.repository._connection() as db:
            return [self._document(db, row, False) for row in db.execute('SELECT * FROM conversations ORDER BY updated_at DESC')]

    def get(self, key):
        self.interactive.reconcile_all()
        with self.repository._connection() as db:
            return self._document(db, self._row(db, key))

    def messages(self, key):
        with self.repository._connection() as db:
            self._row(db, key)
            rows = db.execute('SELECT * FROM messages WHERE conversation_id=? ORDER BY sequence', (key,)).fetchall()
        return [message_to_dict({**{k: row[k] for k in ('message_id', 'conversation_id', 'role', 'content', 'turn_index',
                    'sequence', 'run_id', 'status', 'error', 'created_at', 'finished_at')},
                'context': json.loads(row['context_json']), 'result': json.loads(row['result_json'])}, full=True) for row in rows]

    def rename(self, key, title):
        with self.repository._connection() as db:
            self._row(db, key)
            db.execute('UPDATE conversations SET title=?,updated_at=? WHERE conversation_id=?', (title, utcnow(), key))
            return self._document(db, self._row(db, key))

    def delete(self, key):
        self.interactive.reconcile_all()
        with self.repository._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._row(db, key)
            if db.execute("SELECT 1 FROM messages m JOIN runs r USING(run_id) WHERE m.conversation_id=? AND r.status='running'", (key,)).fetchone():
                raise ExecutionFailure('请先停止当前会话的运行，再删除会话。')
            db.execute('DELETE FROM conversations WHERE conversation_id=?', (key,))

    def send(self, key, question, request_key):
        conversation = self.get(key)
        snapshot = deepcopy(conversation['config_snapshot'])
        workflow = snapshot['workflow_spec']
        models = {a['model_config']['model_name'] for a in snapshot['agents'].values() if a.get('model_config', {}).get('model_name')}
        context = RunContext(run_id=uuid4().hex, entrypoint='api', question=question,
            source_id=snapshot.get('source', {}).get('source_id'), record_id=snapshot.get('dataset', {}).get('record_id'),
            workflow_id=workflow['protocol_id'], method_id=workflow['method_id'], protocol_id=workflow['protocol_id'],
            model_name=next(iter(models)) if len(models) == 1 else 'multiple', code_version=code_version(), config_snapshot=snapshot)
        fingerprint = request_fingerprint({'conversation_id': key, 'question': question})

        def prepare(current):
            with self.repository._connection() as db:
                self._row(db, key)
            history = self.messages(key)
            current.config_snapshot['_chat'] = {'conversation_id': key,
                'turn_index': max((m['turn_index'] for m in history), default=0) + 1,
                'history': bounded_history(history),
                'has_history': any(m['role'] == 'assistant' and m['status'] == 'completed' and m['content'] for m in history),
                'config': context_config(snapshot)}
            return current

        def reserved(current):
            turn = current.config_snapshot['_chat']['turn_index']
            now = utcnow()
            with self.repository._connection() as db:
                for offset, role, content in ((0, 'user', question), (1, 'assistant', '')):
                    db.execute('INSERT INTO messages (message_id,conversation_id,role,content,turn_index,sequence,request_key,run_id,status,created_at) '
                               'VALUES (?,?,?,?,?,?,?,?,?,?)',
                               (uuid4().hex, key, role, content, turn, turn * 2 + offset, request_key, current.run_id, 'running', now))
                if turn == 1:
                    db.execute('UPDATE conversations SET title=?,updated_at=? WHERE conversation_id=? AND title=\'新聊天\'',
                               (question[:32], now, key))
                db.execute('UPDATE conversations SET updated_at=? WHERE conversation_id=?', (now, key))

        submitted = self.interactive.submit(context, 'chat:' + key + ':' + request_key, fingerprint,
                                            prepare=prepare, on_reserved=reserved)
        messages = self.messages(key)
        pair = [m for m in messages if m['run_id'] == submitted['run_id']]
        return {**submitted, 'conversation_id': key,
                'user_message_id': pair[0]['message_id'], 'assistant_message_id': pair[1]['message_id']}
