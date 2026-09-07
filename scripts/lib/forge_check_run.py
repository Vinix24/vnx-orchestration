#!/usr/bin/env python3
"""GitHub-App client for publishing check-runs (Golf B, B2a).

The CLIENT layer, and deliberately nothing more: it authenticates as the
``vnx-gate`` GitHub App and posts a completed check-run. It has ZERO knowledge
of what a review gate decided — no mapping from a review outcome to a
``conclusion``, no filtering of which outcomes may be published. ``conclusion``
is forwarded exactly as given. That judgment, and the wiring into the gate
recorder, is B2b's; keeping it out of here is what lets B2b change the policy
without touching a line of transport.

Why a GitHub App at all: a check-run published with the operator's own token
carries the operator's identity, so branch protection cannot distinguish
"the gate passed" from "a human with a token said so". An App identity can be
bound in branch protection (``app_id`` on the required check), which is what
makes ``vnx-gate/*`` a check only the App can satisfy.

Three secrets, three sources:

  ``app.app_id`` / ``app.slug``   ``scripts/forge/branch_protection.yaml`` —
                                  public, versioned, reviewable.
  private key (PEM)               macOS keychain, item ``vnx-gate-app-key``.
  installation id                 macOS keychain, item ``vnx-gate-installation-id``.

Every keychain read fails LOUD. ``send_digest_email._read_smtp_pass_from_keychain``
returns ``""`` when the item is missing or the keychain is locked; that soft
failure is correct for an optional digest mail and WRONG here — an empty key
would turn "the gate could not authenticate" into "the check-run silently never
appeared", which is the exact failure mode branch protection exists to prevent.
So each failure raises with the literal ``security add-generic-password``
command that repairs it, plus the runbook.

HTTP transport: ``urllib.request``, not ``gh api``. ``gh`` resolves its own
credentials (keychain/keyring, ``hosts.yml``, ``GH_TOKEN``/``GITHUB_TOKEN``)
and its own owner/repo from the ambient cwd. Handing it an App token means
injecting it as ``GH_TOKEN`` into the subprocess environment and trusting gh's
auth-resolution order to prefer it — which makes the IDENTITY of the call, the
one thing this whole module exists to control, depend on gh's precedence rules
and on whatever else that environment carries. ``urllib.request`` sends exactly
the ``Authorization`` header built here and nothing else, and the token never
enters an environment block. ``apply_branch_protection.py`` (B1) uses ``gh api``
for the opposite reason: there the operator's own admin identity is precisely
what is wanted.

BILLING SAFETY: No Anthropic SDK. No direct API calls to api.anthropic.com.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from chain_origin_anchor import _owner_repo_from_remote  # noqa: E402
from forge_protection_drift import PROTECTION_YAML_RELATIVE_PATH  # noqa: E402

#: scripts/lib/forge_check_run.py -> scripts/lib -> scripts -> repo root.
#: Derived from ``__file__``, never from the cwd: this module is imported by
#: callers whose cwd is a dispatch worktree, not the checkout.
REPO_ROOT = _LIB_DIR.parent.parent

DEFAULT_YAML_PATH = REPO_ROOT / PROTECTION_YAML_RELATIVE_PATH

#: The operator runbook every failure points at. Written in B-golf's operator
#: step; referenced here because a loud error with nowhere to go is only half
#: a repair instruction.
RUNBOOK_PATH = "docs/operations/FORGE_GATE.md"

KEYCHAIN_PRIVATE_KEY_SERVICE = "vnx-gate-app-key"
KEYCHAIN_INSTALLATION_ID_SERVICE = "vnx-gate-installation-id"

GITHUB_API_BASE = "https://api.github.com"
GITHUB_ACCEPT = "application/vnd.github+json"
GITHUB_API_VERSION = "2022-11-28"

#: GitHub rejects an App JWT whose ``exp`` is more than 10 minutes out. 9
#: leaves a minute of headroom for a slow clock.
JWT_LIFETIME_SECONDS = 9 * 60
#: ``iat`` is backdated so a client clock running slightly fast cannot produce
#: a token GitHub considers issued in the future.
JWT_BACKDATE_SECONDS = 60
#: An installation token is treated as spent this long before GitHub's own
#: ``expires_at``, so a request is never sent with a token that dies in flight.
TOKEN_REFRESH_MARGIN_SECONDS = 300

_SECURITY_TIMEOUT_SECONDS = 10
_HTTP_TIMEOUT_SECONDS = 30
#: An API error body is quoted, not dumped: enough to name the cause, bounded
#: so a stray HTML error page cannot flood a log or a receipt.
_ERROR_BODY_LIMIT = 500


class ForgeCheckRunError(RuntimeError):
    """Anything that stops a check-run from being published.

    Always raised, never degraded to a no-op return: a check-run that silently
    fails to appear reads to branch protection as "the check has not run yet",
    which blocks forever without ever saying why.
    """


class ForgeAppConfigError(ForgeCheckRunError):
    """``scripts/forge/branch_protection.yaml`` has no usable ``app:`` block."""


class ForgeKeychainError(ForgeCheckRunError):
    """A keychain item is missing, empty, or unreadable."""


class ForgeAPIError(ForgeCheckRunError):
    """A GitHub API call failed. Carries the HTTP status and a bounded body.

    ``status`` is ``None`` when the call never got a response at all (DNS,
    TLS, timeout) — distinct from an HTTP status, because a caller retrying a
    transport blip and a caller reacting to a 422 are different decisions.
    """

    def __init__(self, message: str, *, status: Optional[int] = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(frozen=True)
class AppConfig:
    """The public half of the App's identity, read from the versioned YAML."""

    slug: str
    app_id: int


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_app_config(path: Optional[Path] = None) -> AppConfig:
    """Read ``app.slug`` / ``app.app_id`` from the branch-protection YAML.

    An absent or null ``app_id`` is the expected state until the operator has
    registered the App, so it raises with the operator step rather than a
    generic parse error.
    """
    yaml_path = Path(path) if path is not None else DEFAULT_YAML_PATH
    try:
        raw = yaml_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ForgeAppConfigError(
            f"kan {yaml_path} niet lezen: {exc}. Operator-stap uit {RUNBOOK_PATH}."
        ) from exc

    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ForgeAppConfigError(f"{yaml_path} is geen geldige YAML: {exc}") from exc

    block = (doc or {}).get("app") if isinstance(doc, dict) else None
    if block is None:
        raise ForgeAppConfigError(
            f"app-blok ontbreekt in {PROTECTION_YAML_RELATIVE_PATH}: "
            f"operator-stap uit {RUNBOOK_PATH}"
        )
    if not isinstance(block, dict):
        raise ForgeAppConfigError(
            f"app moet een object zijn met slug en app_id, kreeg {type(block).__name__} "
            f"({PROTECTION_YAML_RELATIVE_PATH})"
        )

    slug = block.get("slug")
    if not isinstance(slug, str) or not slug.strip():
        raise ForgeAppConfigError(
            f"app.slug ontbreekt of is leeg in {PROTECTION_YAML_RELATIVE_PATH}: "
            f"operator-stap uit {RUNBOOK_PATH}"
        )

    app_id = block.get("app_id")
    if app_id is None:
        raise ForgeAppConfigError(
            f"app_id ontbreekt in {PROTECTION_YAML_RELATIVE_PATH}: "
            f"operator-stap uit {RUNBOOK_PATH}"
        )
    # bool is an int subclass; `app_id: true` is a typo, not an identifier.
    if isinstance(app_id, bool) or not isinstance(app_id, int):
        raise ForgeAppConfigError(
            f"app.app_id moet een geheel getal zijn, kreeg {type(app_id).__name__} "
            f"({PROTECTION_YAML_RELATIVE_PATH}): operator-stap uit {RUNBOOK_PATH}"
        )

    return AppConfig(slug=slug.strip(), app_id=app_id)


