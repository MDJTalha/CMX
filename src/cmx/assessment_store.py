"""Tenant-scoped, durable assessment records and claim-level evidence.

SQLite is the single-node reference store. Records are append-only through the
public API; this module does not claim tamper-proof storage against a DB owner.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cmx.assessment_templates import validate_template_reference


SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 1_000_000
MAX_LIST_LIMIT = 100


class AssessmentNotFoundError(LookupError):
    pass


class AssessmentIntegrityError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SQLiteAssessmentStore:
    """Persist immutable decision snapshots and expiring claim-level evidence."""

    def __init__(self, database_path: str | Path):
        raw = str(database_path)
        if raw == ":memory:":
            raise ValueError("Assessment store requires a file-backed SQLite database")
        self.path = Path(raw).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS cmx_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
            """)
            row = db.execute("SELECT MAX(version) AS version FROM cmx_schema_migrations").fetchone()
            version = row["version"] or 0
            if version > SCHEMA_VERSION:
                raise AssessmentIntegrityError("Database schema is newer than this CMX version")
            if version < 1:
                db.executescript("""
                    CREATE TABLE cmx_assessments (
                        tenant_id TEXT NOT NULL,
                        assessment_id TEXT NOT NULL,
                        template_id TEXT NOT NULL,
                        template_version INTEGER NOT NULL CHECK(template_version > 0),
                        created_at TEXT NOT NULL,
                        request_json TEXT NOT NULL,
                        response_json TEXT NOT NULL,
                        snapshot_sha256 TEXT NOT NULL,
                        PRIMARY KEY (tenant_id, assessment_id)
                    );
                    CREATE TABLE cmx_evidence_claims (
                        tenant_id TEXT NOT NULL,
                        assessment_id TEXT NOT NULL,
                        claim_id TEXT NOT NULL,
                        claim_text TEXT NOT NULL,
                        source_title TEXT NOT NULL,
                        source_url TEXT,
                        source_owner TEXT,
                        retrieved_at TEXT NOT NULL,
                        expires_at TEXT,
                        verification_method TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (tenant_id, assessment_id, claim_id),
                        FOREIGN KEY (tenant_id, assessment_id)
                            REFERENCES cmx_assessments(tenant_id, assessment_id)
                    );
                    CREATE INDEX cmx_assessment_created_idx
                        ON cmx_assessments(tenant_id, created_at DESC);
                    CREATE INDEX cmx_evidence_expiry_idx
                        ON cmx_evidence_claims(tenant_id, expires_at);
                """)
                db.execute(
                    "INSERT INTO cmx_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (1, _utc_now()),
                )

    def create(
        self, *, tenant_id: str, request: dict[str, Any], response: dict[str, Any],
        template_id: str = "general-assessment", template_version: int = 1,
        assessment_id: str | None = None,
    ) -> dict[str, Any]:
        self._validate_identity(tenant_id, "tenant_id")
        self._validate_identity(template_id, "template_id")
        if not isinstance(template_version, int) or isinstance(template_version, bool) or template_version < 1:
            raise ValueError("template_version must be a positive integer")
        if not validate_template_reference(template_id, template_version):
            raise ValueError("template_id/template_version is not a supported template version")
        request_json, response_json = _canonical(request), _canonical(response)
        if len(request_json.encode("utf-8")) + len(response_json.encode("utf-8")) > MAX_RECORD_BYTES:
            raise ValueError("Assessment snapshot exceeds the 1 MB limit")
        identifier = assessment_id or "asm_" + uuid.uuid4().hex
        self._validate_identity(identifier, "assessment_id")
        created_at = _utc_now()
        snapshot = _canonical({
            "tenant_id": tenant_id, "assessment_id": identifier, "template_id": template_id,
            "template_version": template_version, "created_at": created_at,
            "request": json.loads(request_json), "response": json.loads(response_json),
        })
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("""
                    INSERT INTO cmx_assessments
                    (tenant_id, assessment_id, template_id, template_version, created_at,
                     request_json, response_json, snapshot_sha256)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (tenant_id, identifier, template_id, template_version, created_at,
                      request_json, response_json, _digest(snapshot)))
                db.commit()
            except Exception:
                db.rollback()
                raise
        return self.get(tenant_id=tenant_id, assessment_id=identifier)

    def get(self, *, tenant_id: str, assessment_id: str) -> dict[str, Any] | None:
        self._validate_identity(tenant_id, "tenant_id")
        self._validate_identity(assessment_id, "assessment_id")
        with self._connect() as db:
            row = db.execute("""
                SELECT * FROM cmx_assessments WHERE tenant_id = ? AND assessment_id = ?
            """, (tenant_id, assessment_id)).fetchone()
        if row is None:
            return None
        result = self._record(row)
        if not self.verify(result):
            raise AssessmentIntegrityError("Assessment snapshot integrity verification failed")
        return result

    def list(self, *, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
        self._validate_identity(tenant_id, "tenant_id")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIST_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
        with self._connect() as db:
            rows = db.execute("""
                SELECT * FROM cmx_assessments WHERE tenant_id = ?
                ORDER BY created_at DESC, assessment_id DESC LIMIT ?
            """, (tenant_id, limit)).fetchall()
        records = [self._record(row) for row in rows]
        if not all(self.verify(record) for record in records):
            raise AssessmentIntegrityError("Assessment snapshot integrity verification failed")
        return records

    def add_evidence_claim(
        self, *, tenant_id: str, assessment_id: str, claim_id: str, claim_text: str,
        source_title: str, source_url: str | None, source_owner: str | None,
        retrieved_at: str, expires_at: str | None, verification_method: str,
        content: str,
    ) -> dict[str, Any]:
        for value, name in ((tenant_id, "tenant_id"), (assessment_id, "assessment_id"),
                            (claim_id, "claim_id")):
            self._validate_identity(value, name)
        required = {
            "claim_text": claim_text, "source_title": source_title,
            "retrieved_at": retrieved_at, "verification_method": verification_method,
            "content": content,
        }
        for name, value in required.items():
            if not isinstance(value, str) or not value.strip() or len(value) > 20_000:
                raise ValueError(f"{name} must be a non-empty string of at most 20000 characters")
        # Require timezone-aware ISO timestamps so freshness is not based on local assumptions.
        retrieved = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
        if retrieved.tzinfo is None:
            raise ValueError("retrieved_at must include a timezone")
        if expires_at is not None:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expiry.tzinfo is None or expiry <= retrieved:
                raise ValueError("expires_at must be timezone-aware and later than retrieved_at")
        with self._connect() as db:
            if db.execute(
                "SELECT 1 FROM cmx_assessments WHERE tenant_id=? AND assessment_id=?",
                (tenant_id, assessment_id),
            ).fetchone() is None:
                raise AssessmentNotFoundError("Assessment not found")
            db.execute("""
                INSERT INTO cmx_evidence_claims
                (tenant_id, assessment_id, claim_id, claim_text, source_title, source_url,
                 source_owner, retrieved_at, expires_at, verification_method,
                 content_sha256, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (tenant_id, assessment_id, claim_id, claim_text, source_title, source_url,
                  source_owner, retrieved_at, expires_at, verification_method,
                  _digest(content), _utc_now()))
        return self.get_evidence_claim(
            tenant_id=tenant_id, assessment_id=assessment_id, claim_id=claim_id
        )

    def get_evidence_claim(
        self, *, tenant_id: str, assessment_id: str, claim_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("""
                SELECT * FROM cmx_evidence_claims
                WHERE tenant_id=? AND assessment_id=? AND claim_id=?
            """, (tenant_id, assessment_id, claim_id)).fetchone()
        if row is None:
            return None
        item = dict(row)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        expiry = datetime.fromisoformat(item["expires_at"].replace("Z", "+00:00")) if item["expires_at"] else None
        item["freshness"] = "expired" if expiry and expiry <= current else "current"
        return item

    def backup(self, destination: str | Path) -> Path:
        target = Path(destination).expanduser().resolve()
        if target == self.path:
            raise ValueError("Backup destination must differ from the source database")
        target.parent.mkdir(parents=True, exist_ok=True)
        # SQLite's backup API safely captures committed state, including WAL content.
        with closing(self._connect()) as source, closing(sqlite3.connect(target)) as dest:
            source.backup(dest)
        return target

    @staticmethod
    def restore(backup_path: str | Path, destination: str | Path) -> "SQLiteAssessmentStore":
        source_path, target = Path(backup_path).expanduser().resolve(), Path(destination).expanduser().resolve()
        if source_path == target or not source_path.is_file():
            raise ValueError("A distinct existing backup file is required")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Validate backup before replacing any destination file.
        with closing(sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)) as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            version = db.execute("SELECT MAX(version) FROM cmx_schema_migrations").fetchone()[0]
        if integrity != "ok" or version != SCHEMA_VERSION:
            raise AssessmentIntegrityError("Backup integrity or schema version check failed")
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".cmx-restore-", delete=False) as tmp:
            temp_path = Path(tmp.name)
        try:
            with closing(sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)) as source, closing(sqlite3.connect(temp_path)) as dest:
                source.backup(dest)
            with closing(sqlite3.connect(temp_path)) as check:
                if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise AssessmentIntegrityError("Restored database integrity check failed")
            os.replace(temp_path, target)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        return SQLiteAssessmentStore(target)

    def verify(self, record: dict[str, Any]) -> bool:
        try:
            snapshot = _canonical({
                "tenant_id": record["tenant_id"], "assessment_id": record["assessment_id"],
                "template_id": record["template_id"], "template_version": record["template_version"],
                "created_at": record["created_at"], "request": record["request"],
                "response": record["response"],
            })
            return _digest(snapshot) == record["snapshot_sha256"]
        except (KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "tenant_id": row["tenant_id"], "assessment_id": row["assessment_id"],
            "template_id": row["template_id"], "template_version": row["template_version"],
            "created_at": row["created_at"], "request": json.loads(row["request_json"]),
            "response": json.loads(row["response_json"]), "snapshot_sha256": row["snapshot_sha256"],
        }

    @staticmethod
    def _validate_identity(value: str, field: str) -> None:
        if not isinstance(value, str) or not value or len(value) > 128 or any(ord(c) < 33 for c in value):
            raise ValueError(f"{field} must be a non-empty identifier of at most 128 characters")
