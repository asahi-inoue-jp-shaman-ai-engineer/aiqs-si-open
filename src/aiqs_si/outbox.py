"""Worker-local durable results awaiting queue acknowledgement. No job replay."""
import json
import sqlite3
from contextlib import contextmanager


class CompletionOutbox:
    def __init__(self, path):
        self.path = str(path)
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS completions (job TEXT PRIMARY KEY, state TEXT NOT NULL, result TEXT NOT NULL)")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute('PRAGMA secure_delete=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, job, state, result):
        encoded = json.dumps(result, sort_keys=True, allow_nan=False)
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO completions VALUES (?,?,?)", (job, state, encoded))
            existing = db.execute("SELECT state,result FROM completions WHERE job=?", (job,)).fetchone()
            if existing != (state, encoded):
                raise ValueError("Conflicting worker completion")

    def pending(self):
        with self.connection() as db:
            rows = db.execute("SELECT job,state,result FROM completions ORDER BY rowid").fetchall()
        return [(job, state, json.loads(result)) for job, state, result in rows]

    def acknowledge(self, job):
        with self.connection() as db:
            db.execute("DELETE FROM completions WHERE job=?", (job,))