# ---------------------------------------------------------------------------
# Keychain — loud on every failure, never an empty string
# ---------------------------------------------------------------------------


def _recovery_hint(service: str, what: str) -> str:
    return (
        f"Herstel: security add-generic-password -s {service} -a \"$USER\" -w '<{what}>' "
        f"(runbook: {RUNBOOK_PATH})"
    )


def _read_keychain_secret(service: str, what: str) -> str:
    """Return the keychain secret for ``service``, or raise.

    Never returns "" — see the module docstring for why the soft-fail pattern
    in ``send_digest_email`` is the wrong shape here.
    """
    account = os.environ.get("USER", "")
    argv = ["security", "find-generic-password", "-s", service, "-a", account, "-w"]
    hint = _recovery_hint(service, what)

    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=_SECURITY_TIMEOUT_SECONDS
        )
    except FileNotFoundError as exc:
        raise ForgeKeychainError(
            f"`security` niet gevonden: de keychain is alleen op macOS beschikbaar, "
            f"en {service} kan hier niet gelezen worden. {hint}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ForgeKeychainError(
            f"`security find-generic-password -s {service}` liep vast na "
            f"{_SECURITY_TIMEOUT_SECONDS}s (wacht de keychain op een wachtwoordprompt?). {hint}"
        ) from exc
    except OSError as exc:
        raise ForgeKeychainError(f"`security` kon niet starten voor {service}: {exc}. {hint}") from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        if proc.returncode == 44:
            reason = f"keychain-item {service} bestaat niet"
        elif proc.returncode == 51:
            reason = (
                f"de keychain staat op slot of weigert niet-interactieve toegang tot {service} "
                f"(ontgrendel met: security unlock-keychain)"
            )
        else:
            reason = f"`security` gaf exit {proc.returncode} voor {service}"
        raise ForgeKeychainError(f"{reason}: {stderr[:200]}. {hint}")

    secret = (proc.stdout or "").strip()
    if not secret:
        raise ForgeKeychainError(
            f"keychain-item {service} is leeg. Een lege waarde is hier nooit bruikbaar: "
            f"de check-run zou stil wegvallen in plaats van te falen. {hint}"
        )
    return secret


