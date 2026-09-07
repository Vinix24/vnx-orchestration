#!/usr/bin/env python3
"""Tests for scripts/lib/forge_check_run.py (Golf B, B2a).

Covers the client layer only — there is deliberately NO gate knowledge here
(no verdict mapping, no "which conclusion means blocked"): that is B2b.

Mocking boundary, per dispatch: only ``subprocess.run`` (for ``security``)
and the HTTP seam (``_api_request``) are mocked. The JWT construction itself
is NEVER mocked — ``test_app_jwt_*`` builds a real RS256 token with a
throwaway RSA keypair generated in the test and verifies it with the matching
public key.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import jwt
import pytest
import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import forge_check_run as fcr  # noqa: E402
import forge_protection_drift as fpd  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_token_cache():
    """The token cache is module-level state; no test may inherit another's."""
    fcr.reset_token_cache()
    yield
    fcr.reset_token_cache()


@pytest.fixture(scope="module")
def rsa_keypair() -> Tuple[str, str]:
    """A throwaway RSA keypair as (private PEM, public PEM)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    return private_pem, public_pem


def _base_protection_doc(**overrides: Any) -> Dict[str, Any]:
    """A minimal schema-valid branch_protection.yaml document."""
    doc: Dict[str, Any] = {
        "branch": "main",
        "required_status_checks": {
            "strict": False,
            "checks": [{"context": "Profile A", "app_id": 15368}],
        },
        "pending_checks": [],
        "required_pull_request_reviews": {
            "required_approving_review_count": 0,
            "dismiss_stale_reviews": False,
            "require_code_owner_reviews": False,
            "require_last_push_approval": False,
        },
        "enforce_admins": True,
        "required_signatures": False,
        "required_linear_history": False,
        "allow_force_pushes": False,
        "allow_deletions": False,
        "allow_fork_syncing": False,
        "block_creations": False,
        "lock_branch": False,
        "required_conversation_resolution": False,
        "restrictions": None,
        "repo": {"allow_auto_merge": False},
        "rulesets": [],
    }
    doc.update(overrides)
    return doc


def _write_yaml(tmp_path: Path, doc: Dict[str, Any], name: str = "bp.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


class _FakeCompleted:
    """Stand-in for subprocess.CompletedProcess from `security`."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _security_returning(
    monkeypatch: pytest.MonkeyPatch, per_service: Dict[str, _FakeCompleted]
) -> List[List[str]]:
    """Mock ``subprocess.run`` for `security`, keyed by the -s service name."""
    calls: List[List[str]] = []

    def fake_run(argv, **kwargs):  # noqa: ANN001
        calls.append(list(argv))
        service = argv[argv.index("-s") + 1]
        if service not in per_service:
            raise AssertionError(f"unexpected keychain service: {service}")
        return per_service[service]

    monkeypatch.setattr(fcr.subprocess, "run", fake_run)
    return calls


def _install_http(
    monkeypatch: pytest.MonkeyPatch, responses: List[Tuple[int, Any]]
) -> List[Dict[str, Any]]:
    """Mock the HTTP seam. Records each request; pops responses in order."""
    recorded: List[Dict[str, Any]] = []
    queue = list(responses)

    def fake_request(method, url, *, headers, payload=None, timeout=30):  # noqa: ANN001
        recorded.append(
            {"method": method, "url": url, "headers": dict(headers), "payload": payload}
        )
        if not queue:
            raise AssertionError(f"unexpected extra HTTP call: {method} {url}")
        status, body = queue.pop(0)
        return status, body if isinstance(body, str) else json.dumps(body)

    monkeypatch.setattr(fcr, "_api_request", fake_request)
    return recorded


def _token_body(expires_in_minutes: int = 60, token: str = "ghs_installtoken") -> Dict[str, Any]:
    expires = datetime.now(timezone.utc) + timedelta(minutes=expires_in_minutes)
    return {"token": token, "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ")}


# ---------------------------------------------------------------------------
# load_app_config — the YAML side
# ---------------------------------------------------------------------------


