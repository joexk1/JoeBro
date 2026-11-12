"""Durable phone-chat delivery, lock-screen replies, and scheduled-run leases.

Lock-screen reply: the phone posts a reply typed on a notification to
/api/joebro/reply; it is queued in the database (idempotent on request_id), run
as a normal phone-agent turn by personal_reply_loop, and the answer is pushed
back. A partly-executed turn is never replayed.

Scheduled-run leases: the task scheduler claims a (task, day@time) slot in the
database before it starts a run, so a slow task or a restart can neither skip
nor double-fire a slot, and a run that failed before doing anything is retried.
"""
import hashlib
import json
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from jb_core import PHONE_SESSION_ID

PERSONAL_SESSION = PHONE_SESSION_ID
PERSONAL_CHAT_LOCK = threading.Lock()
TASK_LOCKS = {}
TASK_LOCKS_GUARD = threading.Lock()

def task_lock(task_id):
    with TASK_LOCKS_GUARD:
        return TASK_LOCKS.setdefault(task_id, threading.Lock())


def ensure_tables(store):
    store.exec('''CREATE TABLE IF NOT EXISTS personal_reply_jobs(
        id TEXT PRIMARY KEY, message TEXT NOT NULL, reply_to INTEGER,
        status TEXT NOT NULL DEFAULT 'pending', error TEXT DEFAULT '',
        created_at TEXT NOT NULL, finished_at TEXT DEFAULT '')''')
    store.exec('''CREATE TABLE IF NOT EXISTS scheduled_runs(
        task_id TEXT NOT NULL, slot TEXT NOT NULL, status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, started_at TEXT DEFAULT '',
        finished_at TEXT DEFAULT '', retry_at TEXT DEFAULT '', error TEXT DEFAULT '',
        PRIMARY KEY(task_id,slot))''')


def personal_context(handler, form):
    sid = (form or {}).get('session') or ''
    return sid == PERSONAL_SESSION or (sid.startswith('task_session_') and
        handler.ptask_is_personal(sid[len('task_session_'):]))


def claim_scheduled_run(store, row, now):
    """Claim a calendar slot atomically. Retries never rewrite task configuration."""
    ensure_tables(store)
    slot = now.strftime('%Y-%m-%d') + '@' + (row.get('scheduled_time') or '')
    stamp = now.astimezone(timezone.utc).isoformat()
    with store.lock, store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        run = db.execute('SELECT * FROM scheduled_runs WHERE task_id=? AND slot=?', (row['id'],slot)).fetchone()
        if run and (run['status'] != 'retry' or run['attempts'] >= 3 or run['retry_at'] > stamp):
            return None
        attempt = (run['attempts'] if run else 0) + 1
        db.execute('''INSERT INTO scheduled_runs(task_id,slot,status,attempts,started_at)
            VALUES(?,?,'running',?,?) ON CONFLICT(task_id,slot) DO UPDATE SET
            status='running',attempts=excluded.attempts,started_at=excluded.started_at,error='' ''',
            (row['id'],slot,attempt,stamp))
        db.commit()
    return slot


def finish_scheduled_run(store, task_id, slot, error='', retry_safe=False):
    now = datetime.now(timezone.utc)
    run = store.one('SELECT attempts FROM scheduled_runs WHERE task_id=? AND slot=?',(task_id,slot)) or {}
    retry = bool(error and retry_safe and run.get('attempts',3) < 3)
    store.exec('''UPDATE scheduled_runs SET status=?,finished_at=?,retry_at=?,error=?
        WHERE task_id=? AND slot=?''',
        ('retry' if retry else 'failed' if error else 'done', now.isoformat(),
         (now+timedelta(minutes=2*run.get('attempts',1))).isoformat() if retry else '',
         str(error)[:700],task_id,slot))


