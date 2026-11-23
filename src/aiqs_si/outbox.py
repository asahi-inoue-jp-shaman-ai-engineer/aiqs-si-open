"""Worker-local durable results awaiting queue acknowledgement. No job replay."""
import json
import sqlite3
from contextlib import contextmanager


class CompletionOutbox:
    def __init__(self, path):
        self.path = str(path)
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS completions (job TEXT PRIMARY KEY, state TEXT NOT NULL, result TEXT NOT NULL)")
            # Claim metadata rides along with the durable result
            # (batch 2b item 2): after a restart the replay presents
            # the ORIGINAL claim generation, so the server can
            # authenticate the acknowledgement's identity instead of
            # depending on the generation checks being omitted.
            columns = [row[1] for row in db.execute("PRAGMA table_info(completions)")]
            if "claim_generation" not in columns:
                db.execute("ALTER TABLE completions ADD COLUMN claim_generation INTEGER")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute('PRAGMA secure_delete=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, job, state, result, *, claim_generation=None):
        encoded = json.dumps(result, sort_keys=True, allow_nan=False)
        if claim_generation is not None and type(claim_generation) is not int:
            raise ValueError("Invalid claim generation")
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO completions VALUES (?,?,?,?)",
                       (job, state, encoded, claim_generation))
            existing = db.execute("SELECT state,result FROM completions WHERE job=?", (job,)).fetchone()
            if existing != (state, encoded):
                raise ValueError("Conflicting worker completion")

    def pending(self):
        with self.connection() as db:
            rows = db.execute("SELECT job,state,result,claim_generation FROM completions ORDER BY rowid").fetchall()
        return [(job, state, json.loads(result), claim_generation)
                for job, state, result, claim_generation in rows]

    def acknowledge(self, job):
        with self.connection() as db:
            db.execute("DELETE FROM completions WHERE job=?", (job,))
