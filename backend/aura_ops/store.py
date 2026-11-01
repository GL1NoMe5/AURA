"""Durable operations records and content-verified local artifacts.

Callers supply already-redacted evidence. This module never reads credentials
or target environments. Mutations are transactional across threads and store
instances; it does not execute actions or recover interrupted investigations.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator
import uuid

from .models import OpsError, utc_now
from .redaction import redact_text
from .target_management import connection_revision as _revision_of


MAX_JSON_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
MAX_EVENTS = 10000
MAX_TARGETS = 3
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_EVENT_TYPE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.-]{0,63}$")


def _identifier(value: str, label: str = "id") -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise OpsError("invalid_identifier", f"Invalid {label}.")
    return value


def _json(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":"))
        size = len(text.encode("utf-8"))
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise OpsError("invalid_record", "Record must contain finite JSON data.") from exc
    if size > MAX_JSON_BYTES:
        raise OpsError("record_too_large", "JSON record exceeds 1 MiB.", 413)
    return text


def _object(value: Any) -> dict:
    if not isinstance(value, dict):
        raise OpsError("invalid_record", "An object is required.")
    # Isolate records from mutable caller data and normalize string enums.
    return json.loads(_json(value))


def _limit(value: int, maximum: int = 1000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise OpsError("invalid_limit", "Limit must be a positive integer.")
    return min(value, maximum)


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise OpsError("invalid_record", "Record must contain finite JSON data.") from exc


def proposal_content_hash(payload: dict) -> str:
    """Stable binding to a proposal's content, excluding its write timestamp.

    Any later content change (including an appended verification) yields a
    different hash, so a reconciliation covering the old content no longer
    applies. A free-form `reconciled` flag in a proposal is never trusted.
    """
    if not isinstance(payload, dict):
        raise OpsError("invalid_record", "An object is required.")
    content = {key: value for key, value in payload.items() if key != "updated_at"}
    return hashlib.sha256(_canonical_bytes(content)).hexdigest()


class OpsStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir = self.root / "artifacts"
        self.artifacts_dir.mkdir(exist_ok=True)
        if self.artifacts_dir.is_symlink() or self.artifacts_dir.resolve() != self.artifacts_dir:
            raise OpsError("invalid_artifact_directory", "Artifact directory must stay within the store.")
        self._lock = threading.RLock()
        self._closed = False
        self._db = sqlite3.connect(self.root / "ops.sqlite3", timeout=10,
                                   check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS targets (
                target_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS investigations (
                investigation_id TEXT PRIMARY KEY,
                target_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS investigations_target
                ON investigations(target_id, created_at);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS proposals (
                proposal_id TEXT PRIMARY KEY,
                investigation_id TEXT,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS proposals_investigation
                ON proposals(investigation_id, created_at);
            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS target_tombstones (
                target_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reconciliations (
                review_id TEXT PRIMARY KEY,
                target_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS reconciliations_target
                ON reconciliations(target_id, created_at);
        """)

    def _assert_open(self) -> None:
        if self._closed:
            raise OpsError("store_closed", "Operations store is closed.", 503)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._assert_open()
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    def _get(self, table: str, key: str, value: str) -> dict | None:
        # Table/key arguments are module constants, never caller input.
        with self._lock:
            self._assert_open()
            row = self._db.execute(
                f"SELECT payload FROM {table} WHERE {key} = ?", (value,)).fetchone()
            return json.loads(row["payload"]) if row else None

    # -- internal helpers (caller holds lock / transaction) -----------------
    def _get_setting_locked(self, key: str):
        row = self._db.execute("SELECT payload FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _put_setting_locked(self, key: str, value) -> None:
        self._db.execute("INSERT INTO settings(key,payload) VALUES (?,?) "
                         "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                         (key, _json(value)))

    def _delete_setting_locked(self, key: str) -> None:
        self._db.execute("DELETE FROM settings WHERE key=?", (key,))

    def _tombstone_locked(self, target_id: str) -> dict | None:
        row = self._db.execute(
            "SELECT payload FROM target_tombstones WHERE target_id=?", (target_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def get_tombstone(self, target_id: str) -> dict | None:
        _identifier(target_id, "target_id")
        with self._lock:
            self._assert_open()
            return self._tombstone_locked(target_id)

    def is_retired(self, target_id: str) -> bool:
        return self.get_tombstone(target_id) is not None

    def _unknowns_locked(self, target_id: str) -> list[dict]:
        covered = self._covered_unknowns_locked(target_id)
        result = []
        for row in self._db.execute("SELECT payload FROM proposals").fetchall():
            try:
                payload = json.loads(row[0])
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict) and payload.get("target_id") == target_id \
                    and payload.get("status") == "unknown" \
                    and (payload.get("proposal_id"), proposal_content_hash(payload)) not in covered:
                result.append(payload)
        result.sort(key=lambda item: (item.get("created_at") or "", item.get("proposal_id") or ""))
        return result

    def _blockers_locked(self, target_id: str):
        """Return (kind, detail) for durable busy/unknown gates, or None."""
        if self._unknowns_locked(target_id):
            return ("target_reconciliation_required", "A previous operation outcome is unknown; reconcile the target first.")
        phases = {"queued", "investigating", "awaiting_approval", "executing", "verifying",
                  "preparing_patch", "preparing_deployment"}
        inv_phase = {}
        for row in self._db.execute("SELECT payload FROM investigations WHERE target_id=?", (target_id,)):
            try:
                rec = json.loads(row[0])
            except (ValueError, TypeError):
                continue
            inv_phase[rec.get("investigation_id")] = rec.get("phase")
            if rec.get("phase") in phases:
                return ("operation_busy", "Finish or decide the pending operation for this target first.")
        for row in self._db.execute("SELECT payload FROM proposals").fetchall():
            try:
                proposal = json.loads(row[0])
            except (ValueError, TypeError):
                continue
            if proposal.get("target_id") != target_id:
                continue
            if proposal.get("status") in {"executing", "dispatching", "verifying", "rolling_back"}:
                return ("operation_busy", "Finish or decide the pending operation for this target first.")
            if proposal.get("status") == "proposed" \
                    and inv_phase.get(proposal.get("investigation_id")) == "awaiting_approval":
                return ("operation_busy", "Finish or decide the pending operation for this target first.")
        return None

    def _expire_orphans_locked(self, target_id: str, now: str) -> list[str]:
        inv_phase = {}
        for row in self._db.execute("SELECT payload FROM investigations WHERE target_id=?", (target_id,)):
            try:
                rec = json.loads(row[0])
            except (ValueError, TypeError):
                continue
            inv_phase[rec.get("investigation_id")] = rec.get("phase")
        expired = []
        for row in self._db.execute("SELECT payload FROM proposals").fetchall():
            try:
                proposal = json.loads(row[0])
            except (ValueError, TypeError):
                continue
            if proposal.get("target_id") != target_id or proposal.get("status") != "proposed":
                continue
            if inv_phase.get(proposal.get("investigation_id")) == "awaiting_approval":
                continue
            proposal["status"] = "expired"
            proposal["updated_at"] = now
            self._db.execute("UPDATE proposals SET payload=? WHERE proposal_id=?",
                             (_json(proposal), proposal["proposal_id"]))
            expired.append(proposal["proposal_id"])
        return expired

    def _append_event_locked(self, type_: str, payload: dict, target_id=None, investigation_id=None) -> dict:
        event = {"type": type_, "timestamp": utc_now(), "target_id": target_id,
                 "investigation_id": investigation_id, "payload": _object(payload)}
        cursor = self._db.execute("INSERT INTO events(payload) VALUES (?)", (_json(event),))
        event["seq"] = cursor.lastrowid
        self._db.execute("DELETE FROM events WHERE seq <= ?", (event["seq"] - MAX_EVENTS,))
        return event

    @staticmethod
    def _validate_revision_value(value) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise OpsError("target_revision_changed", "Expected revision must be a nonnegative integer.", 409)
        return value

    def list_targets(self) -> list[dict]:
        with self._lock:
            self._assert_open()
            return [json.loads(row[0]) for row in self._db.execute(
                "SELECT payload FROM targets ORDER BY target_id")]

    def put_target(self, spec: dict) -> dict:
        record = _object(spec)
        target_id = _identifier(record.get("target_id"), "target_id")
        with self._transaction():
            existing = self.get_target(target_id)
            if existing is None:
                if self._tombstone_locked(target_id) is not None:
                    raise OpsError("target_id_retired", "This application ID was retired and cannot be re-enrolled.", 409)
                count = self._db.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
                if count >= MAX_TARGETS:
                    raise OpsError("target_limit", "A maximum of three targets is supported.", 409)
            # Preserve server metadata and extension metadata on replacement.
            now = utc_now()
            merged = {**(existing or {}), **record,
                      "created_at": existing["created_at"] if existing else now,
                      "updated_at": now,
                      "connection_revision": _revision_of(existing) if existing else 0}
            encoded = _json(merged)
            self._db.execute("INSERT INTO targets(target_id,payload) VALUES (?,?) "
                             "ON CONFLICT(target_id) DO UPDATE SET payload=excluded.payload",
                             (target_id, encoded))
        return json.loads(encoded)

    def enroll_target(self, spec: dict) -> dict:
        """Create-only enrollment under one transaction."""
        record = _object(spec)
        target_id = _identifier(record.get("target_id"), "target_id")
        with self._transaction():
            if self.get_target(target_id) is not None:
                raise OpsError("target_already_enrolled", "This application ID is already enrolled.", 409)
            if self._tombstone_locked(target_id) is not None:
                raise OpsError("target_id_retired", "This application ID was retired and cannot be re-enrolled.", 409)
            pair = (record.get("docker_context", "default"), record.get("container_name"))
            for row in self._db.execute("SELECT payload FROM targets").fetchall():
                try:
                    other = json.loads(row[0])
                except (ValueError, TypeError):
                    continue
                if (other.get("docker_context", "default"), other.get("container_name")) == pair:
                    raise OpsError("target_already_enrolled",
                                   "This Docker context and container are already enrolled.", 409)
            count = self._db.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
            if count >= MAX_TARGETS:
                raise OpsError("target_limit", "A maximum of three targets is supported.", 409)
            now = utc_now()
            merged = {**record, "created_at": now, "updated_at": now, "connection_revision": 0}
            encoded = _json(merged)
            self._db.execute("INSERT INTO targets(target_id,payload) VALUES (?,?)", (target_id, encoded))
        return json.loads(encoded)

    def cas_replace_target(self, expected_current: dict, new_spec: dict) -> dict:
        """Replace a target spec only if the complete current record matches."""
        target_id = _identifier(expected_current.get("target_id"), "target_id")
        record = _object(new_spec)
        with self._transaction():
            current = self.get_target(target_id)
            if current is None:
                if self._tombstone_locked(target_id) is not None:
                    raise OpsError("target_id_retired", "This application ID was retired and cannot be re-enrolled.", 409)
                raise OpsError("target_not_found", "Target was not found.", 404)
            if current != expected_current:
                raise OpsError("target_revision_changed", "Target changed; reload it and retry.", 409)
            now = utc_now()
            merged = {**current, **record, "created_at": current["created_at"],
                      "updated_at": now, "connection_revision": _revision_of(current)}
            encoded = _json(merged)
            self._db.execute("UPDATE targets SET payload=? WHERE target_id=?", (encoded, target_id))
        return json.loads(encoded)

    def update_target_registration(self, target_id: str, expected_revision, expected_current: dict,
                                   new_spec: dict):
        """Atomic profile update. Returns (record, effective, event)."""
        _identifier(target_id, "target_id")
        expected_revision = self._validate_revision_value(expected_revision)
        record = _object(new_spec)
        with self._transaction():
            current = self.get_target(target_id)
            if current is None:
                raise OpsError("target_not_found", "Application is not enrolled.", 404)
            if _revision_of(current) != expected_revision:
                raise OpsError("target_revision_changed", "Connection revision changed; reload the target and retry.", 409)
            if current != expected_current:
                raise OpsError("target_revision_changed", "Target changed; reload it and retry.", 409)
            if record.get("target_id") != target_id \
                    or record.get("docker_context") != current.get("docker_context") \
                    or record.get("container_name") != current.get("container_name"):
                raise OpsError("target_identity_immutable",
                               "target_id, docker_context and container_name cannot be changed; enroll a new connection instead.", 409)
            if current.get("monitoring") is True:
                raise OpsError("target_must_be_disconnected", "Disconnect this connection before editing it.", 409)
            blocker = self._blockers_locked(target_id)
            if blocker is not None:
                code, message = blocker
                raise OpsError(code, message, 409)
            # Effective-change comparison on normalized TargetSpec fields.
            from .models import TargetSpec as _TS
            try:
                current_spec = _TS.model_validate(
                    {k: v for k, v in current.items() if k in _TS.model_fields}).model_dump(mode="json")
                new_spec_norm = _TS.model_validate(record).model_dump(mode="json")
            except Exception as exc:
                raise OpsError("invalid_record", "Target profile is invalid.") from exc
            if new_spec_norm == current_spec:
                return current, False, None
            now = utc_now()
            old_revision = _revision_of(current)
            new_revision = old_revision + 1
            old_snapshot = self._get_setting_locked(f"snapshot.{target_id}")
            archive_key = f"target_archive.{target_id}.{old_revision}"
            self._put_setting_locked(archive_key, {
                "target_id": target_id, "connection_revision": old_revision,
                "archived_at": now, "reason": "profile_update",
                "spec": current, "snapshot": old_snapshot})
            if old_snapshot is not None:
                self._delete_setting_locked(f"snapshot.{target_id}")
            self._expire_orphans_locked(target_id, now)
            merged = {**current, **new_spec_norm, "created_at": current["created_at"],
                      "updated_at": now, "connection_revision": new_revision}
            merged.pop("verified_deployment", None)
            self._db.execute("UPDATE targets SET payload=? WHERE target_id=?",
                             (_json(merged), target_id))
            event = self._append_event_locked("target_updated", {
                "previous_revision": old_revision, "new_revision": new_revision,
                "archive_key": archive_key}, target_id)
            return json.loads(_json(merged)), True, event

    def remove_target_registration(self, target_id: str, expected_revision, expected_current: dict):
        """Atomic registration removal. Returns (result, event)."""
        _identifier(target_id, "target_id")
        expected_revision = self._validate_revision_value(expected_revision)
        with self._transaction():
            current = self.get_target(target_id)
            if current is None:
                raise OpsError("target_not_found", "Application is not enrolled.", 404)
            if _revision_of(current) != expected_revision:
                raise OpsError("target_revision_changed", "Connection revision changed; reload the target and retry.", 409)
            if current != expected_current:
                raise OpsError("target_revision_changed", "Target changed; reload it and retry.", 409)
            if current.get("monitoring") is True:
                raise OpsError("target_must_be_disconnected", "Disconnect this connection before removing it.", 409)
            blocker = self._blockers_locked(target_id)
            if blocker is not None:
                code, message = blocker
                raise OpsError(code, message, 409)
            last = self._get_setting_locked(f"lastrestart.{target_id}")
            if last is not None:
                try:
                    elapsed = time.time() - float(last)
                    cooldown = (current.get("policy") or {}).get("restart_cooldown_seconds", 600)
                    if isinstance(cooldown, bool) or not isinstance(cooldown, (int, float)):
                        cooldown = 600
                    active = elapsed < max(600, cooldown)
                except (TypeError, ValueError):
                    active = True
                if active:
                    raise OpsError("restart_cooldown",
                                   "A restart was recently attempted; wait for its cooldown before removing this registration.", 409)
            now = utc_now()
            old_revision = _revision_of(current)
            old_snapshot = self._get_setting_locked(f"snapshot.{target_id}")
            archive_key = f"target_archive.{target_id}.{old_revision}"
            self._put_setting_locked(archive_key, {
                "target_id": target_id, "connection_revision": old_revision,
                "archived_at": now, "reason": "registration_removed",
                "spec": current, "snapshot": old_snapshot})
            if old_snapshot is not None:
                self._delete_setting_locked(f"snapshot.{target_id}")
            self._expire_orphans_locked(target_id, now)
            self._db.execute("DELETE FROM targets WHERE target_id=?", (target_id,))
            event = self._append_event_locked("target_removed", {
                "connection_revision": old_revision, "archive_key": archive_key,
                "history_retained": True, "container_stopped": False}, target_id)
            tombstone = {"target_id": target_id, "connection_revision": old_revision,
                         "removed_at": now, "audit_event_seq": event["seq"],
                         "archive_key": archive_key}
            self._db.execute("INSERT INTO target_tombstones(target_id,payload) VALUES (?,?)",
                             (target_id, _json(tombstone)))
            result = {"target_id": target_id, "removed": True,
                      "history_retained": True, "container_stopped": False}
            return result, event

    def get_target(self, target_id: str) -> dict | None:
        return self._get("targets", "target_id", _identifier(target_id, "target_id"))

    def promote_baseline(self, expected_target, expected_snapshot, expected_proposal, target, snapshot, record):
        """Commit source enrollment and its baseline together, with stale-read refusal."""
        target_id = expected_target["target_id"]
        with self._transaction():
            if self.get_target(target_id) != expected_target \
                    or self.get_setting("snapshot." + target_id) != expected_snapshot \
                    or self.get_proposal(expected_proposal["proposal_id"]) != expected_proposal:
                raise OpsError("promotion_state_changed", "Target, baseline or deployment changed during promotion.", 409)
            updated = {**expected_target, **target, "updated_at": utc_now(),
                       "connection_revision": _revision_of(expected_target),
                       "verified_deployment": {"proposal_id": record["proposal_id"],
                           "timestamp": record["timestamp"], "image_id": record["identity"]["image_id"],
                           "patch_id": record["lineage"]["patch_id"]}}
            self._db.execute("UPDATE targets SET payload=? WHERE target_id=?", (_json(updated), target_id))
            for key, value in (("snapshot." + target_id, snapshot),
                               ("promotion." + expected_proposal["proposal_id"], record)):
                self._db.execute("INSERT INTO settings(key,payload) VALUES (?,?) "
                                 "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload", (key, _json(value)))
        return record

    def delete_target(self, target_id: str) -> None:
        with self._transaction():
            self._db.execute("DELETE FROM targets WHERE target_id = ?",
                             (_identifier(target_id, "target_id"),))
        # Deliberately retain historical investigations and evidence.

    def create_investigation(self, target_id: str, question: str,
                             trigger: str = "manual", connection_revision=None) -> dict:
        _identifier(target_id, "target_id")
        if not isinstance(question, str) or len(question) > 1000:
            raise OpsError("invalid_question", "Question must be a string of at most 1000 characters.")
        if not isinstance(trigger, str) or not _EVENT_TYPE.fullmatch(trigger):
            raise OpsError("invalid_trigger", "Invalid investigation trigger.")
        now = utc_now()
        record = {"investigation_id": uuid.uuid4().hex, "target_id": target_id,
                  "question": question, "trigger": trigger, "phase": "queued",
                  "created_at": now, "updated_at": now, "evidence": [],
                  "diagnosis": None, "verification": None, "error": None}
        with self._transaction():
            target_record = self.get_target(target_id)
            if target_record is None:
                raise OpsError("target_not_found", "Target was not found.", 404)
            if connection_revision is None:
                record["connection_revision"] = _revision_of(target_record)
            else:
                record["connection_revision"] = self._validate_revision_value(connection_revision)
            self._db.execute("INSERT INTO investigations VALUES (?,?,?,?)",
                             (record["investigation_id"], target_id, now, _json(record)))
        return record

    def update_investigation(self, investigation_id: str, **changes) -> dict:
        _identifier(investigation_id, "investigation_id")
        changes = _object(changes)
        with self._transaction():
            record = self.get_investigation(investigation_id)
            if record is None:
                raise OpsError("investigation_not_found", "Investigation was not found.", 404)
            self._immutable(record, changes, ("investigation_id", "target_id", "created_at", "connection_revision"))
            record.update(changes)
            record["updated_at"] = utc_now()
            self._db.execute("UPDATE investigations SET payload=? WHERE investigation_id=?",
                             (_json(record), investigation_id))
        return record

    def get_investigation(self, investigation_id: str) -> dict | None:
        return self._get("investigations", "investigation_id",
                         _identifier(investigation_id, "investigation_id"))

    def list_investigations(self, target_id: str | None = None,
                            limit: int = 50) -> list[dict]:
        limit = _limit(limit)
        with self._lock:
            self._assert_open()
            if target_id is None:
                rows = self._db.execute(
                    "SELECT payload FROM investigations ORDER BY created_at DESC,rowid DESC LIMIT ?",
                    (limit,))
            else:
                rows = self._db.execute(
                    "SELECT payload FROM investigations WHERE target_id=? "
                    "ORDER BY created_at DESC,rowid DESC LIMIT ?",
                    (_identifier(target_id, "target_id"), limit))
            return [json.loads(row[0]) for row in rows]

    def scan_investigations(self, target_id: str | None = None) -> list[dict]:
        """Unpaginated investigations for safety decisions, never UI windows.

        Same newest-first order as list_investigations. Safety classification,
        busy checks and startup repair must use this; UI snapshots keep the
        bounded list_investigations(limit=50).
        """
        with self._lock:
            self._assert_open()
            if target_id is None:
                rows = self._db.execute(
                    "SELECT payload FROM investigations ORDER BY created_at DESC,rowid DESC")
            else:
                rows = self._db.execute(
                    "SELECT payload FROM investigations WHERE target_id=? "
                    "ORDER BY created_at DESC,rowid DESC",
                    (_identifier(target_id, "target_id"),))
            return [json.loads(row[0]) for row in rows]

    def append_event(self, type: str, payload: dict, target_id: str | None = None,
                     investigation_id: str | None = None) -> dict:
        if not isinstance(type, str) or not _EVENT_TYPE.fullmatch(type):
            raise OpsError("invalid_event_type", "Invalid event type.")
        if target_id is not None:
            _identifier(target_id, "target_id")
        if investigation_id is not None:
            _identifier(investigation_id, "investigation_id")
        event = {"type": type, "timestamp": utc_now(), "target_id": target_id,
                 "investigation_id": investigation_id, "payload": _object(payload)}
        with self._transaction():
            cursor = self._db.execute("INSERT INTO events(payload) VALUES (?)", (_json(event),))
            event["seq"] = cursor.lastrowid
            # Sequence comes from AUTOINCREMENT, never from retained row count.
            self._db.execute("DELETE FROM events WHERE seq <= ?",
                             (event["seq"] - MAX_EVENTS,))
        return event

    def events_since(self, seq: int, limit: int = 200) -> list[dict]:
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise OpsError("invalid_sequence", "Sequence must be a nonnegative integer.")
        limit = _limit(limit, MAX_EVENTS)
        with self._lock:
            self._assert_open()
            return [{**json.loads(row["payload"]), "seq": row["seq"]}
                    for row in self._db.execute(
                        "SELECT seq,payload FROM events WHERE seq>? ORDER BY seq LIMIT ?",
                        (seq, limit))]

    def latest_seq(self) -> int:
        with self._lock:
            self._assert_open()
            row = self._db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
            return int(row[0]) if row else 0

    @staticmethod
    def _immutable(record: dict, changes: dict, keys: tuple[str, ...]) -> None:
        if any(key in changes and changes[key] != record.get(key) for key in keys):
            raise OpsError("immutable_record", "Record identity and creation time cannot be changed.", 409)

    def put_proposal(self, proposal: dict) -> dict:
        record = _object(proposal)
        proposal_id = _identifier(record.setdefault("proposal_id", uuid.uuid4().hex), "proposal_id")
        investigation_id = record.get("investigation_id")
        if investigation_id is not None:
            _identifier(investigation_id, "investigation_id")
        now = utc_now()
        with self._transaction():
            existing = self.get_proposal(proposal_id)
            if existing is not None:
                self._immutable(existing, record, ("proposal_id", "investigation_id", "target_id", "created_at"))
            record = {**(existing or {"status": "proposed"}), **record,
                      "created_at": existing["created_at"] if existing else now,
                      "updated_at": now}
            self._db.execute(
                "INSERT INTO proposals(proposal_id,investigation_id,created_at,payload) VALUES (?,?,?,?) "
                "ON CONFLICT(proposal_id) DO UPDATE SET payload=excluded.payload",
                (proposal_id, investigation_id, record["created_at"], _json(record)))
        return record

    def get_proposal(self, proposal_id: str) -> dict | None:
        return self._get("proposals", "proposal_id", _identifier(proposal_id, "proposal_id"))

    def list_proposals(self, investigation_id: str | None = None) -> list[dict]:
        with self._lock:
            self._assert_open()
            if investigation_id is None:
                rows = self._db.execute("SELECT payload FROM proposals ORDER BY created_at DESC,rowid DESC")
            else:
                rows = self._db.execute(
                    "SELECT payload FROM proposals WHERE investigation_id=? ORDER BY created_at DESC,rowid DESC",
                    (_identifier(investigation_id, "investigation_id"),))
            return [json.loads(row[0]) for row in rows]

    def list_unresolved_unknowns(self, target_id: str) -> list[dict]:
        """Durable target-level gate: every unknown proposal, including old records.

        Scans proposals directly so UI list limits, current in-memory tasks,
        cooldown expiry, disconnect/reconnect or restart cannot hide an
        unreconciled outcome. Never deletes, expires or succeeds unknowns.
        Records covered by a committed reconciliation with a matching content
        hash are excluded; a later new or changed unknown blocks again.
        """
        _identifier(target_id, "target_id")
        with self._lock:
            self._assert_open()
            rows = self._db.execute("SELECT payload FROM proposals").fetchall()
            covered = self._covered_unknowns_locked(target_id)
        result = []
        for row in rows:
            try:
                payload = json.loads(row[0])
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict) and payload.get("target_id") == target_id \
                    and payload.get("status") == "unknown" \
                    and (payload.get("proposal_id"), proposal_content_hash(payload)) not in covered:
                result.append(payload)
        result.sort(key=lambda item: (item.get("created_at") or "", item.get("proposal_id") or ""))
        return result

    def _covered_unknowns_locked(self, target_id: str) -> set[tuple[str | None, str]]:
        """Consumed reconciliation coverage. Caller must hold the lock."""
        covered: set[tuple[str | None, str]] = set()
        for row in self._db.execute(
                "SELECT payload FROM reconciliations WHERE target_id=?", (target_id,)):
            try:
                review = json.loads(row[0])
            except (ValueError, TypeError):
                continue
            if not isinstance(review, dict) or review.get("status") != "consumed":
                continue
            for entry in review.get("unknowns") or []:
                if isinstance(entry, dict) and isinstance(entry.get("content_hash"), str):
                    covered.add((entry.get("proposal_id"), entry["content_hash"]))
        return covered

    def target_has_active_work(self, target_id: str) -> bool:
        """Unpaginated durable work check, also safe within a write transaction."""
        _identifier(target_id, "target_id")
        with self._lock:
            self._assert_open()
            phases = {"queued", "investigating", "awaiting_approval", "executing", "verifying",
                      "preparing_patch", "preparing_deployment"}
            for row in self._db.execute("SELECT payload FROM investigations WHERE target_id=?", (target_id,)):
                if json.loads(row[0]).get("phase") in phases:
                    return True
            for row in self._db.execute("SELECT payload FROM proposals"):
                proposal = json.loads(row[0])
                if proposal.get("target_id") == target_id and proposal.get("status") in {
                        "executing", "dispatching", "verifying", "rolling_back"}:
                    return True
        return False

    def save_review(self, review: dict) -> dict:
        """Persist a reconciliation review; identity fields are immutable."""
        record = _object(review)
        review_id = _identifier(record.get("review_id"), "review_id")
        target_id = _identifier(record.get("target_id"), "target_id")
        now = utc_now()
        with self._transaction():
            existing = self.get_review(review_id)
            if existing is not None:
                self._immutable(existing, record, ("review_id", "target_id", "created_at"))
                if existing.get("target_id") != target_id:
                    raise OpsError("immutable_record", "Record identity and creation time cannot be changed.", 409)
            record = {**record, "created_at": existing["created_at"] if existing else now,
                      "updated_at": now}
            encoded = _json(record)
            self._db.execute(
                "INSERT INTO reconciliations(review_id,target_id,created_at,payload) VALUES (?,?,?,?) "
                "ON CONFLICT(review_id) DO UPDATE SET payload=excluded.payload",
                (review_id, target_id, record["created_at"], encoded))
        return json.loads(encoded)

    def get_review(self, review_id: str) -> dict | None:
        return self._get("reconciliations", "review_id", _identifier(review_id, "review_id"))

    def list_reviews(self, target_id: str) -> list[dict]:
        with self._lock:
            self._assert_open()
            rows = self._db.execute(
                "SELECT payload FROM reconciliations WHERE target_id=? ORDER BY created_at,rowid",
                (_identifier(target_id, "target_id"),))
            return [json.loads(row[0]) for row in rows]

    def expire_pending_reviews(self) -> int:
        """Expire unconfirmed reviews (for example after a restart)."""
        count = 0
        with self._transaction():
            for row in self._db.execute("SELECT payload FROM reconciliations").fetchall():
                try:
                    review = json.loads(row[0])
                except (ValueError, TypeError):
                    continue
                if isinstance(review, dict) and review.get("status") == "pending":
                    review["status"] = "expired"
                    review["updated_at"] = utc_now()
                    self._db.execute("UPDATE reconciliations SET payload=? WHERE review_id=?",
                                     (_json(review), review["review_id"]))
                    count += 1
        return count

    def confirm_reconciliation_review(self, *, review_id: str, review_hash: str, target_id: str,
                                      expected_target: dict, expected_unknowns: dict[str, str],
                                      operator_note: str) -> dict:
        """Atomically consume a review: audit event, policy reset, pending expiry.

        One SQLite transaction with compare-and-set checks on the current
        target record and the exact unknown set (IDs plus content hashes). A
        new/changed unknown or a changed target aborts with no partial writes.
        Original unknown records keep status=unknown with history intact.
        """
        _identifier(review_id, "review_id")
        _identifier(target_id, "target_id")
        if not isinstance(review_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", review_hash):
            raise OpsError("invalid_review_hash", "Review hash must be 64 hexadecimal characters.")
        if not isinstance(operator_note, str) or not operator_note.strip() or len(operator_note) > 500:
            raise OpsError("invalid_operator_note", "A bounded nonempty operator note is required.")
        operator_note = redact_text(operator_note.strip())
        with self._transaction():
            review = self.get_review(review_id)
            if review is None or review.get("target_id") != target_id:
                raise OpsError("review_not_found", "Reconciliation review was not found.", 404)
            if review.get("status") == "consumed":
                raise OpsError("review_consumed", "This review was already consumed.", 409)
            if review.get("status") != "pending" or review.get("review_hash") != review_hash:
                raise OpsError("review_state_changed", "The review is no longer available for confirmation.", 409)
            try:
                expired = review["expires_at"] <= utc_now()
            except (KeyError, TypeError):
                expired = True
            if expired:
                raise OpsError("review_expired", "The review expired; collect a fresh review.", 409)
            if self.target_has_active_work(target_id):
                raise OpsError("reconciliation_active_operation",
                               "Finish or cancel the active operation before confirming.", 409)
            reviewed_unknowns = {entry["proposal_id"]: entry["content_hash"]
                                for entry in review.get("unknowns", [])}
            if not reviewed_unknowns or reviewed_unknowns != expected_unknowns:
                raise OpsError("reconciliation_state_changed", "Reviewed operation records changed.", 409)
            current_target = self.get_target(target_id)
            if current_target is None or current_target != expected_target:
                raise OpsError("reconciliation_state_changed",
                               "Target enrollment changed; collect a fresh review.", 409)
            covered = self._covered_unknowns_locked(target_id)
            live: dict[str, str] = {}
            for row in self._db.execute("SELECT payload FROM proposals").fetchall():
                try:
                    payload = json.loads(row[0])
                except (ValueError, TypeError):
                    continue
                if isinstance(payload, dict) and payload.get("target_id") == target_id \
                        and payload.get("status") == "unknown":
                    entry = (payload.get("proposal_id"), proposal_content_hash(payload))
                    if entry not in covered:
                        live[entry[0]] = entry[1]
            if live != expected_unknowns:
                raise OpsError("reconciliation_state_changed",
                               "Unknown operation records changed; collect a fresh review.", 409)
            now = utc_now()
            expired_ids: list[str] = []
            for row in self._db.execute("SELECT payload FROM proposals").fetchall():
                try:
                    payload = json.loads(row[0])
                except (ValueError, TypeError):
                    continue
                if isinstance(payload, dict) and payload.get("target_id") == target_id \
                        and payload.get("status") == "proposed":
                    payload["status"] = "expired"
                    payload["updated_at"] = now
                    self._db.execute("UPDATE proposals SET payload=? WHERE proposal_id=?",
                                     (_json(payload), payload["proposal_id"]))
                    expired_ids.append(payload["proposal_id"])
            observe_only = {**current_target,
                            "policy": {"automatic_actions": [],
                                       "auto_investigate": False,
                                       "restart_cooldown_seconds": (current_target.get("policy") or {}).get(
                                           "restart_cooldown_seconds", 600)},
                            "updated_at": now}
            self._db.execute("UPDATE targets SET payload=? WHERE target_id=?",
                             (_json(observe_only), target_id))
            event = {"type": "reconciliation_confirmed", "timestamp": now, "target_id": target_id,
                     "investigation_id": None,
                     "payload": {"review_id": review_id, "reconciled_ids": sorted(expected_unknowns),
                                 "expired_proposals": sorted(expired_ids), "operator_note": operator_note.strip(),
                                 "result": "outcome_reconciled"}}
            cursor = self._db.execute("INSERT INTO events(payload) VALUES (?)", (_json(event),))
            event["seq"] = cursor.lastrowid
            self._db.execute("DELETE FROM events WHERE seq <= ?", (event["seq"] - MAX_EVENTS,))
            review["status"] = "consumed"
            review["updated_at"] = now
            review["confirmation"] = {"review_hash": review_hash, "operator_note": operator_note.strip(),
                                      "confirmed_at": now, "reconciled_ids": sorted(expected_unknowns),
                                      "expired_proposals": sorted(expired_ids),
                                      "event_seq": event["seq"]}
            self._db.execute("UPDATE reconciliations SET payload=? WHERE review_id=?",
                             (_json(review), review_id))
        return {"review": review, "event": event, "expired_proposals": sorted(expired_ids),
                "reconciled_ids": sorted(expected_unknowns)}

    def _change_proposal(self, proposal_id: str, changes: dict,
                         expected_status: str | None = None) -> dict | None:
        changes = _object(changes)
        with self._transaction():
            record = self.get_proposal(proposal_id)
            if record is None:
                raise OpsError("proposal_not_found", "Proposal was not found.", 404)
            if expected_status is not None and record.get("status") != expected_status:
                return None
            self._immutable(record, changes, ("proposal_id", "investigation_id", "target_id", "created_at"))
            record.update(changes)
            record["updated_at"] = utc_now()
            self._db.execute("UPDATE proposals SET payload=? WHERE proposal_id=?",
                             (_json(record), proposal_id))
            return record

    def update_proposal(self, proposal_id: str, **changes) -> dict:
        return self._change_proposal(_identifier(proposal_id, "proposal_id"), changes)

    def transition_proposal(self, proposal_id: str, expected_status: str,
                            new_status: str, **changes) -> dict | None:
        if not isinstance(expected_status, str) or not isinstance(new_status, str) \
                or not _EVENT_TYPE.fullmatch(expected_status) or not _EVENT_TYPE.fullmatch(new_status):
            raise OpsError("invalid_status", "Invalid proposal status.")
        return self._change_proposal(_identifier(proposal_id, "proposal_id"),
                                     {**changes, "status": new_status}, expected_status)

    def put_setting(self, key: str, value: Any) -> None:
        key = _identifier(key, "setting key")
        encoded = _json(value)
        with self._transaction():
            self._db.execute("INSERT INTO settings(key,payload) VALUES (?,?) "
                             "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload", (key, encoded))

    def get_setting(self, key: str, default: Any = None) -> Any:
        key = _identifier(key, "setting key")
        with self._lock:
            self._assert_open()
            row = self._db.execute("SELECT payload FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def _artifact_path(self, artifact_id: str) -> Path:
        # Only UUID-derived names go on disk; display names never form a path.
        if not re.fullmatch(r"[a-f0-9]{32}", artifact_id):
            raise OpsError("invalid_artifact_id", "Invalid artifact identifier.")
        path = self.artifacts_dir / artifact_id
        if self.artifacts_dir.resolve() != self.artifacts_dir or path.is_symlink() \
                or path.resolve().parent != self.artifacts_dir:
            raise OpsError("invalid_artifact_path", "Artifact path is outside the store.")
        return path

    def save_artifact(self, name: str, content: bytes,
                       media_type: str = "application/octet-stream") -> dict:
        if not isinstance(name, str) or not name or len(name) > 180 \
                or name in (".", "..") or name != name.strip() \
                or any(ord(char) < 32 or ord(char) == 127 or char in '/\\:' for char in name):
            raise OpsError("invalid_artifact_name", "Artifact name must be a plain filename.")
        if not isinstance(content, bytes):
            raise OpsError("invalid_artifact", "Artifact content must be bytes.")
        if len(content) > MAX_ARTIFACT_BYTES:
            raise OpsError("artifact_too_large", "Artifact exceeds 20 MiB.", 413)
        if not isinstance(media_type, str) or not re.fullmatch(
                r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", media_type):
            raise OpsError("invalid_media_type", "Invalid artifact media type.")
        artifact_id = uuid.uuid4().hex
        destination = self._artifact_path(artifact_id)
        temporary = self._artifact_path(uuid.uuid4().hex)
        record = {"artifact_id": artifact_id, "name": name, "media_type": media_type,
                  "sha256": hashlib.sha256(content).hexdigest(), "size": len(content),
                  "created_at": utc_now()}
        with self._lock:
            self._assert_open()
            try:
                with temporary.open("xb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
                with self._transaction():
                    self._db.execute("INSERT INTO artifacts VALUES (?,?)", (artifact_id, _json(record)))
            except BaseException:
                # Only remove exact private files created by this invocation.
                temporary.unlink(missing_ok=True)
                destination.unlink(missing_ok=True)
                raise
        return record

    def get_artifact(self, artifact_id: str) -> dict | None:
        path = self._artifact_path(artifact_id)
        record = self._get("artifacts", "artifact_id", artifact_id)
        if record is None:
            return None
        try:
            with path.open("rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                if size != record["size"] or size > MAX_ARTIFACT_BYTES:
                    raise OpsError("artifact_corrupt", "Artifact integrity check failed.", 409)
                digest = hashlib.sha256()
                for block in iter(lambda: handle.read(65536), b""):
                    digest.update(block)
            if digest.hexdigest() != record["sha256"]:
                raise OpsError("artifact_corrupt", "Artifact integrity check failed.", 409)
        except OSError as exc:
            raise OpsError("artifact_missing", "Artifact file is unavailable.", 404) from exc
        return {**record, "path": str(path)}