def test_load_app_config_reads_slug_and_app_id(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, _base_protection_doc(app={"slug": "vnx-gate", "app_id": 987654}))
    config = fcr.load_app_config(path)
    assert config.slug == "vnx-gate"
    assert config.app_id == 987654


def test_load_app_config_null_app_id_raises_with_runbook_reference(tmp_path: Path) -> None:
    """The operator has not filled app_id in yet: loud, with where to go."""
    path = _write_yaml(tmp_path, _base_protection_doc(app={"slug": "vnx-gate", "app_id": None}))
    with pytest.raises(fcr.ForgeAppConfigError) as excinfo:
        fcr.load_app_config(path)
    message = str(excinfo.value)
    assert "app_id ontbreekt" in message
    assert "scripts/forge/branch_protection.yaml" in message
    assert fcr.RUNBOOK_PATH in message


def test_load_app_config_missing_app_block_raises(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, _base_protection_doc())
    with pytest.raises(fcr.ForgeAppConfigError) as excinfo:
        fcr.load_app_config(path)
    assert fcr.RUNBOOK_PATH in str(excinfo.value)


def test_load_app_config_rejects_non_integer_app_id(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, _base_protection_doc(app={"slug": "vnx-gate", "app_id": "987654"}))
    with pytest.raises(fcr.ForgeAppConfigError):
        fcr.load_app_config(path)


def test_load_app_config_rejects_empty_slug(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, _base_protection_doc(app={"slug": "", "app_id": 987654}))
    with pytest.raises(fcr.ForgeAppConfigError):
        fcr.load_app_config(path)


def test_load_app_config_unreadable_path_raises(tmp_path: Path) -> None:
    with pytest.raises(fcr.ForgeAppConfigError):
        fcr.load_app_config(tmp_path / "does-not-exist.yaml")


def test_repo_yaml_carries_the_app_block() -> None:
    """The shipped YAML has the app: block B2b will read."""
    path = VNX_ROOT / "scripts" / "forge" / "branch_protection.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "app" in doc, "scripts/forge/branch_protection.yaml mist het app:-blok"
    assert doc["app"]["slug"] == "vnx-gate"
    assert "app_id" in doc["app"]


# ---------------------------------------------------------------------------
# The app: block must not read as drift to B1's schema reader / comparator
# ---------------------------------------------------------------------------


def test_app_block_parses_under_b1_schema_reader() -> None:
    """B1 refuses unknown top-level keys; `app` must be an accepted one."""
    doc = _base_protection_doc(app={"slug": "vnx-gate", "app_id": None})
    config = fpd.parse_protection_config(yaml.safe_dump(doc, sort_keys=False))
    assert config.branch == "main"


def test_app_block_triggers_no_drift() -> None:
    """Same config with and without `app:` normalizes identically."""
    without = fpd.parse_protection_config(yaml.safe_dump(_base_protection_doc(), sort_keys=False))
    with_app = fpd.parse_protection_config(
        yaml.safe_dump(
            _base_protection_doc(app={"slug": "vnx-gate", "app_id": 987654}), sort_keys=False
        )
    )
    diffs = fpd.compare(fpd.to_normalized_dict(without), fpd.to_normalized_dict(with_app))
    assert diffs == [], f"app: block leaked into the comparator: {diffs}"


def test_app_block_is_not_a_weakening() -> None:
    without = fpd.parse_protection_config(yaml.safe_dump(_base_protection_doc(), sort_keys=False))
    with_app = fpd.parse_protection_config(
        yaml.safe_dump(
            _base_protection_doc(app={"slug": "vnx-gate", "app_id": 987654}), sort_keys=False
        )
    )
    weakening, fields = fpd.is_weakening(
        fpd.to_normalized_dict(without), fpd.to_normalized_dict(with_app)
    )
    assert weakening is False
    assert fields == []


def test_b1_still_refuses_a_genuinely_unknown_key() -> None:
    """Accepting `app` must not have opened the schema to anything at all."""
    doc = _base_protection_doc(vnx_typo_field=True)
    with pytest.raises(fpd.ProtectionConfigError):
        fpd.parse_protection_config(yaml.safe_dump(doc, sort_keys=False))


