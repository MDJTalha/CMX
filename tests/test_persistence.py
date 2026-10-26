import sqlite3

import pytest
from pydantic import ValidationError

from cmx.kernel import StaleTaskVersionError, TaskEvent, TaskRecord, TaskState
from cmx.persistence import (
    AuditIntegrityError,
    SQLiteTaskStore,
    TaskAlreadyExistsError,
    TaskNotFoundError,
)
from cmx.security import AuthorizationError, PrincipalContext


ALL_TASK_PERMISSIONS = frozenset(
    {"tasks:create", "tasks:read", "tasks:write", "audit:read"}
)


def principal(
    *,
    tenant_id: str = "tenant_alpha",
    permissions: frozenset[str] = ALL_TASK_PERMISSIONS,
    issued_at: int = 100,
    expires_at: int = 200,
    authentication_verified: bool = True,
) -> PrincipalContext:
    return PrincipalContext(
        subject_id="user_001",
        tenant_id=tenant_id,
        issuer="https://identity.cmx.test",
        permissions=permissions,
        issued_at=issued_at,
        expires_at=expires_at,
        authentication_verified=authentication_verified,
        authentication_method="test",
        credential_id="cred_001",
    )


def create(store: SQLiteTaskStore, *, task_id: str = "task_001", identity=None) -> TaskRecord:
    return store.create_task(
        TaskRecord(task_id=task_id, max_attempts=3),
        principal=identity or principal(),
        occurred_at=150,
        authorization_now_epoch=150,
    )


def event(
    store: SQLiteTaskStore,
    task_id: str,
    transition: TaskEvent,
    *,
    version: int,
    identity=None,
) -> TaskRecord:
    return store.apply_event(
        task_id,
        transition,
        principal=identity or principal(),
        expected_version=version,
        occurred_at=151 + version,
        authorization_now_epoch=150,
    )


def audit(store: SQLiteTaskStore, task_id: str = "task_001", *, identity=None):
    return store.list_audit_events(
        task_id,
        principal=identity or principal(),
        authorization_now_epoch=150,
    )


def test_create_and_reopen_task_store_preserves_task_and_audit_history(tmp_path):
    path = tmp_path / "cmx.sqlite3"
    store = SQLiteTaskStore(path)
    created = create(store)
    assert created.state == TaskState.CREATED
    assert created.version == 0
    assert store.verify_audit_chain(
        "task_001", principal=principal(), authorization_now_epoch=150
    )

    reopened = SQLiteTaskStore(path)
    loaded = reopened.get_task(
        "task_001", principal=principal(), authorization_now_epoch=150
    )
    assert loaded == created
    events = audit(reopened)
    assert len(events) == 1
    assert events[0].event_type == "task.created"
    assert events[0].sequence == 0
    assert events[0].actor_id == "user_001"


def test_state_transition_and_audit_append_are_durable_and_ordered(tmp_path):
    store = SQLiteTaskStore(tmp_path / "cmx.sqlite3")
    create(store)
    record = event(store, "task_001", TaskEvent.PLAN_ACCEPTED, version=0)
    assert record.state == TaskState.PLANNED
    record = event(store, "task_001", TaskEvent.START, version=1)
    assert record.state == TaskState.RUNNING
    record = event(store, "task_001", TaskEvent.BEGIN_VERIFICATION, version=2)
    assert record.state == TaskState.VERIFYING
    record = event(store, "task_001", TaskEvent.VERIFICATION_PASSED, version=3)
    assert record.state == TaskState.SUCCEEDED
    assert record.version == 4

    loaded = SQLiteTaskStore(tmp_path / "cmx.sqlite3").get_task(
        "task_001", principal=principal(), authorization_now_epoch=150
    )
    assert loaded == record
    history = audit(store)
    assert [item.sequence for item in history] == [0, 1, 2, 3, 4]
    assert [item.event_type for item in history] == [
        "task.created",
        "plan_accepted",
        "start",
        "begin_verification",
        "verification_passed",
    ]
    assert store.verify_audit_chain(
        "task_001", principal=principal(), authorization_now_epoch=150
    )