class PersonalUpdatesMixin:
    def enqueue_personal_reply(self, body):
        ensure_tables(self.store)
        rid = str(body.get('request_id') or '').strip()
        message = str(body.get('message') or '').strip()
        if not rid or len(rid)>100 or not message or len(message)>4000:
            raise ValueError('request_id and a reply of 1–4000 characters are required')
        parent = body.get('message_id')
        if parent is not None:
            try: parent = int(parent)
            except (TypeError,ValueError): raise ValueError('invalid message_id')
            original = self.store.one('SELECT session_id,role FROM messages WHERE id=?', (parent,))
            if original and (original['session_id'] != PERSONAL_SESSION or original['role'] != 'assistant'):
                raise ValueError('the reply must target a message from the phone agent')
        from jb_core import now_iso
        with self.store.lock, self.store.db() as db:
            old = db.execute('SELECT message,reply_to FROM personal_reply_jobs WHERE id=?',(rid,)).fetchone()
            if old and (old['message'] != message or old['reply_to'] != parent):
                raise ValueError('request_id was already used for a different reply')
            db.execute('''INSERT OR IGNORE INTO personal_reply_jobs(id,message,reply_to,created_at)
                VALUES(?,?,?,?)''',(rid,message,parent,now_iso()))
            db.commit()
        return {'ok':True,'accepted':True,'request_id':rid}

    def process_personal_reply(self):
        ensure_tables(self.store)
        if not PERSONAL_CHAT_LOCK.acquire(blocking=False):
            return False
        from jb_chat import _ACTIVE_TURNS, _ACTIVE_TURNS_LOCK
        from jb_core import now_iso
        try:
            job = self.store.one("SELECT * FROM personal_reply_jobs WHERE status='pending' ORDER BY created_at LIMIT 1")
            if not job: return False
            self.store.exec("UPDATE personal_reply_jobs SET status='running' WHERE id=?",(job['id'],))
            f = self.phone_turn({'session':PERSONAL_SESSION,'message':job['message'],
                                      '_user_already_saved':True, '_personal_reply_to':job['reply_to']})
            self.add_message(PERSONAL_SESSION,'user',job['message'],
                             {'source':'lock_screen_reply','reply_to':job['reply_to'],'request_id':job['id']})
            with _ACTIVE_TURNS_LOCK: _ACTIVE_TURNS.add(PERSONAL_SESSION)
            aid = self.chat_turn_open(PERSONAL_SESSION,f)
            try:
                reply, _ = self.bot_run(f)
                self.chat_turn_close(aid,'done','')
                self.store.exec("UPDATE personal_reply_jobs SET status='done',finished_at=? WHERE id=?",(now_iso(),job['id']))
                last = self.store.one("SELECT id FROM messages WHERE session_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",(PERSONAL_SESSION,))
                if reply.strip():
                    self.notify(reply,title=self.phone_agent_name(),kind='personal_reply',source=job['id'],
                                channel='mobile',session=PERSONAL_SESSION,message_id=(last or {}).get('id'))
            except Exception as exc:
                self.chat_turn_close(aid,'failed',str(exc))
                # Do not replay a partly-executed turn: a tool may already have acted.
                self.store.exec("UPDATE personal_reply_jobs SET status='failed',error=?,finished_at=? WHERE id=?",(str(exc)[:700],now_iso(),job['id']))
                self.notify('I saved your reply, but could not finish responding. Open our chat to continue.',
                            title=self.phone_agent_name(),kind='personal_reply',source=job['id'],channel='mobile',session=PERSONAL_SESSION)
            finally:
                with _ACTIVE_TURNS_LOCK: _ACTIVE_TURNS.discard(PERSONAL_SESSION)
            return True
        finally:
            PERSONAL_CHAT_LOCK.release()


def personal_reply_loop(server):
    from joebro_backend import Handler
    import types
    h=Handler.__new__(Handler);h.server=types.SimpleNamespace(store=server.store)
    ensure_tables(server.store)
    while True:
        try: h.process_personal_reply()
        except Exception: traceback.print_exc()
        time.sleep(2)