def read_private_key() -> str:
    """The App's RSA private key (PEM) from keychain item ``vnx-gate-app-key``."""
    return _read_keychain_secret(KEYCHAIN_PRIVATE_KEY_SERVICE, "pad naar de .pem, via $(cat ...)")


def read_installation_id() -> str:
    """The installation id from keychain item ``vnx-gate-installation-id``."""
    value = _read_keychain_secret(KEYCHAIN_INSTALLATION_ID_SERVICE, "installation-id")
    if not value.isdigit():
        raise ForgeKeychainError(
            f"keychain-item {KEYCHAIN_INSTALLATION_ID_SERVICE} is geen getal ({value[:40]!r}). "
            f"{_recovery_hint(KEYCHAIN_INSTALLATION_ID_SERVICE, 'installation-id')}"
        )
    return value


# ---------------------------------------------------------------------------
# owner/repo
# ---------------------------------------------------------------------------


def resolve_owner_repo(project_root: Optional[Path] = None) -> str:
    """``owner/repo`` from the ``origin`` remote of ``project_root``.

    Reuses ``chain_origin_anchor._owner_repo_from_remote`` rather than adding
    a fourth copy of the same regex to this repo.
    """
    root = Path(project_root) if project_root is not None else REPO_ROOT
    owner_repo = _owner_repo_from_remote(root)
    if not owner_repo:
        raise ForgeCheckRunError(
            f"kan owner/repo niet afleiden uit de git-remote 'origin' in {root} "
            "(geen GitHub-remote, of de remote ontbreekt)"
        )
    return owner_repo


# ---------------------------------------------------------------------------
# HTTP seam — one function, so tests replace the network and nothing else
# ---------------------------------------------------------------------------


def _api_request(
    method: str,
    url: str,
    *,
    headers: Dict[str, str],
    payload: Optional[str] = None,
    timeout: int = _HTTP_TIMEOUT_SECONDS,
) -> Tuple[int, str]:
    """Perform one API call. Returns ``(status, body_text)``.

    A 4xx/5xx is a RETURN, not a raise: each caller composes its own message
    from the status it actually got. Only a call that never reached GitHub
    raises here, because there is no status to hand back.
    """
    data = payload.encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in headers.items():
        request.add_header(key, value)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except OSError:
            body = ""
        return exc.code, body
    except urllib.error.URLError as exc:
        raise ForgeAPIError(f"{method} {url} bereikte GitHub niet: {exc.reason}") from exc
    except OSError as exc:
        raise ForgeAPIError(f"{method} {url} faalde op transportniveau: {exc}") from exc


def _github_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": GITHUB_ACCEPT,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "vnx-gate-check-run-client",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# JWT + installation token
# ---------------------------------------------------------------------------