def test_stale_version_is_rejected_without_adding_audit_event(tmp_path):
    store = SQLiteTaskStore(tmp_path / "cmx.sqlite3")
    create(store)
    event(store, "task_001", TaskEvent.PLAN_ACCEPTED, version=0)
    before = audit(store)
    with pytest.raises(StaleTaskVersionError):
        event(store, "task_001", TaskEvent.START, version=0)
    assert audit(store) == before
    assert store.get_task(
        "task_001", principal=principal(), authorization_now_epoch=150
    ).state == TaskState.PLANNED


def test_duplicate_task_id_is_scoped_to_tenant(tmp_path):
    store = SQLiteTaskStore(tmp_path / "cmx.sqlite3")
    create(store)
    with pytest.raises(TaskAlreadyExistsError):
        create(store)
    same_id_other_tenant = create(
        store,
        identity=principal(tenant_id="tenant_beta"),
    )
    assert same_id_other_tenant.task_id == "task_001"
    beta_history = audit(store, identity=principal(tenant_id="tenant_beta"))
    alpha_history = audit(store)
    assert beta_history[0].tenant_id == "tenant_beta"
    assert alpha_history[0].tenant_id == "tenant_alpha"


def test_cross_tenant_task_reads_and_transitions_are_not_visible(tmp_path):
    store = SQLiteTaskStore(tmp_path / "cmx.sqlite3")
    create(store)
    beta = principal(tenant_id="tenant_beta")
    assert store.get_task("task_001", principal=beta, authorization_now_epoch=150) is None
    assert audit(store, identity=beta) == []
    assert not store.verify_audit_chain(
        "task_001", principal=beta, authorization_now_epoch=150
    )
    with pytest.raises(TaskNotFoundError):
        event(store, "task_001", TaskEvent.PLAN_ACCEPTED, version=0, identity=beta)


def test_expired_or_unverified_principal_cannot_read_or_write(tmp_path):
    store = SQLiteTaskStore(tmp_path / "cmx.sqlite3")
    with pytest.raises(AuthorizationError) as expired:
        store.create_task(
            TaskRecord(task_id="task_001"),
            principal=principal(expires_at=150),
            occurred_at=150,
            authorization_now_epoch=150,
        )
    assert expired.value.code == "identity_expired"

    with pytest.raises(AuthorizationError) as unverified:
        store.create_task(
            TaskRecord(task_id="task_001"),
            principal=principal(authentication_verified=False),
            occurred_at=150,
            authorization_now_epoch=150,
        )
    assert unverified.value.code == "unauthenticated"
    assert store.get_task(
        "task_001", principal=principal(), authorization_now_epoch=150
    ) is None


def test_missing_store_permission_is_denied(tmp_path):
    store = SQLiteTaskStore(tmp_path / "cmx.sqlite3")
    with pytest.raises(AuthorizationError) as captured:
        store.create_task(
            TaskRecord(task_id="task_001"),
            principal=principal(permissions=frozenset({"tasks:read"})),
            occurred_at=150,
            authorization_now_epoch=150,
        )
    assert captured.value.code == "missing_permission"


def test_audit_events_cannot_be_updated_or_deleted_through_normal_sql(tmp_path):
    path = tmp_path / "cmx.sqlite3"
    store = SQLiteTaskStore(path)
    create(store)
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE cmx_audit_events SET event_type = 'forged' WHERE task_id = ?",
                ("task_001",),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM cmx_audit_events WHERE task_id = ?",
                ("task_001",),
            )
    assert store.verify_audit_chain(
        "task_001", principal=principal(), authorization_now_epoch=150
    )


def test_audit_chain_detects_payload_corruption(tmp_path):
    path = tmp_path / "cmx.sqlite3"
    store = SQLiteTaskStore(path)
    create(store)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER cmx_audit_events_no_update")
        connection.execute(
            "UPDATE cmx_audit_events SET payload_json = ? WHERE task_id = ?",
            ('{"attempts":99,"state":"succeeded","version":0}', "task_001"),
        )
    assert not store.verify_audit_chain(
        "task_001", principal=principal(), authorization_now_epoch=150
    )


