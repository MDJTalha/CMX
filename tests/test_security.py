import pytest
from pydantic import ValidationError

from cmx.security import AuthorizationError, PrincipalContext, authorize_permissions


def principal(**overrides) -> PrincipalContext:
    values = {
        "subject_id": "user_001",
        "tenant_id": "tenant_alpha",
        "issuer": "https://identity.cmx.test",
        "permissions": frozenset({"assessment:read", "assessment:write"}),
        "issued_at": 100,
        "expires_at": 200,
        "authentication_verified": True,
        "authentication_method": "test",
        "credential_id": "cred_001",
    }
    values.update(overrides)
    return PrincipalContext(**values)


def authorize(context, permissions=frozenset({"assessment:read"}), tenant="tenant_alpha", now=150):
    return authorize_permissions(
        context,
        required_permissions=permissions,
        resource_tenant_id=tenant,
        now_epoch=now,
    )


def test_verified_context_with_required_permission_is_allowed():
    authorize(principal())


def test_missing_context_is_denied():
    with pytest.raises(AuthorizationError) as captured:
        authorize(None)
    assert captured.value.code == "unauthenticated"


def test_unverified_identity_context_is_denied():
    with pytest.raises(AuthorizationError) as captured:
        authorize(principal(authentication_verified=False))
    assert captured.value.code == "unauthenticated"


def test_expired_identity_context_is_denied():
    with pytest.raises(AuthorizationError) as captured:
        authorize(principal(), now=200)
    assert captured.value.code == "identity_expired"


def test_not_yet_valid_identity_context_is_denied():
    with pytest.raises(AuthorizationError) as captured:
        authorize(principal(), now=99)
    assert captured.value.code == "identity_not_yet_valid"


def test_cross_tenant_access_is_denied_even_with_permission():
    with pytest.raises(AuthorizationError) as captured:
        authorize(principal(), tenant="tenant_beta")
    assert captured.value.code == "tenant_mismatch"


def test_every_required_permission_must_be_present():
    with pytest.raises(AuthorizationError) as captured:
        authorize(principal(permissions=frozenset({"assessment:read"})),
                  permissions=frozenset({"assessment:read", "assessment:write"}))
    assert captured.value.code == "missing_permission"


def test_invalid_principal_permission_format_is_rejected():
    with pytest.raises(ValidationError, match="permission format"):
        principal(permissions=frozenset({"Admin:*"}))


def test_invalid_required_permission_policy_fails_closed():
    with pytest.raises(AuthorizationError) as captured:
        authorize(principal(), permissions=frozenset({"Admin:*"}))
    assert captured.value.code == "invalid_policy"


def test_invalid_expiry_order_is_rejected():
    with pytest.raises(ValidationError, match="later than issued_at"):
        principal(issued_at=200, expires_at=200)


def test_principal_context_is_immutable():
    context = principal()
    with pytest.raises(ValidationError):
        context.tenant_id = "tenant_beta"


def test_unauthorized_error_does_not_disclose_missing_permission_names():
    with pytest.raises(AuthorizationError) as captured:
        authorize(principal(permissions=frozenset()),
                  permissions=frozenset({"payroll:admin"}))
    assert "payroll:admin" not in str(captured.value)
