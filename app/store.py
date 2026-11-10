import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid

TERMINAL = ("succeeded", "partial", "failed", "cancelled", "unknown")


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def payload_hash(body):
    canon = {
        "question_id": body.get("question_id"),
        "prompt": body.get("prompt"),
        "platform": body.get("platform"),
        "mode": body.get("mode"),
        "retry_limit": body.get("retry_limit", 1),
        "context": body.get("context") or {},
    }
    blob = json.dumps(canon, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class Store:
    def __init__(self, db_path):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        self.recover_on_boot()

    def _init_schema(self):
        c = self._conn
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                idem_key TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                question_id TEXT,
                prompt TEXT NOT NULL,
                platform TEXT NOT NULL,
                mode TEXT NOT NULL,
                retry_limit INTEGER NOT NULL DEFAULT 1,
                context_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'queued',
                error_code TEXT,
                error_message TEXT,
                response_kind TEXT,
                result_json TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                cancel_requested_at TEXT,
                attempts_used INTEGER NOT NULL DEFAULT 0,
                UNIQUE(idem_key)
            );
            CREATE TABLE IF NOT EXISTS attempts (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                slot_id TEXT,
                prepared_at TEXT,
                submitted_at TEXT,
                submission_confirmed INTEGER NOT NULL DEFAULT 0,
                finished_at TEXT,
                outcome TEXT,
                error_code TEXT,
                error_message TEXT,
                duration_ms INTEGER,
                conversation_url TEXT,
                evidence_json TEXT
            );
            CREATE TABLE IF NOT EXISTS assets (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                attempt_id TEXT,
                kind TEXT NOT NULL,
                seq INTEGER NOT NULL DEFAULT 0,
                coverage TEXT,
                sha256 TEXT,
                path TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        c.commit()

    def recover_on_boot(self):
        """Restart recovery: never blindly requeue a running task.

        A task that was running when the service stopped has an unknowable
        upstream state (the question may or may not have been answered in the
        original session), so it becomes `unknown` unless a cancel was already
        requested before any submission was confirmed.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, cancel_requested_at FROM tasks WHERE status IN ('running','cancel_requested')"
            ).fetchall()
            for r in rows:
                att = self._conn.execute(
                    "SELECT submission_confirmed FROM attempts WHERE task_id=? ORDER BY attempt_no DESC LIMIT 1",
                    (r["id"],),
                ).fetchone()
                submitted = bool(att and att["submission_confirmed"])
                if r["cancel_requested_at"] and not submitted:
                    self._conn.execute(
                        "UPDATE tasks SET status='cancelled', finished_at=? WHERE id=?",
                        (now_iso(), r["id"]),
                    )
                else:
                    self._conn.execute(
                        "UPDATE tasks SET status='unknown', error_code='SERVICE_RESTART_UNVERIFIED', "
                        "error_message='服务重启时任务正在运行，原会话结果未经核对，不自动重发', finished_at=? WHERE id=?",
                        (now_iso(), r["id"]),
                    )
            self._conn.commit()

    # ---------- tasks ----------
    def create_or_reuse(self, idem_key, phash, body):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE idem_key=?", (idem_key,)
            ).fetchone()
            if row:
                if row["payload_hash"] != phash:
                    return None, "conflict"
                return dict(row), "reused"
            if self.count_queued() >= 10**9:  # queue limit enforced by caller
                pass
            tid = uuid.uuid4().hex
            self._conn.execute(
                "INSERT INTO tasks (id, idem_key, payload_hash, question_id, prompt, platform, mode, retry_limit, context_json, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    tid,
                    idem_key,
                    phash,
                    body.get("question_id"),
                    body["prompt"],
                    body["platform"],
                    body["mode"],
                    int(body.get("retry_limit", 1)),
                    json.dumps(body.get("context") or {}, ensure_ascii=False),
                    "queued",
                    now_iso(),
                ),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            return dict(row), "created"

    def count_queued(self):
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE status='queued'"
            ).fetchone()
            return r["n"]

    def get_task(self, tid):
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            return dict(row) if row else None

    def claim_next(self, slot_id):
        """Atomically claim the oldest queued task for a slot."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE status='queued' ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            self._conn.execute(
                "UPDATE tasks SET status='running', started_at=COALESCE(started_at, ?) WHERE id=? AND status='queued'",
                (now_iso(), row["id"]),
            )
            if self._conn.total_changes == 0:
                return None
            self._conn.commit()
            return self.get_task(row["id"])

    def update_task(self, tid, **fields):
        if not fields:
            return
        with self._lock:
            cols = ", ".join(f"{k}=?" for k in fields)
            self._conn.execute(
                f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), tid)
            )
            self._conn.commit()

    def request_cancel(self, tid):
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if not row:
                return None
            if row["status"] in TERMINAL:
                return dict(row)
            if row["status"] == "queued":
                self._conn.execute(
                    "UPDATE tasks SET status='cancelled', cancel_requested_at=?, finished_at=? WHERE id=?",
                    (now_iso(), now_iso(), tid),
                )
            else:  # running
                self._conn.execute(
                    "UPDATE tasks SET status='cancel_requested', cancel_requested_at=? WHERE id=?",
                    (now_iso(), tid),
                )
            self._conn.commit()
            return self.get_task(tid)

    # ---------- attempts ----------
    def start_attempt(self, task_id, attempt_no, slot_id):
        aid = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                "INSERT INTO attempts (id, task_id, attempt_no, slot_id, prepared_at) VALUES (?,?,?,?,?)",
                (aid, task_id, attempt_no, slot_id, now_iso()),
            )
            self._conn.execute(
                "UPDATE tasks SET attempts_used=? WHERE id=?", (attempt_no, task_id)
            )
            self._conn.commit()
        return aid

    def finish_attempt(self, aid, **fields):
        if not fields:
            return
        with self._lock:
            cols = ", ".join(f"{k}=?" for k in fields)
            self._conn.execute(
                f"UPDATE attempts SET {cols} WHERE id=?", (*fields.values(), aid)
            )
            self._conn.commit()

    def list_attempts(self, task_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM attempts WHERE task_id=? ORDER BY attempt_no ASC", (task_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- assets ----------
    def add_asset(self, task_id, attempt_id, kind, seq, coverage, sha256, path):
        aid = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                "INSERT INTO assets (id, task_id, attempt_id, kind, seq, coverage, sha256, path, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (aid, task_id, attempt_id, kind, seq, coverage, sha256, path, now_iso()),
            )
            self._conn.commit()
        return aid

    def get_asset(self, task_id, asset_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM assets WHERE task_id=? AND id=?", (task_id, asset_id)
            ).fetchone()
            return dict(row) if row else None

    def list_assets(self, task_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM assets WHERE task_id=? ORDER BY seq ASC", (task_id,)
            ).fetchall()
            return [dict(r) for r in rows]
