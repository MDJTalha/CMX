import hashlib
import sqlite3

import pytest
from pydantic import ValidationError

from cmx.service_auth import (
    CredentialStoreIntegrityError,
    InvalidServiceCredential,
    SQLiteCredentialStore,
)


def make_store(tmp_path):
    return SQLiteCredentialStore(tmp_path / "auth.sqlite3")


def issue(store, **overrides):
    values = {
        "subject_id": "svc_assessments",
        "tenant_id": "tenant_alpha",
        "permissions": ["assessment:write", "assessment:read"],
        "expires_in_seconds": 3600,
        "now_epoch": 100,
    }
    values.update(overrides)
    return store.issue(**values)


def test_issue_returns_secret_once_and_only_digest_is_persisted(tmp_path):
    store = make_store(tmp_path)
    issued = issue(store)

    assert issued.token.startswith("cmx_live_")
    assert len(issued.token.split(".", 1)[1]) == 43
    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            "SELECT token_prefix, secret_sha256 FROM cmx_service_credentials WHERE credential_id = ?",
            (issued.credential_id,),
        ).fetchone()

    prefix, digest = issued.token.removeprefix("cmx_live_").split(".", 1)
    assert row == (prefix, hashlib.sha256(digest.encode("ascii")).hexdigest())
    assert issued.token not in repr(row)
    assert store.verify_audit_chain()


def test_authentication_derives_verified_principal_from_database(tmp_path):
    store = make_store(tmp_path)
    issued = issue(store)

    principal = store.authenticate(issued.token, now_epoch=101)

    assert principal.subject_id == "svc_assessments"
    assert principal.tenant_id == "tenant_alpha"
    assert principal.permissions == frozenset({"assessment:write", "assessment:read"})
    assert principal.authentication_verified is True
    assert principal.authentication_method == "service"
    assert principal.credential_id == issued.credential_id
    assert principal.issued_at == 100
    assert principal.expires_at == 3700


@pytest.mark.parametrize("token", ["", "Bearer cmx_live_token", "cmx_live_bad.bad", None, 12])
def test_malformed_credentials_are_rejected_without_disclosing_details(tmp_path, token):
    store = make_store(tmp_path)
    with pytest.raises(InvalidServiceCredential):
        store.authenticate(token, now_epoch=101)


def test_wrong_secret_is_rejected(tmp_path):
    store = make_store(tmp_path)
    issued = issue(store)
    prefix, _ = issued.token.split(".", 1)
    wrong_token = prefix + "." + ("A" * 43)

    with pytest.raises(InvalidServiceCredential):
        store.authenticate(wrong_token, now_epoch=101)


def test_unknown_token_prefix_is_rejected(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(InvalidServiceCredential):
        store.authenticate("cmx_live_ABCDEFGHIJKL." + ("B" * 43), now_epoch=101)


@pytest.mark.parametrize("now", [99, 3700])
def test_not_yet_valid_and_expired_credentials_are_rejected(tmp_path, now):
    store = make_store(tmp_path)
    issued = issue(store)
    with pytest.raises(InvalidServiceCredential):
        store.authenticate(issued.token, now_epoch=now)


def test_revocation_is_audited_and_cannot_be_reversed(tmp_path):
    store = make_store(tmp_path)
    issued = issue(store)

    assert store.revoke(issued.credential_id, now_epoch=150) is True
    assert store.revoke(issued.credential_id, now_epoch=151) is False
    with pytest.raises(InvalidServiceCredential):
        store.authenticate(issued.token, now_epoch=151)

    with sqlite3.connect(store.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="one-way"):
            connection.execute(
                "UPDATE cmx_service_credentials SET revoked_at = NULL WHERE credential_id = ?",
                (issued.credential_id,),
            )
    assert store.verify_audit_chain()


def test_credential_identity_cannot_be_edited_or_deleted(tmp_path):
    store = make_store(tmp_path)
    issued = issue(store)

    with sqlite3.connect(store.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE cmx_service_credentials SET tenant_id = 'tenant_other' WHERE credential_id = ?",
                (issued.credential_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute(
                "DELETE FROM cmx_service_credentials WHERE credential_id = ?",
                (issued.credential_id,),
            )


def test_audit_chain_tampering_is_detected_and_blocks_further_issuance(tmp_path):
    store = make_store(tmp_path)
    issue(store)

    with sqlite3.connect(store.database_path) as connection:
        connection.execute("DROP TRIGGER cmx_credential_events_no_update")
        connection.execute(
            "UPDATE cmx_credential_events SET payload_json = '{}' WHERE event_id = 1"
        )

    with pytest.raises(CredentialStoreIntegrityError, match="integrity validation"):
        store.verify_audit_chain()
    with pytest.raises(CredentialStoreIntegrityError, match="integrity validation"):
        issue(store, subject_id="svc_second")


@pytest.mark.parametrize(
    "overrides",
    [
        {"expires_in_seconds": 59},
        {"expires_in_seconds": 365 * 24 * 60 * 60 + 1},
        {"permissions": []},
        {"permissions": ["assessment:write", "assessment:write"]},
        {"permissions": ["Admin:*"]},
        {"subject_id": "x"},
        {"tenant_id": "x"},
    ],
)
def test_invalid_credential_policy_is_rejected_before_persistence(tmp_path, overrides):
    store = make_store(tmp_path)
    with pytest.raises((ValueError, ValidationError)):
        issue(store, **overrides)

    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM cmx_service_credentials"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM cmx_credential_events"
        ).fetchone()[0] == 0


def test_credential_audit_events_are_append_only(tmp_path):
    store = make_store(tmp_path)
    issued = issue(store)

    with sqlite3.connect(store.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE cmx_credential_events SET payload_json = '{}' WHERE credential_id = ?",
                (issued.credential_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM cmx_credential_events WHERE credential_id = ?",
                (issued.credential_id,),
            )

    assert store.verify_audit_chain()