def build_app_jwt(app_id: int, private_key_pem: str, *, now: Optional[datetime] = None) -> str:
    """An RS256 App JWT: ``iss`` = app id, backdated ``iat``, 9-minute ``exp``."""
    # Imported here rather than at module scope: PyJWT is NOT in
    # pyproject.toml [project.dependencies], so a plain `pip install
    # vnx-orchestration` has no ``jwt`` module — and every other importer of
    # this module would then die on an import it never reaches. The ImportError
    # is re-raised in this module's own shape for the same reason every
    # keychain read does: a bare ModuleNotFoundError names the module but not
    # the install that repairs it, and this is the one dependency an operator
    # will not already have. CI installs it in .github/workflows/vnx-ci.yml.
    try:
        import jwt  # noqa: PLC0415
    except ImportError as exc:
        raise ForgeCheckRunError(
            "PyJWT ontbreekt, dus er kan geen App-JWT ondertekend worden. "
            "Herstel: pip install 'pyjwt[crypto]'. "
            f"Runbook: {RUNBOOK_PATH}"
        ) from exc

    moment = now or datetime.now(timezone.utc)
    claims = {
        "iat": int((moment - timedelta(seconds=JWT_BACKDATE_SECONDS)).timestamp()),
        "exp": int((moment + timedelta(seconds=JWT_LIFETIME_SECONDS)).timestamp()),
        "iss": str(app_id),
    }
    try:
        return jwt.encode(claims, private_key_pem, algorithm="RS256")
    # PyJWT installed WITHOUT its asymmetric backend: `import jwt` succeeds and
    # only RS256 is absent, so the gap surfaces here at signing time instead of
    # at import time above. Kept separate from the key errors below because the
    # key was never even looked at — telling an operator to re-add a perfectly
    # good .pem would send them down the wrong repair.
    except NotImplementedError as exc:
        raise ForgeCheckRunError(
            "PyJWT kent RS256 niet: de [crypto]-extra ontbreekt, dus er is geen "
            "asymmetrische backend. Herstel: pip install 'pyjwt[crypto]'. "
            f"Runbook: {RUNBOOK_PATH}"
        ) from exc
    # A malformed PEM surfaces as cryptography's ValueError ("Could not
    # deserialize key data"), a non-string key as TypeError, and a key of the
    # wrong type for RS256 as PyJWTError. All three mean the same thing to a
    # caller — the key in the keychain is unusable — so all three become one
    # loud error naming the item to repair.
    except (ValueError, TypeError, jwt.exceptions.PyJWTError) as exc:
        raise ForgeCheckRunError(
            f"kon geen App-JWT bouwen met de sleutel uit {KEYCHAIN_PRIVATE_KEY_SERVICE}: {exc}. "
            f"{_recovery_hint(KEYCHAIN_PRIVATE_KEY_SERVICE, 'pad naar de .pem, via $(cat ...)')}"
        ) from exc


@dataclass(frozen=True)
class _CachedToken:
    installation_id: str
    token: str
    expires_at: datetime


#: Process-local, single slot: one process talks to one installation. Cleared
#: by :func:`reset_token_cache`.
_TOKEN_CACHE: Optional[_CachedToken] = None


def reset_token_cache() -> None:
    """Drop the cached installation token (tests, and any identity change)."""
    global _TOKEN_CACHE
    _TOKEN_CACHE = None


def _parse_expires_at(raw: Any) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise ForgeAPIError(
            f"installatietoken zonder bruikbare expires_at ({raw!r}): zonder vervaltijd "
            "is er geen veilig moment om te verversen"
        )
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ForgeAPIError(f"onbegrepen expires_at in installatietoken: {raw!r}") from exc


def _exchange_installation_token(installation_id: str, app_jwt: str) -> _CachedToken:
    url = f"{GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens"
    status, body = _api_request("POST", url, headers=_github_headers(app_jwt))

    if status >= 400:
        # A 401 here means the JWT itself was refused — a wrong app_id, a key
        # that does not belong to it, or a revoked App. Re-minting the same
        # JWT would produce the same 401, so this raises instead of retrying.
        raise ForgeAPIError(
            f"installatietoken ophalen faalde (HTTP {status}) voor installatie "
            f"{installation_id}: {body[:_ERROR_BODY_LIMIT]}",
            status=status,
            body=body[:_ERROR_BODY_LIMIT],
        )

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ForgeAPIError(
            f"installatietoken-respons was geen JSON (HTTP {status}): "
            f"{body[:_ERROR_BODY_LIMIT]}",
            status=status,
            body=body[:_ERROR_BODY_LIMIT],
        ) from exc

    token = parsed.get("token") if isinstance(parsed, dict) else None
    if not isinstance(token, str) or not token:
        raise ForgeAPIError(
            f"installatietoken-respons bevat geen token-veld: {body[:_ERROR_BODY_LIMIT]}",
            status=status,
            body=body[:_ERROR_BODY_LIMIT],
        )

    return _CachedToken(
        installation_id=installation_id,
        token=token,
        expires_at=_parse_expires_at(parsed.get("expires_at")),
    )