def test_state_update_rolls_back_when_audit_append_fails(tmp_path):
    path = tmp_path / "cmx.sqlite3"
    store = SQLiteTaskStore(path)
    create(store)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER block_audit_insert BEFORE INSERT ON cmx_audit_events
            WHEN NEW.event_type != 'task.created'
            BEGIN SELECT RAISE(ABORT, 'blocked for atomicity test'); END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="blocked for atomicity test"):
        event(store, "task_001", TaskEvent.PLAN_ACCEPTED, version=0)
    current = store.get_task(
        "task_001", principal=principal(), authorization_now_epoch=150
    )
    assert current.state == TaskState.CREATED
    assert current.version == 0
    assert len(audit(store)) == 1
    assert store.verify_audit_chain(
        "task_001", principal=principal(), authorization_now_epoch=150
    )


def test_occurrence_timestamp_cannot_be_used_to_extend_expired_identity(tmp_path):
    store = SQLiteTaskStore(tmp_path / "cmx.sqlite3")
    with pytest.raises(AuthorizationError) as captured:
        store.create_task(
            TaskRecord(task_id="task_001"),
            principal=principal(expires_at=200),
            occurred_at=150,
            authorization_now_epoch=200,
        )
    assert captured.value.code == "identity_expired"


def test_out_of_order_audit_timestamp_rolls_back_transition(tmp_path):
    store = SQLiteTaskStore(tmp_path / "cmx.sqlite3")
    create(store)
    with pytest.raises(ValueError, match="must not move backwards"):
        store.apply_event(
            "task_001",
            TaskEvent.PLAN_ACCEPTED,
            principal=principal(),
            expected_version=0,
            occurred_at=149,
            authorization_now_epoch=150,
        )
    current = store.get_task(
        "task_001", principal=principal(), authorization_now_epoch=150
    )
    assert current.state == TaskState.CREATED
    assert current.version == 0
    assert len(audit(store)) == 1


def test_task_row_tampering_is_detected_on_read_and_transition(tmp_path):
    path = tmp_path / "cmx.sqlite3"
    store = SQLiteTaskStore(path)
    create(store)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE cmx_tasks SET state = 'running' WHERE tenant_id = ? AND task_id = ?",
            ("tenant_alpha", "task_001"),
        )
    with pytest.raises(AuditIntegrityError, match="does not match"):
        store.get_task(
            "task_001", principal=principal(), authorization_now_epoch=150
        )
    with pytest.raises(AuditIntegrityError, match="does not match"):
        store.apply_event(
            "task_001",
            TaskEvent.PLAN_ACCEPTED,
            principal=principal(),
            expected_version=0,
            occurred_at=151,
            authorization_now_epoch=150,
        )


def test_new_task_must_start_at_initial_state(tmp_path):
    store = SQLiteTaskStore(tmp_path / "cmx.sqlite3")
    non_initial = TaskRecord(task_id="task_001", state=TaskState.PLANNED)
    with pytest.raises(ValueError, match="must start in created"):
        store.create_task(
            non_initial,
            principal=principal(),
            occurred_at=150,
            authorization_now_epoch=150,
        )
    assert store.get_task(
        "task_001", principal=principal(), authorization_now_epoch=150
    ) is None


def test_audit_event_limit_is_bounded(tmp_path):
    store = SQLiteTaskStore(tmp_path / "cmx.sqlite3")
    create(store)
    with pytest.raises(ValueError, match="between 1 and 1000"):
        store.list_audit_events(
            "task_001", principal=principal(), limit=1001, authorization_now_epoch=150
        )


def test_file_backed_store_rejects_per_connection_memory_database():
    with pytest.raises(ValueError, match="file-backed"):
        SQLiteTaskStore(":memory:")
