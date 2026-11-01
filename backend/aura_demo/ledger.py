"""Admission and provider reservations survive reloads, failures and restarts."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid

from aura_ops.models import OpsError


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class DemoLedger:
    def __init__(self, settings):
        self.settings = settings
        settings.data.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(settings.data / "demo.sqlite", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS login_attempts(at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, session TEXT NOT NULL,
              request_key TEXT NOT NULL, created REAL NOT NULL, finished REAL,
              calls INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
              report TEXT NOT NULL, UNIQUE(session, request_key));
        """)
        self.db.execute("INSERT OR IGNORE INTO settings VALUES('owner', ?)", (secrets.token_hex(16),))
        self.owner = self.db.execute("SELECT value FROM settings WHERE key='owner'").fetchone()[0]

    def close(self):
        self.db.close()

    def login_attempt(self):
        now = time.time()
        self.db.execute("DELETE FROM login_attempts WHERE at < ?", (now - 60,))
        if self.db.execute("SELECT count(*) FROM login_attempts").fetchone()[0] >= 10:
            raise OpsError("login_rate_limit", "Too many attempts. Wait a minute.", 429)
        self.db.execute("INSERT INTO login_attempts VALUES(?)", (now,))

    def new_session(self):
        now = time.time()
        self.db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        if self.db.execute("SELECT count(*) FROM sessions").fetchone()[0] >= 100:
            raise OpsError("session_limit", "Demo access is temporarily full.", 429)
        token = secrets.token_urlsafe(32)
        self.db.execute("INSERT INTO sessions VALUES(?, ?)", (digest(token), now + 43200))
        return token

    def session(self, token):
        if not token or len(token) > 128:
            return None
        hashed = digest(token)
        row = self.db.execute("SELECT expires FROM sessions WHERE id=?", (hashed,)).fetchone()
        return hashed if row and row[0] > time.time() else None

    def logout(self, token):
        self.db.execute("DELETE FROM sessions WHERE id=?", (digest(token or ""),))

    def row(self, run_id):
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def latest(self, session):
        row = self.db.execute("SELECT * FROM runs WHERE session=? ORDER BY created DESC LIMIT 1", (session,)).fetchone()
        return dict(row) if row else None

    def active(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM runs WHERE active=1")]

    def reserve(self, session, request_key):
        try:
            if str(uuid.UUID(request_key)) != request_key:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise OpsError("invalid_request", "A valid request identifier is required.", 400) from None
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute("SELECT id FROM runs WHERE session=? AND request_key=?", (session, request_key)).fetchone()
            if existing:
                self.db.execute("COMMIT")
                return existing[0], False
            if self.active():
                raise OpsError("demo_busy", "Another recovery is running. Please try again shortly.", 409)
            count, calls, last = self.db.execute("SELECT count(*), coalesce(sum(calls),0), max(finished) FROM runs").fetchone()
            if count >= self.settings.max_runs or calls + self.settings.calls_per_run > self.settings.max_calls:
                raise OpsError("demo_limit", "The demonstration has reached its usage limit.", 429)
            if last and time.time() - last < self.settings.cooldown:
                raise OpsError("demo_cooldown", "The demo is resting between runs. Try again in a minute.", 429)
            if self.db.execute("SELECT count(*) FROM runs WHERE session=?", (session,)).fetchone()[0] >= self.settings.session_runs:
                raise OpsError("session_limit", "You have used the runs available for this visit.", 429)
            run_id = uuid.uuid4().hex
            report = {"run_id": run_id, "phase": "preparing", "verified": False,
                      "model_mode": "live", "events": [], "samples": [], "calls": []}
            self.db.execute("INSERT INTO runs(id,session,request_key,created,report) VALUES(?,?,?,?,?)",
                            (run_id, session, request_key, time.time(), json.dumps(report)))
            self.db.execute("COMMIT")
            return run_id, True
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def reserve_call(self, run_id):
        # No await inside the transaction; parallel specialist reservations serialize.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.row(run_id)
            total = self.db.execute("SELECT coalesce(sum(calls),0) FROM runs").fetchone()[0]
            if (not row or not row["active"] or row["calls"] >= self.settings.calls_per_run
                    or total >= self.settings.max_calls):
                raise OpsError("demo_call_limit", "The model request limit was reached.", 429)
            self.db.execute("UPDATE runs SET calls=calls+1 WHERE id=?", (run_id,))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def save(self, run_id, report, *, finished=False, blocked=False):
        text = json.dumps(report, allow_nan=False)
        if len(text) > 500_000:
            raise ValueError("Demo report exceeded its bound")
        self.db.execute("UPDATE runs SET report=?, active=?, finished=? WHERE id=?",
                        (text, int(not finished or blocked), time.time() if finished else None, run_id))