def _installation_token_with_freshness(
    *, now: Optional[datetime] = None, force_refresh: bool = False
) -> Tuple[str, bool]:
    """``(token, minted)``. ``minted`` is True when this call exchanged a new one.

    The caller needs that distinction: a 401 on a token minted milliseconds ago
    is a real authentication problem, while a 401 on a cached one may just be a
    token GitHub retired early. See :func:`publish_check_run`.
    """
    global _TOKEN_CACHE

    moment = now or datetime.now(timezone.utc)
    installation_id = read_installation_id()

    cached = _TOKEN_CACHE
    if (
        not force_refresh
        and cached is not None
        and cached.installation_id == installation_id
        and moment < cached.expires_at - timedelta(seconds=TOKEN_REFRESH_MARGIN_SECONDS)
    ):
        return cached.token, False

    config = load_app_config()
    app_jwt = build_app_jwt(config.app_id, read_private_key(), now=moment)
    # Assigned only after a fully validated exchange: a failed call must leave
    # the previous cache state untouched rather than poisoning it.
    fresh = _exchange_installation_token(installation_id, app_jwt)
    _TOKEN_CACHE = fresh
    return fresh.token, True


def installation_token(*, now: Optional[datetime] = None, force_refresh: bool = False) -> str:
    """A valid installation access token, from cache when one is still good."""
    token, _ = _installation_token_with_freshness(now=now, force_refresh=force_refresh)
    return token


# ---------------------------------------------------------------------------
# The primitive
# ---------------------------------------------------------------------------


def publish_check_run(
    head_sha: str,
    name: str,
    conclusion: str,
    summary: str,
    *,
    details_url: Optional[str] = None,
    project_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """POST a completed check-run and return GitHub's response object.

    ``conclusion`` is passed through UNTOUCHED — not validated against
    GitHub's enum, not mapped from any review outcome. Choosing it is B2b's
    job; a client that second-guessed it would put that policy in two places.
    """
    if not head_sha or not head_sha.strip():
        raise ForgeCheckRunError("head_sha is leeg: een check-run zonder commit hoort nergens")
    if not name or not name.strip():
        raise ForgeCheckRunError("name is leeg: branch protection matcht check-runs op naam")

    owner_repo = resolve_owner_repo(project_root)
    url = f"{GITHUB_API_BASE}/repos/{owner_repo}/check-runs"
    body: Dict[str, Any] = {
        "name": name,
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": conclusion,
        "output": {"title": name, "summary": summary},
    }
    if details_url:
        body["details_url"] = details_url
    payload = json.dumps(body)

    token, minted = _installation_token_with_freshness()
    status, response_body = _api_request(
        "POST", url, headers=_github_headers(token), payload=payload
    )

    if status == 401 and not minted:
        # The cached token was refused. GitHub can retire one before its stated
        # expires_at, so exactly ONE forced refresh and ONE retry — never a
        # loop, which would turn a genuinely bad key into an API hammer.
        token, _ = _installation_token_with_freshness(force_refresh=True)
        status, response_body = _api_request(
            "POST", url, headers=_github_headers(token), payload=payload
        )

    if status >= 400:
        raise ForgeAPIError(
            f"check-run publiceren faalde (HTTP {status}) op {owner_repo} voor "
            f"{head_sha[:12]}: {response_body[:_ERROR_BODY_LIMIT]}",
            status=status,
            body=response_body[:_ERROR_BODY_LIMIT],
        )

    try:
        parsed = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise ForgeAPIError(
            f"check-run-respons was geen JSON (HTTP {status}): "
            f"{response_body[:_ERROR_BODY_LIMIT]}",
            status=status,
            body=response_body[:_ERROR_BODY_LIMIT],
        ) from exc

    if not isinstance(parsed, dict):
        raise ForgeAPIError(
            f"check-run-respons was geen object: {response_body[:_ERROR_BODY_LIMIT]}",
            status=status,
            body=response_body[:_ERROR_BODY_LIMIT],
        )
    return parsed
