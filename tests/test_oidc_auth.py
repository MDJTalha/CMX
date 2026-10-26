import pytest
import jwt

from cmx.oidc_auth import OIDCAuthenticator, OIDCConfigurationError, OIDCIdentityStore
from cmx.security import AuthorizationError, authorize_permissions


ISSUER = "https://identity.example.test"
AUDIENCE = "cmx-api"
JWKS = "https://identity.example.test/.well-known/jwks.json"


class FakeKey:
    key = "test-public-key"


class FakeJwksClient:
    def get_signing_key_from_jwt(self, token):
        if token == "untrusted":
            raise ValueError("bad token")
        return FakeKey()


def claims(**overrides):
    value = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "idp-subject-123",
        "iat": 100,
        "exp": 200,
        "jti": "credential-001",
        "permissions": ["admin:*", "billing:write"],
    }
    value.update(overrides)
    return value


def setup(tmp_path, monkeypatch, token_claims=None, *, require_mfa=False, mfa_acr=None):
    store = OIDCIdentityStore(tmp_path / "oidc.sqlite3")
    store.grant(
        issuer=ISSUER, subject="idp-subject-123", tenant_id="tenant_alpha",
        permissions={"workflow:read", "workflow:execute"}, now_epoch=90,
    )
    current = claims() if token_claims is None else token_claims
    monkeypatch.setattr(jwt, "decode", lambda *args, **kwargs: current)
    return OIDCAuthenticator(
        issuer=ISSUER, audience=AUDIENCE, jwks_url=JWKS, identity_store=store,
        require_mfa=require_mfa, mfa_acr=mfa_acr, jwks_client=FakeJwksClient(),
    ), store


def test_valid_oidc_token_uses_server_side_permissions_not_token_claims(tmp_path, monkeypatch):
    auth, _ = setup(tmp_path, monkeypatch)
    principal = auth.authenticate("signed-token", tenant_id="tenant_alpha", now_epoch=150)
    assert principal.authentication_verified is True
    assert principal.authentication_method == "oidc"
    assert principal.permissions == frozenset({"workflow:read", "workflow:execute"})
    assert principal.subject_id.startswith("oidc_")
    authorize_permissions(
        principal, required_permissions={"workflow:execute"},
        resource_tenant_id="tenant_alpha", now_epoch=150,
    )


def test_membership_is_tenant_scoped(tmp_path, monkeypatch):
    auth, _ = setup(tmp_path, monkeypatch)
    with pytest.raises(AuthorizationError) as error:
        auth.authenticate("signed-token", tenant_id="tenant_other", now_epoch=150)
    assert error.value.code == "membership_required"


def test_revoked_membership_fails_closed(tmp_path, monkeypatch):
    auth, store = setup(tmp_path, monkeypatch)
    assert store.revoke(
        issuer=ISSUER, subject="idp-subject-123", tenant_id="tenant_alpha", now_epoch=120,
    )
    with pytest.raises(AuthorizationError) as error:
        auth.authenticate("signed-token", tenant_id="tenant_alpha", now_epoch=150)
    assert error.value.code == "membership_required"


def test_mfa_policy_requires_signed_mfa_claim(tmp_path, monkeypatch):
    auth, _ = setup(tmp_path, monkeypatch, require_mfa=True)
    with pytest.raises(AuthorizationError) as error:
        auth.authenticate("signed-token", tenant_id="tenant_alpha", now_epoch=150)
    assert error.value.code == "mfa_required"


def test_mfa_can_be_satisfied_by_configured_acr(tmp_path, monkeypatch):
    auth, _ = setup(
        tmp_path, monkeypatch, token_claims=claims(acr="urn:example:loa:2"),
        require_mfa=True, mfa_acr="urn:example:loa:2",
    )
    principal = auth.authenticate("signed-token", tenant_id="tenant_alpha", now_epoch=150)
    assert principal.authentication_verified is True


def test_expired_token_is_rejected_even_if_test_decoder_returns_claims(tmp_path, monkeypatch):
    auth, _ = setup(tmp_path, monkeypatch, token_claims=claims(exp=140))
    with pytest.raises(AuthorizationError) as error:
        auth.authenticate("signed-token", tenant_id="tenant_alpha", now_epoch=150)
    assert error.value.code == "invalid_credential"


def test_bad_signature_or_jwks_failure_is_normalized(tmp_path, monkeypatch):
    auth, _ = setup(tmp_path, monkeypatch)
    with pytest.raises(AuthorizationError) as error:
        auth.authenticate("untrusted", tenant_id="tenant_alpha", now_epoch=150)
    assert error.value.code == "invalid_credential"


def test_unsafe_oidc_configuration_is_rejected(tmp_path):
    store = OIDCIdentityStore(tmp_path / "oidc.sqlite3")
    with pytest.raises(OIDCConfigurationError):
        OIDCAuthenticator(
            issuer="http://identity.example.test", audience=AUDIENCE,
            jwks_url=JWKS, identity_store=store,
        )


def test_membership_grants_reject_invalid_permission_patterns(tmp_path):
    store = OIDCIdentityStore(tmp_path / "oidc.sqlite3")
    with pytest.raises(ValueError):
        store.grant(
            issuer=ISSUER, subject="idp-subject-123", tenant_id="tenant_alpha",
            permissions={"Admin:*"}, now_epoch=100,
        )