def test_shipped_yaml_parses_and_applies_clean(tmp_path: Path) -> None:
    """The real file still round-trips through B1 with the app: block on it."""
    path = VNX_ROOT / "scripts" / "forge" / "branch_protection.yaml"
    config = fpd.load_protection_config(path)
    assert config.branch == "main"
    assert config.checks


# ---------------------------------------------------------------------------
# Keychain reads — loud, never an empty string
# ---------------------------------------------------------------------------


def test_read_private_key_returns_the_pem(monkeypatch: pytest.MonkeyPatch) -> None:
    pem = "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n"
    calls = _security_returning(
        monkeypatch, {fcr.KEYCHAIN_PRIVATE_KEY_SERVICE: _FakeCompleted(0, stdout=pem)}
    )
    assert fcr.read_private_key() == pem.strip()
    assert calls[0][:2] == ["security", "find-generic-password"]
    assert "-w" in calls[0]


def test_read_private_key_item_missing_is_loud_with_recovery_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`security` exit 44 = item not in the keychain."""
    _security_returning(
        monkeypatch,
        {
            fcr.KEYCHAIN_PRIVATE_KEY_SERVICE: _FakeCompleted(
                44, stderr="SecKeychainSearchCopyNext: The specified item could not be found"
            )
        },
    )
    with pytest.raises(fcr.ForgeKeychainError) as excinfo:
        fcr.read_private_key()
    message = str(excinfo.value)
    assert "security add-generic-password" in message
    assert fcr.KEYCHAIN_PRIVATE_KEY_SERVICE in message
    assert fcr.RUNBOOK_PATH in message


def test_read_private_key_locked_keychain_is_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit 51 = user interaction not allowed, i.e. a locked keychain."""
    _security_returning(
        monkeypatch,
        {
            fcr.KEYCHAIN_PRIVATE_KEY_SERVICE: _FakeCompleted(
                51, stderr="User interaction is not allowed."
            )
        },
    )
    with pytest.raises(fcr.ForgeKeychainError) as excinfo:
        fcr.read_private_key()
    message = str(excinfo.value)
    assert "security unlock-keychain" in message
    assert "security add-generic-password" in message


