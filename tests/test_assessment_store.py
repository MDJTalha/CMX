from datetime import datetime, timezone
import sqlite3

import pytest

from cmx.assessment_store import (
    AssessmentIntegrityError,
    AssessmentNotFoundError,
    SQLiteAssessmentStore,
)


def snapshot(store, tenant="tenant_alpha"):
    return store.create(
        tenant_id=tenant,
        template_id="sme-back-office-workflow",
        template_version=1,
        request={"problem_statement": "Reduce manual processing", "requirements": [{"key": "audit_log"}]},
        response={"status": "validation_needed", "score": 0.5},
    )


def test_assessment_is_durable_and_versioned_across_reopen(tmp_path):
    path = tmp_path / "assessments.sqlite3"
    created = snapshot(SQLiteAssessmentStore(path))
    reopened = SQLiteAssessmentStore(path)
    loaded = reopened.get(tenant_id="tenant_alpha", assessment_id=created["assessment_id"])
    assert loaded == created
    assert loaded["template_id"] == "sme-back-office-workflow"
    assert loaded["template_version"] == 1
    assert reopened.verify(loaded)


def test_assessments_are_isolated_by_tenant(tmp_path):
    store = SQLiteAssessmentStore(tmp_path / "assessments.sqlite3")
    record = snapshot(store)
    assert store.get(tenant_id="tenant_beta", assessment_id=record["assessment_id"]) is None
    assert store.list(tenant_id="tenant_beta") == []
    assert len(store.list(tenant_id="tenant_alpha")) == 1


def test_assessment_snapshots_are_append_only_and_duplicate_ids_fail(tmp_path):
    store = SQLiteAssessmentStore(tmp_path / "assessments.sqlite3")
    record = snapshot(store)
    with pytest.raises(sqlite3.IntegrityError):
        store.create(
            tenant_id="tenant_alpha", assessment_id=record["assessment_id"],
            request={"different": True}, response={"status": "changed"},
        )
    assert store.get(tenant_id="tenant_alpha", assessment_id=record["assessment_id"]) == record


def test_snapshot_hash_detects_database_tampering(tmp_path):
    path = tmp_path / "assessments.sqlite3"
    store = SQLiteAssessmentStore(path)
    record = snapshot(store)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE cmx_assessments SET response_json = ? WHERE assessment_id = ?",
            ('{"status":"forged"}', record["assessment_id"]),
        )
    with pytest.raises(AssessmentIntegrityError):
        store.get(tenant_id="tenant_alpha", assessment_id=record["assessment_id"])


def test_evidence_claim_records_provenance_content_hash_and_expiry(tmp_path):
    store = SQLiteAssessmentStore(tmp_path / "assessments.sqlite3")
    record = snapshot(store)
    claim = store.add_evidence_claim(
        tenant_id="tenant_alpha", assessment_id=record["assessment_id"],
        claim_id="claim_001", claim_text="Vendor supports audit logs",
        source_title="Vendor security documentation", source_url="https://example.com/security",
        source_owner="Example Vendor", retrieved_at="2026-01-01T00:00:00+00:00",
        expires_at="2026-02-01T00:00:00+00:00", verification_method="manual_review",
        content="Supports audit logs in enterprise plan",
    )
    assert claim["content_sha256"]
    assert claim["freshness"] == "expired"
    current = store.get_evidence_claim(
        tenant_id="tenant_alpha", assessment_id=record["assessment_id"],
        claim_id="claim_001", now=datetime(2026, 1, 15, tzinfo=timezone.utc),
    )
    assert current["freshness"] == "current"


def test_evidence_requires_existing_assessment_and_tenant_scope(tmp_path):
    store = SQLiteAssessmentStore(tmp_path / "assessments.sqlite3")
    with pytest.raises(AssessmentNotFoundError):
        store.add_evidence_claim(
            tenant_id="tenant_alpha", assessment_id="missing", claim_id="claim_001",
            claim_text="claim", source_title="source", source_url=None, source_owner=None,
            retrieved_at="2026-01-01T00:00:00+00:00", expires_at=None,
            verification_method="manual", content="source text",
        )
    record = snapshot(store)
    assert store.get_evidence_claim(
        tenant_id="tenant_beta", assessment_id=record["assessment_id"], claim_id="claim_001"
    ) is None


def test_evidence_rejects_naive_or_invalid_expiry_timestamps(tmp_path):
    store = SQLiteAssessmentStore(tmp_path / "assessments.sqlite3")
    record = snapshot(store)
    args = dict(
        tenant_id="tenant_alpha", assessment_id=record["assessment_id"], claim_id="claim_001",
        claim_text="claim", source_title="source", source_url=None, source_owner=None,
        expires_at="2026-02-01T00:00:00+00:00", verification_method="manual", content="text",
    )
    with pytest.raises(ValueError, match="timezone"):
        store.add_evidence_claim(**args, retrieved_at="2026-01-01T00:00:00")
    with pytest.raises(ValueError, match="later"):
        store.add_evidence_claim(**{**args, "expires_at": "2025-12-01T00:00:00+00:00"},
                                 retrieved_at="2026-01-01T00:00:00+00:00")


def test_backup_restore_preserves_assessment_and_evidence(tmp_path):
    original = SQLiteAssessmentStore(tmp_path / "source" / "assessments.sqlite3")
    record = snapshot(original)
    original.add_evidence_claim(
        tenant_id="tenant_alpha", assessment_id=record["assessment_id"],
        claim_id="claim_001", claim_text="Claim", source_title="Source", source_url=None,
        source_owner=None, retrieved_at="2026-01-01T00:00:00+00:00", expires_at=None,
        verification_method="manual", content="evidence",
    )
    backup = original.backup(tmp_path / "backup.sqlite3")
    restored = SQLiteAssessmentStore.restore(backup, tmp_path / "restored" / "assessments.sqlite3")
    assert restored.get(tenant_id="tenant_alpha", assessment_id=record["assessment_id"]) == record
    assert restored.get_evidence_claim(
        tenant_id="tenant_alpha", assessment_id=record["assessment_id"], claim_id="claim_001"
    )["content_sha256"]


def test_backup_restore_rejects_missing_or_same_path(tmp_path):
    path = tmp_path / "assessments.sqlite3"
    store = SQLiteAssessmentStore(path)
    with pytest.raises(ValueError, match="differ"):
        store.backup(path)
    with pytest.raises(ValueError, match="existing backup"):
        SQLiteAssessmentStore.restore(tmp_path / "missing.sqlite3", tmp_path / "restore.sqlite3")


def test_file_backed_store_and_bounded_list_limit_are_required(tmp_path):
    with pytest.raises(ValueError, match="file-backed"):
        SQLiteAssessmentStore(":memory:")
    store = SQLiteAssessmentStore(tmp_path / "assessments.sqlite3")
    with pytest.raises(ValueError, match="between 1 and"):
        store.list(tenant_id="tenant_alpha", limit=101)



def test_store_rejects_unknown_template_versions(tmp_path):
    store = SQLiteAssessmentStore(tmp_path / "assessments.sqlite3")
    with pytest.raises(ValueError, match="supported template version"):
        store.create(
            tenant_id="tenant_alpha", template_id="sme-back-office-workflow",
            template_version=99, request={"problem": "test"}, response={"status": "unknown"},
        )