def test_read_private_key_empty_output_is_loud_not_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The send_digest_email soft-fail (return "") is exactly what must NOT happen."""
    _security_returning(
        monkeypatch, {fcr.KEYCHAIN_PRIVATE_KEY_SERVICE: _FakeCompleted(0, stdout="   \n")}
    )
    with pytest.raises(fcr.ForgeKeychainError) as excinfo:
        fcr.read_private_key()
    assert "leeg" in str(excinfo.value).lower()
    assert "security add-generic-password" in str(excinfo.value)


def test_read_private_key_missing_security_binary_is_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv, **kwargs):  # noqa: ANN001
        raise FileNotFoundError("security")

    monkeypatch.setattr(fcr.subprocess, "run", fake_run)
    with pytest.raises(fcr.ForgeKeychainError) as excinfo:
        fcr.read_private_key()
    assert "security" in str(excinfo.value)


def test_read_private_key_timeout_is_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv, **kwargs):  # noqa: ANN001
        raise subprocess.TimeoutExpired(cmd="security", timeout=5)

    monkeypatch.setattr(fcr.subprocess, "run", fake_run)
    with pytest.raises(fcr.ForgeKeychainError):
        fcr.read_private_key()


def test_read_installation_id_returns_the_value(monkeypatch: pytest.MonkeyPatch) -> None:
    _security_returning(
        monkeypatch, {fcr.KEYCHAIN_INSTALLATION_ID_SERVICE: _FakeCompleted(0, stdout="12345678\n")}
    )
    assert fcr.read_installation_id() == "12345678"


def test_read_installation_id_missing_is_loud_with_its_own_service_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _security_returning(
        monkeypatch, {fcr.KEYCHAIN_INSTALLATION_ID_SERVICE: _FakeCompleted(44)}
    )
    with pytest.raises(fcr.ForgeKeychainError) as excinfo:
        fcr.read_installation_id()
    message = str(excinfo.value)
    assert fcr.KEYCHAIN_INSTALLATION_ID_SERVICE in message
    assert "security add-generic-password" in message


def test_read_installation_id_rejects_non_numeric(monkeypatch: pytest.MonkeyPatch) -> None:
    _security_returning(
        monkeypatch,
        {fcr.KEYCHAIN_INSTALLATION_ID_SERVICE: _FakeCompleted(0, stdout="not-an-id\n")},
    )
    with pytest.raises(fcr.ForgeKeychainError):
        fcr.read_installation_id()


# ---------------------------------------------------------------------------
# JWT construction — never mocked
# ---------------------------------------------------------------------------


def _b64url_decode(segment: str) -> bytes:
    """Decode one JWT segment, restoring the padding a JWT strips."""
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _load_public_key(public_pem: str):
    return serialization.load_pem_public_key(public_pem.encode("utf-8"))


def _verify_rs256(public_pem: str, signing_input: str, signature: bytes) -> None:
    """Check an RS256 signature with cryptography, raising InvalidSignature."""
    _load_public_key(public_pem).verify(
        signature, signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
    )


def test_app_jwt_carries_iss_and_a_bounded_exp(rsa_keypair: Tuple[str, str]) -> None:
    private_pem, public_pem = rsa_keypair
    before = datetime.now(timezone.utc)
    token = fcr.build_app_jwt(987654, private_pem)
    claims = jwt.decode(token, public_pem, algorithms=["RS256"], issuer="987654")

    assert claims["iss"] == "987654"
    exp = datetime.fromtimestamp(claims["exp"], tz=timezone.utc)
    iat = datetime.fromtimestamp(claims["iat"], tz=timezone.utc)
    assert exp <= before + timedelta(minutes=10), "exp must stay inside GitHub's 10 minute ceiling"
    assert exp > before + timedelta(minutes=8)
    assert iat <= before, "iat must be backdated against clock skew"
    assert iat >= before - timedelta(seconds=120)


def test_app_jwt_uses_rs256(rsa_keypair: Tuple[str, str]) -> None:
    private_pem, _ = rsa_keypair
    header = jwt.get_unverified_header(fcr.build_app_jwt(987654, private_pem))
    assert header["alg"] == "RS256"


def test_app_jwt_rejects_a_malformed_private_key() -> None:
    with pytest.raises(fcr.ForgeCheckRunError):
        fcr.build_app_jwt(987654, "-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----")


def test_app_jwt_verification_fails_against_a_foreign_public_key(
    rsa_keypair: Tuple[str, str],
) -> None:
    private_pem, _ = rsa_keypair
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_public = (
        other.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    token = fcr.build_app_jwt(987654, private_pem)
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, other_public, algorithms=["RS256"])


def test_app_jwt_signature_verifies_with_cryptography_alone(
    rsa_keypair: Tuple[str, str],
) -> None:
    """The signature is checked WITHOUT PyJWT, and the claims are read raw.

    ``test_app_jwt_carries_iss_and_a_bounded_exp`` signs and verifies through
    the same library, so it proves the round trip and not the artefact. Here the
    token is split by hand and the signature checked against the public key with
    cryptography's primitives: what GitHub will do, minus GitHub.
    """
    private_pem, public_pem = rsa_keypair
    before = datetime.now(timezone.utc)
    token = fcr.build_app_jwt(987654, private_pem)

    header_b64, payload_b64, signature_b64 = token.split(".")
    _verify_rs256(public_pem, f"{header_b64}.{payload_b64}", _b64url_decode(signature_b64))

    assert json.loads(_b64url_decode(header_b64))["alg"] == "RS256"
    claims = json.loads(_b64url_decode(payload_b64))
    assert claims["iss"] == "987654", "iss must be the app id, as a string"
    exp = datetime.fromtimestamp(claims["exp"], tz=timezone.utc)
    assert exp <= before + timedelta(minutes=10), "GitHub rejects an exp beyond 10 minutes"
    assert exp > before


def test_app_jwt_signature_breaks_when_the_payload_is_changed(
    rsa_keypair: Tuple[str, str],
) -> None:
    """Re-issuing the same token under another app id must not validate.

    This is the negative half of the test above: if it stayed green after the
    payload changed, the "verification" there would be checking nothing.
    """
    private_pem, public_pem = rsa_keypair
    header_b64, payload_b64, signature_b64 = fcr.build_app_jwt(987654, private_pem).split(".")

    claims = json.loads(_b64url_decode(payload_b64))
    assert claims["iss"] == "987654"
    claims["iss"] = "111111"
    forged_payload = _b64url_encode(json.dumps(claims).encode("utf-8"))
    forged = f"{header_b64}.{forged_payload}.{signature_b64}"

    with pytest.raises(InvalidSignature):
        _verify_rs256(public_pem, f"{header_b64}.{forged_payload}", _b64url_decode(signature_b64))
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(forged, public_pem, algorithms=["RS256"])


def test_app_jwt_names_the_install_when_pyjwt_is_absent(
    monkeypatch: pytest.MonkeyPatch, rsa_keypair: Tuple[str, str]
) -> None:
    """A missing PyJWT fails like every other credential gap here: loud, with the repair."""
    private_pem, _ = rsa_keypair
    # A None entry in sys.modules is what the import machinery itself uses to
    # mark a module as unimportable, so `import jwt` inside build_app_jwt takes
    # the real ImportError path rather than a stubbed one.
    monkeypatch.setitem(sys.modules, "jwt", None)

    with pytest.raises(fcr.ForgeCheckRunError) as excinfo:
        fcr.build_app_jwt(987654, private_pem)
    assert "pyjwt[crypto]" in str(excinfo.value)


def test_app_jwt_names_the_extra_when_rs256_has_no_backend(
    monkeypatch: pytest.MonkeyPatch, rsa_keypair: Tuple[str, str]
) -> None:
    """Bare PyJWT imports fine and only fails at signing — with its own message."""
    private_pem, _ = rsa_keypair

    def _no_asymmetric_backend(*args: Any, **kwargs: Any) -> str:
        raise NotImplementedError("Algorithm 'RS256' could not be found.")

    monkeypatch.setattr(jwt, "encode", _no_asymmetric_backend)

    with pytest.raises(fcr.ForgeCheckRunError) as excinfo:
        fcr.build_app_jwt(987654, private_pem)
    message = str(excinfo.value)
    assert "pyjwt[crypto]" in message
    assert "RS256" in message


# ---------------------------------------------------------------------------
# installation_token — exchange + in-memory cache
# ---------------------------------------------------------------------------


def _wire_credentials(monkeypatch: pytest.MonkeyPatch, private_pem: str) -> None:
    monkeypatch.setattr(fcr, "read_private_key", lambda: private_pem)
    monkeypatch.setattr(fcr, "read_installation_id", lambda: "12345678")
    monkeypatch.setattr(fcr, "load_app_config", lambda path=None: fcr.AppConfig("vnx-gate", 987654))


def test_installation_token_exchanges_the_jwt(
    monkeypatch: pytest.MonkeyPatch, rsa_keypair: Tuple[str, str]
) -> None:
    private_pem, public_pem = rsa_keypair
    _wire_credentials(monkeypatch, private_pem)
    recorded = _install_http(monkeypatch, [(201, _token_body())])

    assert fcr.installation_token() == "ghs_installtoken"
    assert len(recorded) == 1
    assert recorded[0]["method"] == "POST"
    assert recorded[0]["url"].endswith("/app/installations/12345678/access_tokens")

    sent = recorded[0]["headers"]["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(sent, public_pem, algorithms=["RS256"], issuer="987654")
    assert claims["iss"] == "987654"


def test_installation_token_caches_within_validity(
    monkeypatch: pytest.MonkeyPatch, rsa_keypair: Tuple[str, str]
) -> None:
    private_pem, _ = rsa_keypair
    _wire_credentials(monkeypatch, private_pem)
    recorded = _install_http(monkeypatch, [(201, _token_body(expires_in_minutes=60))])

    first = fcr.installation_token()
    second = fcr.installation_token()

    assert first == second == "ghs_installtoken"
    assert len(recorded) == 1, "second call inside validity must not hit the API"


def test_installation_token_refreshes_five_minutes_before_expiry(
    monkeypatch: pytest.MonkeyPatch, rsa_keypair: Tuple[str, str]
) -> None:
    private_pem, _ = rsa_keypair
    _wire_credentials(monkeypatch, private_pem)
    expires = datetime.now(timezone.utc) + timedelta(minutes=60)
    body = {"token": "ghs_first", "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ")}
    recorded = _install_http(
        monkeypatch, [(201, body), (201, _token_body(expires_in_minutes=60, token="ghs_second"))]
    )

    assert fcr.installation_token() == "ghs_first"
    # Still cached one second before the refresh margin opens.
    just_inside = expires - timedelta(seconds=fcr.TOKEN_REFRESH_MARGIN_SECONDS + 1)
    assert fcr.installation_token(now=just_inside) == "ghs_first"
    assert len(recorded) == 1

    # At exactly expires_at minus the margin the cached token is spent.
    at_margin = expires - timedelta(seconds=fcr.TOKEN_REFRESH_MARGIN_SECONDS)
    assert fcr.installation_token(now=at_margin) == "ghs_second"
    assert len(recorded) == 2


def test_installation_token_401_is_loud_and_does_not_loop(
    monkeypatch: pytest.MonkeyPatch, rsa_keypair: Tuple[str, str]
) -> None:
    private_pem, _ = rsa_keypair
    _wire_credentials(monkeypatch, private_pem)
    recorded = _install_http(
        monkeypatch, [(401, {"message": "A JSON web token could not be decoded"})]
    )

    with pytest.raises(fcr.ForgeAPIError) as excinfo:
        fcr.installation_token()

    assert excinfo.value.status == 401
    assert "could not be decoded" in str(excinfo.value)
    assert len(recorded) == 1, "a 401 on a fresh JWT must not be retried"


def test_installation_token_unparseable_body_is_loud(
    monkeypatch: pytest.MonkeyPatch, rsa_keypair: Tuple[str, str]
) -> None:
    private_pem, _ = rsa_keypair
    _wire_credentials(monkeypatch, private_pem)
    _install_http(monkeypatch, [(201, "<html>gateway</html>")])
    with pytest.raises(fcr.ForgeAPIError):
        fcr.installation_token()


def test_installation_token_missing_token_field_is_loud(
    monkeypatch: pytest.MonkeyPatch, rsa_keypair: Tuple[str, str]
) -> None:
    private_pem, _ = rsa_keypair
    _wire_credentials(monkeypatch, private_pem)
    _install_http(monkeypatch, [(201, {"expires_at": "2026-09-07T12:00:00Z"})])
    with pytest.raises(fcr.ForgeAPIError):
        fcr.installation_token()


def test_installation_token_failed_exchange_is_not_cached(
    monkeypatch: pytest.MonkeyPatch, rsa_keypair: Tuple[str, str]
) -> None:
    private_pem, _ = rsa_keypair
    _wire_credentials(monkeypatch, private_pem)
    _install_http(monkeypatch, [(500, {"message": "boom"}), (201, _token_body())])

    with pytest.raises(fcr.ForgeAPIError):
        fcr.installation_token()
    assert fcr.installation_token() == "ghs_installtoken"


# ---------------------------------------------------------------------------
# publish_check_run — the primitive. No verdict interpretation lives here.
# ---------------------------------------------------------------------------


def test_publish_check_run_posts_the_expected_body(
    monkeypatch: pytest.MonkeyPatch, rsa_keypair: Tuple[str, str]
) -> None:
    private_pem, _ = rsa_keypair
    _wire_credentials(monkeypatch, private_pem)
    monkeypatch.setattr(fcr, "resolve_owner_repo", lambda project_root=None: "Vinix24/vnx-orchestration")
    created = {"id": 42, "conclusion": "success"}
    recorded = _install_http(monkeypatch, [(201, _token_body()), (201, created)])

    result = fcr.publish_check_run(
        "a" * 40, "vnx-gate/review", "success", "12 findings, 0 blocking"
    )

    assert result == created
    post = recorded[1]
    assert post["method"] == "POST"
    assert post["url"].endswith("/repos/Vinix24/vnx-orchestration/check-runs")
    body = json.loads(post["payload"])
    assert body["head_sha"] == "a" * 40
    assert body["name"] == "vnx-gate/review"
    assert body["status"] == "completed"
    assert body["conclusion"] == "success"
    assert body["output"]["summary"] == "12 findings, 0 blocking"
    assert post["headers"]["Authorization"] == "Bearer ghs_installtoken"


def test_publish_check_run_includes_details_url_when_given(
    monkeypatch: pytest.MonkeyPatch, rsa_keypair: Tuple[str, str]
) -> None:
    private_pem, _ = rsa_keypair
    _wire_credentials(monkeypatch, private_pem)
    monkeypatch.setattr(fcr, "resolve_owner_repo", lambda project_root=None: "o/r")
    recorded = _install_http(monkeypatch, [(201, _token_body()), (201, {"id": 1})])

    fcr.publish_check_run(
        "b" * 40, "vnx-gate/review", "failure", "blocked", details_url="https://example.test/run/1"
    )
    body = json.loads(recorded[1]["payload"])
    assert body["details_url"] == "https://example.test/run/1"


def test_publish_check_run_omits_details_url_when_absent(
    monkeypatch: pytest.MonkeyPatch, rsa_keypair: Tuple[str, str]
) -> None:
    private_pem, _ = rsa_keypair
    _wire_credentials(monkeypatch, private_pem)
    monkeypatch.setattr(fcr, "resolve_owner_repo", lambda project_root=None: "o/r")
    recorded = _install_http(monkeypatch, [(201, _token_body()), (201, {"id": 1})])

    fcr.publish_check_run("c" * 40, "vnx-gate/review", "success", "ok")
    assert "details_url" not in json.loads(recorded[1]["payload"])


@pytest.mark.parametrize(
    "conclusion", ["success", "failure", "neutral", "cancelled", "action_required", "whatever_b2b_sends"]
)
def test_publish_check_run_does_not_interpret_the_conclusion(
    monkeypatch: pytest.MonkeyPatch, rsa_keypair: Tuple[str, str], conclusion: str
) -> None:
    """Client layer, zero gate knowledge: it forwards, it does not judge (B2b)."""
    private_pem, _ = rsa_keypair
    _wire_credentials(monkeypatch, private_pem)
    monkeypatch.setattr(fcr, "resolve_owner_repo", lambda project_root=None: "o/r")
    recorded = _install_http(monkeypatch, [(201, _token_body()), (201, {"id": 1})])

    fcr.publish_check_run("d" * 40, "vnx-gate/review", conclusion, "summary text")
    assert json.loads(recorded[1]["payload"])["conclusion"] == conclusion


def test_publish_check_run_422_raises_with_the_body(
    monkeypatch: pytest.MonkeyPatch, rsa_keypair: Tuple[str, str]
) -> None:
    private_pem, _ = rsa_keypair
    _wire_credentials(monkeypatch, private_pem)
    monkeypatch.setattr(fcr, "resolve_owner_repo", lambda project_root=None: "o/r")
    _install_http(
        monkeypatch,
        [
            (201, _token_body()),
            (422, {"message": "Validation Failed", "errors": [{"field": "head_sha"}]}),
        ],
    )

    with pytest.raises(fcr.ForgeAPIError) as excinfo:
        fcr.publish_check_run("e" * 40, "vnx-gate/review", "success", "ok")

    assert excinfo.value.status == 422
    assert "Validation Failed" in str(excinfo.value)
    assert "422" in str(excinfo.value)


def test_publish_check_run_rejects_an_empty_head_sha(
    monkeypatch: pytest.MonkeyPatch, rsa_keypair: Tuple[str, str]
) -> None:
    private_pem, _ = rsa_keypair
    _wire_credentials(monkeypatch, private_pem)
    monkeypatch.setattr(fcr, "resolve_owner_repo", lambda project_root=None: "o/r")
    _install_http(monkeypatch, [])
    with pytest.raises(fcr.ForgeCheckRunError):
        fcr.publish_check_run("", "vnx-gate/review", "success", "ok")


def test_publish_check_run_rejects_an_empty_name(
    monkeypatch: pytest.MonkeyPatch, rsa_keypair: Tuple[str, str]
) -> None:
    private_pem, _ = rsa_keypair
    _wire_credentials(monkeypatch, private_pem)
    monkeypatch.setattr(fcr, "resolve_owner_repo", lambda project_root=None: "o/r")
    _install_http(monkeypatch, [])
    with pytest.raises(fcr.ForgeCheckRunError):
        fcr.publish_check_run("f" * 40, "", "success", "ok")


def test_publish_check_run_401_on_a_cached_token_refreshes_once(
    monkeypatch: pytest.MonkeyPatch, rsa_keypair: Tuple[str, str]
) -> None:
    """A cached token can expire server-side early: one refresh, one retry."""
    private_pem, _ = rsa_keypair
    _wire_credentials(monkeypatch, private_pem)
    monkeypatch.setattr(fcr, "resolve_owner_repo", lambda project_root=None: "o/r")
    fcr.installation_token  # noqa: B018  (documents the cache is primed below)
    recorded = _install_http(
        monkeypatch,
        [
            (201, _token_body(token="ghs_stale")),
            (201, {"id": 7}),
            (401, {"message": "Bad credentials"}),
            (201, _token_body(token="ghs_fresh")),
            (201, {"id": 8}),
        ],
    )

    assert fcr.publish_check_run("a" * 40, "n", "success", "s") == {"id": 7}
    assert fcr.publish_check_run("b" * 40, "n", "success", "s") == {"id": 8}
    assert recorded[-1]["headers"]["Authorization"] == "Bearer ghs_fresh"
    assert len(recorded) == 5


def test_publish_check_run_401_on_a_fresh_token_is_loud(
    monkeypatch: pytest.MonkeyPatch, rsa_keypair: Tuple[str, str]
) -> None:
    """No cache to blame: fail loudly instead of looping on refresh."""
    private_pem, _ = rsa_keypair
    _wire_credentials(monkeypatch, private_pem)
    monkeypatch.setattr(fcr, "resolve_owner_repo", lambda project_root=None: "o/r")
    recorded = _install_http(
        monkeypatch, [(201, _token_body()), (401, {"message": "Bad credentials"})]
    )

    with pytest.raises(fcr.ForgeAPIError) as excinfo:
        fcr.publish_check_run("a" * 40, "n", "success", "s")

    assert excinfo.value.status == 401
    assert len(recorded) == 2, "must not re-mint and retry in a loop"


# ---------------------------------------------------------------------------
# owner/repo resolution
# ---------------------------------------------------------------------------


def test_resolve_owner_repo_reads_the_git_remote() -> None:
    assert fcr.resolve_owner_repo(VNX_ROOT) == "Vinix24/vnx-orchestration"


def test_resolve_owner_repo_raises_on_a_non_github_remote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(fcr, "_owner_repo_from_remote", lambda project_root: None)
    with pytest.raises(fcr.ForgeCheckRunError) as excinfo:
        fcr.resolve_owner_repo(tmp_path)
    assert "origin" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The transport itself carries no gate vocabulary
# ---------------------------------------------------------------------------


def test_module_has_no_gate_knowledge() -> None:
    """B2a is the client layer; verdict vocabulary belongs to B2b."""
    source = (VNX_ROOT / "scripts" / "lib" / "forge_check_run.py").read_text(encoding="utf-8")
    for forbidden in ("gate_recorder", "REVISE", "APPROVE", "verdict"):
        assert forbidden not in source, f"gate knowledge leaked into the client: {forbidden}"
