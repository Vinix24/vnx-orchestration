"""dispatch_cli.py — Single-entry dispatch gate (PR-4).

spec -> validate -> snapshot -> compile_plan -> permit -> execute

Feature-gated by VNX_SINGLE_ENTRY_DISPATCH=1 in dispatch.sh. When the flag is
unset the bash layer uses the legacy path; this module's logic is unchanged.

BILLING SAFETY: no anthropic SDK import. Claude lane executes via interactive
tmux (subscription). Provider lane executes via run_envelope_plan (provider_metered).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

logger = logging.getLogger(__name__)

from dispatch_spec import (  # noqa: E402
    DispatchPath,
    DispatchSpec,
    Isolation,
    PathAccess,
    Provider,
    Reject,
    ValidatedSpec,
    validate,
)
from dispatch_plan import (  # noqa: E402
    ConstraintVerdict,
    ExecutionPlan,
    ModelPin,
    RuntimeSnapshot,
    claude_auth_is_api_metered,
    compile_plan,
)
from dispatch_internal import (  # noqa: E402
    ExecutionPermit,
    is_valid_instruction_hash,
    issue_permit,
    require_permit,
)
from dispatch_envelope import run_envelope_plan, run_envelope_headless_plan  # noqa: E402
from dispatch_serialization import force_release, serialize_lane  # noqa: E402


class _InvariantViolation(Exception):
    """A door closed-set or permit invariant was breached — a should-never-happen,
    security-relevant event, categorically distinct from a transient runtime error.
    Surfaced with its own reject code so the audit signal is not masked (audit finding A7)."""


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _resolve_data_dir(project_id: "str | None" = None) -> Path:
    """Resolve VNX data directory. Mirrors provider_dispatch._resolve_data_dir.

    ``project_id`` (when given) is the authoritative tenant — used to derive the
    central store ``~/.vnx-data/<project_id>`` so a caller that already knows the
    target project (e.g. the staged-bundle authority in ``run_dispatch``) does not
    fall back to the ambient ``VNX_PROJECT_ID``/``vnx-dev`` default.


    PR-4d trust boundary: the resolved data root is OPERATOR config, not attacker
    input. The threat model is our own agents, not an external adversary — and an
    operator who wants an external-drive layout would point VNX_DATA_DIR straight
    at it. A symlinked data root is therefore legitimate and is NOT rejected (that
    would break external-drive setups). What IS untrusted is anything PLANTED
    INSIDE this root by a dispatch (e.g. a symlinked dispatches/pending escaping
    it); that is closed by _check_pending_root_anchor_verdict below.
    """
    explicit_flag = os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1"
    explicit_val = os.environ.get("VNX_DATA_DIR", "")
    if explicit_flag and explicit_val:
        return Path(explicit_val).resolve()
    pid = project_id or os.environ.get("VNX_PROJECT_ID", "vnx-dev")
    return Path.home() / ".vnx-data" / pid


def _resolve_project_id() -> str:
    """Authoritative project_id for the door's ADR-007 guard.

    Delegates to the canonical resolver: VNX_PROJECT_ID env, then the nearest
    ``.vnx-project-id`` marker walking up from CWD. The old ``return
    os.environ.get("VNX_PROJECT_ID", "vnx-dev")`` HARDCODED ``vnx-dev`` as the
    fallback, so EVERY consumer dispatch that did not export VNX_PROJECT_ID
    resolved to ``vnx-dev`` — the guard then either mis-routed the entire
    governance state (receipt, report, spec, events, log) into the vnx-dev store
    or rejected the correct project_id as a "cross-project redirect". This hit
    every consumer (sales-copilot, mission-control, seocrawler). Resolving from
    the marker fixes the fleet; an unresolvable project fails closed (raises)
    rather than silently landing in vnx-dev.
    """
    from project_root import resolve_project_id  # noqa: PLC0415
    return resolve_project_id()


def _resolve_repo_root() -> Path:
    """scripts/lib/dispatch_cli.py -> repo root (parents[2])."""
    return Path(__file__).resolve().parents[2]


def _authority_from_spec_path(spec_file: Path) -> "tuple[str | None, Path | None]":
    """Derive (project_id, data_dir) from a staged bundle's PHYSICAL location.

    Bundle layout (stage_spec_bundle): ``<data_dir>/dispatches/pending/<id>/dispatch-spec.json``.
    The store the bundle physically lives in IS the dispatch's tenant authority — NOT the
    ambient CWD. In a central install the door's CWD is the shared engine tree, whose stray
    ``.vnx-project-id`` would mis-resolve every consumer to ``vnx-dev`` (misroute pre-#1091,
    hard-reject post-#1091). Deriving from the bundle location fixes the whole class and keeps
    the ADR-007 anti-redirect guard meaningful: a spec that declares a project_id different from
    the store it was staged into still fails validation.

    Returns ``(None, None)`` when ``spec_file`` is not under that layout (ad-hoc/test specs), so
    the caller falls back to ambient resolution.
    """
    try:
        p = Path(spec_file).resolve()
        if p.name != "dispatch-spec.json":
            return None, None
        if p.parents[1].name != "pending" or p.parents[2].name != "dispatches":
            return None, None
        data_dir = p.parents[3]
        from vnx_paths import project_id_from_state_dir  # noqa: PLC0415
        pid = project_id_from_state_dir(data_dir / "state")
        if not pid:
            return None, None
        return pid, data_dir
    except Exception:  # noqa: BLE001 — resolution is best-effort; fall back to ambient
        return None, None


# ---------------------------------------------------------------------------
# Spec loading from JSON
# ---------------------------------------------------------------------------

def _sanitize_headless_reason(raw: object) -> "str | None":
    """Strip newlines/control chars from headless_reason so multi-line values can't break log formatting."""
    if not raw or not isinstance(raw, str):
        return None
    cleaned = _re.sub(r"[\x00-\x1f\x7f]+", " ", raw).strip()
    return cleaned or None


def load_spec(spec_file: Path) -> DispatchSpec:
    """Parse a DispatchSpec from a JSON dispatch-spec.json file."""
    raw = json.loads(spec_file.read_text(encoding="utf-8"))

    raw_paths = raw.get("dispatch_paths") or []
    dispatch_paths = tuple(
        DispatchPath(
            path=PurePosixPath(str(p["path"])),
            access=PathAccess(p.get("access", "read_write")),
            materialize_at_cwd=p.get("materialize_at_cwd") is True,
        )
        for p in raw_paths
    )

    return DispatchSpec(
        schema_version=int(raw["schema_version"]),
        project_id=str(raw["project_id"]),
        dispatch_id=str(raw["dispatch_id"]),
        staging_id=str(raw["staging_id"]),
        instruction_file=Path(raw["instruction_file"]),
        role=str(raw["role"]),
        target_slot=str(raw["target_slot"]),
        gate=str(raw.get("gate", "")),
        dispatch_paths=dispatch_paths,
        provider=Provider(raw.get("provider", "auto")),
        model=(raw.get("model") or None),
        skill=(raw.get("skill") or None),
        task_class=(raw.get("task_class") or None),
        pr_id=(raw.get("pr_id") or None),
        work_ref=(raw.get("work_ref") or None),
        track_id=(raw.get("track_id") or None),
        # Chain-link (dispatch-20260802-model-ssot-en-ketenlink).
        parent_dispatch=(raw.get("parent_dispatch") or None),
        tier_from=(raw.get("tier_from") or None),
        tier_to=(raw.get("tier_to") or None),
        deadline_seconds=int(raw.get("deadline_seconds", 3600)),
        base_ref=str(raw.get("base_ref", "origin/main")),
        isolation=Isolation(raw.get("isolation", "worktree")),
        requires_mcp=raw.get("requires_mcp") is True,
        target_id_override=(raw.get("target_id_override") or None),
        tags=tuple(str(t) for t in (raw.get("tags") or [])),
        instruction_sha256=(raw.get("instruction_sha256") or None),
        allow_headless=raw.get("allow_headless") is True,
        headless_reason=_sanitize_headless_reason(raw.get("headless_reason")),
        post_merge_verification=raw.get("post_merge_verification") is True,
    )


# ---------------------------------------------------------------------------
# Permit fingerprint helper
# ---------------------------------------------------------------------------

def fingerprint(permit: ExecutionPermit) -> str:
    """Short stable display string: plan_digest[:12]-dispatch_id.

    Unforgeable (anchored to plan digest) yet human-readable for log lines.
    """
    return f"{permit.plan_digest[:12]}-{permit.dispatch_id}"


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _emit_reject(r: Reject) -> None:
    print(f"[dispatch_cli] REJECT [{r.code}]: {r.reason}", file=sys.stderr)


def _print_plan(plan: ExecutionPlan, fp: str) -> None:
    print(f"[dispatch_cli] DRY RUN — fingerprint: {fp}")
    print(f"  dispatch_id:  {plan.dispatch_id}")
    print(f"  provider:     {plan.provider.value}")
    print(f"  model:        {plan.model}")
    print(f"  lane:         {plan.lane}")
    print(f"  target_id:    {plan.target_id}")
    print(f"  billing:      {plan.billing}")
    print(f"  requires_mcp: {plan.requires_mcp}")
    print(f"  route_reason: {plan.route_reason}")
    # Chain-link (dispatch-20260802-model-ssot-en-ketenlink): printed so a dry-run
    # proves the spec fields landed on the plan.
    if plan.parent_dispatch:
        print(f"  parent_dispatch: {plan.parent_dispatch}")
    if plan.task_class:
        print(f"  task_class:   {plan.task_class}")
    if plan.tier_from or plan.tier_to:
        print(f"  tier:         {plan.tier_from or '-'} -> {plan.tier_to or '-'}")
    for w in plan.warnings:
        print(f"  [WARN] {w}")


_HEADLESS_ISOLATION_WARNING = (
    "OI-1158: claude_headless lane isolation is NOT structurally verified by "
    "the door. Every ExecutionPlan claims isolation=worktree (dispatch_plan.py's "
    "D6 rule — Isolation.WORKTREE is the only legal member), but the door has no "
    "check that the CHOSEN LANE actually delivers it. The claude_tmux_subscription "
    "lane's isolation is enforced one call away via tmux_worktree.allocate(); the "
    "claude_headless lane's isolation lives in a parallel code path "
    "(dispatch_envelope.py -> dispatch_worktree_isolation.py) this module never "
    "inspects or cross-checks. Treat this dispatch as isolation-UNVERIFIED by the "
    "door until a structural check replaces this warning — do not assume the "
    "worktree guarantee held just because the plan says isolation=worktree."
)


def _headless_isolation_guard(plan: "ExecutionPlan") -> "Optional[str]":
    """Return a loud isolation-guarantee warning for a claude_headless plan.

    OI-1158 (peer-measured 2026-08-12, pacompany-engine): two headless
    dispatches, both staged with ``isolation: worktree`` in the spec, both
    ended up sharing the SAME tree — the project's own main checkout — on the
    branch of whichever dispatch fired first. The door printed nothing: the
    ExecutionPlan's ``isolation`` field always reads ``Isolation.WORKTREE``
    (it is the only legal enum member — see dispatch_spec.Isolation), so a
    caller reading the plan sees an isolation guarantee that the door itself
    never verifies the LANE actually implements. A silent no-op on an
    isolation guarantee is the most dangerous form: the caller believes they
    are protected.

    Deliberately WARNS rather than REFUSES: the headless lane is a live,
    relied-upon lane (opened 2026-08-11 by explicit operator directive,
    ``docs/core/DISPATCH_RULES.md`` §8), and a hard door-level refusal would
    block dispatches that run successfully today. A refusal belongs on the
    lane's OWN fail-closed gate (``_execute_claude_headless`` already has one
    via ``lane_safety.headless_block``) — not duplicated here as a second,
    coarser gate that can't distinguish "isolation is broken" from "isolation
    is merely unverified by this door". The warning plus the receipt-visible
    field (``_persist_route_decision``'s ``isolation_note``) is the minimum
    bar this dispatch closes: never silent again, escalate to a hard door
    gate only once the real enforcement gap it flags is closed.

    Returns None for every lane other than ``claude_headless`` — the tmux
    lane's isolation is verified structurally (``tmux_worktree.allocate()``
    is one call away from ``_execute_claude`` and asserts the worktree's own
    branch, OI-1124), so it earns no warning here.
    """
    if plan.lane != "claude_headless":
        return None
    return _HEADLESS_ISOLATION_WARNING


def _probe_litellm_style_proxy(label: str, models_url: str, api_key: str, auth_hint: str) -> None:
    """Shared HTTP GET .../v1/models reachability probe (OI-867, OI-1147 pt.11).

    Never raises — prints a [reachability]/[WARN] line to stderr and returns.
    Shared by the litellm:* benchmark-baseline lanes and the glm-harness lane:
    both terminate on the same litellm-shaped /v1/models endpoint, just with
    different url/key resolution supplied by the caller.
    """
    import urllib.request
    import urllib.error

    try:
        req = urllib.request.Request(models_url)
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        resp = urllib.request.urlopen(req, timeout=5)
        body = resp.read().decode("utf-8", errors="replace")
        # A 200 with an empty model list is suspicious but not a hard
        # failure — the proxy may be warming up.
        if resp.status == 200:
            try:
                data = json.loads(body)
                model_count = len(data.get("data", []))
                if model_count == 0:
                    print(
                        f"[dispatch_cli] [WARN] {label} returned 0 models "
                        f"from {models_url} — the lane may be stale. Evidence: "
                        f"empty model list.",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"[dispatch_cli] [reachability] {label} OK: "
                        f"{model_count} models at {models_url}",
                        file=sys.stderr,
                    )
            except json.JSONDecodeError:
                print(
                    f"[dispatch_cli] [WARN] {label} response is not valid "
                    f"JSON from {models_url} — response: {body[:200]}",
                    file=sys.stderr,
                )
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:  # vnx-silent-except: body read is best-effort; empty case caught by `body or str(exc)` below
            pass
        if status in (401, 403):
            print(
                f"[dispatch_cli] [WARN] {label} AUTH REJECTED "
                f"(HTTP {status}) at {models_url}. Evidence: {body or str(exc)}. "
                f"{auth_hint}",
                file=sys.stderr,
            )
        else:
            print(
                f"[dispatch_cli] [WARN] {label} returned HTTP {status} "
                f"from {models_url}. Evidence: {body or str(exc)}",
                file=sys.stderr,
            )
    except (urllib.error.URLError, OSError) as exc:
        print(
            f"[dispatch_cli] [WARN] {label} unreachable at "
            f"{models_url} ({exc}). The lane may be down — dispatch will "
            f"likely fail.",
            file=sys.stderr,
        )


def _check_reachability(plan: "ExecutionPlan", spec: "DispatchSpec") -> None:
    """Verify the selected lane's endpoint is reachable (OI-867, OI-1147 pt.11).

    Never fails the dry-run — always returns None.  Prints a [WARN] with
    concrete evidence when the endpoint is unreachable or auth-rejected
    so an operator can spot a dead route before approving the dispatch.

    Checks performed:
    - litellm-proxy lanes: HTTP GET http://127.0.0.1:4141/v1/models with
      short timeout.  Carries Authorization: Bearer $LITELLM_API_KEY when that
      env is set (OI-893) so an auth-gated proxy is probed the way the lane
      actually talks to it.  401/403 → hard warning (auth broken).  Connection
      refused/timeout → soft warning (proxy may be down).
    - glm-harness lane: same physical :4141 litellm proxy as above, resolved
      via glm_harness_spawn's own url/key resolvers (VNX_GLM_PROXY_URL /
      VNX_GLM_PROXY_KEY) so this probe can never drift from what the spawn
      actually connects to. Before this check, glm-harness fell into the
      generic "no cheap endpoint check" bucket below and a dead proxy was
      only discovered after a worker had already spent tokens (OI-1147 pt.11).
    - deepseek-harness: HTTP GET to api.deepseek.com/v1/models with
      Authorization: Bearer $DEEPSEEK_API_KEY (OI-893).
    - Other lanes: skipped (no cheap endpoint check available; claude-tmux
      has no network endpoint, codex/kimi have ephemeral auth).
    """
    import urllib.request
    import urllib.error

    provider_val = plan.provider.value
    lane = plan.lane

    # ── litellm proxy lanes (litellm:deepseek, litellm:zai, litellm:moonshot) ──
    if lane == "provider" and (
        provider_val.startswith("litellm:")
        or provider_val in ("litellm",)
    ):
        proxy_url = os.environ.get(
            "LITELLM_PROXY_BASE",
            "http://127.0.0.1:4141",
        )
        models_url = f"{proxy_url.rstrip('/')}/v1/models"
        # OI-893: the probe must carry the proxy's Authorization header — an
        # unauthenticated GET to an auth-gated /v1/models returns HTTP 401 by
        # construction, so the OK branch would be unreachable whenever the proxy
        # enforces a key. Add the header when LITELLM_API_KEY is present; when it
        # is absent, the 401 the proxy returns is genuine and is reported as
        # AUTH REJECTED with a "key not set" hint, never as an OK.
        litellm_key = os.environ.get("LITELLM_API_KEY", "").strip()
        if litellm_key:
            auth_hint = (
                "The proxy API key (LITELLM_API_KEY) is missing or "
                "invalid — ANY dispatch on this lane will fail silently."
            )
        else:
            auth_hint = (
                "LITELLM_API_KEY is not set — ANY dispatch on this "
                "lane will fail silently."
            )
        _probe_litellm_style_proxy("litellm proxy", models_url, litellm_key, auth_hint)
        return

    # ── glm-harness lane (OI-1147 pt.11): claude CLI -> local litellm proxy ──
    # -> OpenRouter. Same physical :4141 proxy the litellm:zai benchmark lane
    # above probes, but glm-harness resolves its own url/key via
    # glm_harness_spawn's resolvers so this can never name a different
    # endpoint than the one the spawn actually redirects ANTHROPIC_BASE_URL
    # to. spawn_glm_harness() repeats a plain TCP-connect fail-closed check
    # right before spawning (belt-and-suspenders); this is the earlier,
    # door-level check that fires before a worker slot/tokens are spent.
    if lane == "provider" and provider_val == Provider.GLM_HARNESS.value:
        from provider_spawns.glm_harness_spawn import (
            _proxy_key as _glm_proxy_key,
            _proxy_url as _glm_proxy_url,
        )

        proxy_url = _glm_proxy_url()
        models_url = f"{proxy_url.rstrip('/')}/v1/models"
        glm_key = _glm_proxy_key()
        auth_hint = (
            "The proxy's master_key doesn't match VNX_GLM_PROXY_KEY (or the "
            "local default sk-glm-harness-local) — ANY glm-harness dispatch "
            "will fail silently."
        )
        _probe_litellm_style_proxy("glm-harness proxy", models_url, glm_key, auth_hint)
        return

    # ── deepseek-harness lane ──
    if lane == "provider" and provider_val in (
        "deepseek-harness", "deepseek_harness",
    ):
        ds_url = "https://api.deepseek.com/v1/models"
        # OI-893: the probe must carry the API key. An unauthenticated GET to
        # api.deepseek.com/v1/models returns HTTP 401 by construction, so the
        # OK branch was unreachable. With DEEPSEEK_API_KEY set the probe now
        # exercises the same auth the lane uses; when it is absent the 401 is
        # genuine and is reported as AUTH REJECTED with a "key not set" hint.
        ds_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        try:
            req = urllib.request.Request(ds_url)
            if ds_key:
                req.add_header("Authorization", f"Bearer {ds_key}")
            urllib.request.urlopen(req, timeout=5)
            print(
                f"[dispatch_cli] [reachability] deepseek API OK: {ds_url}",
                file=sys.stderr,
            )
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                if not ds_key:
                    auth_hint = "DEEPSEEK_API_KEY is not set."
                elif exc.code == 403:
                    auth_hint = "the API key is missing permissions."
                else:
                    auth_hint = "the API key is missing, invalid, or expired."
                print(
                    f"[dispatch_cli] [WARN] deepseek API AUTH REJECTED "
                    f"(HTTP {exc.code}) at {ds_url}. {auth_hint}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[dispatch_cli] [WARN] deepseek API returned HTTP "
                    f"{exc.code} from {ds_url}.",
                    file=sys.stderr,
                )
        except (urllib.error.URLError, OSError) as exc:
            print(
                f"[dispatch_cli] [WARN] deepseek API unreachable at "
                f"{ds_url} ({exc}).",
                file=sys.stderr,
            )
        return

    # ── Claude tmux lane, codex, kimi, gemini — no cheap endpoint check ──
    print(
        f"[dispatch_cli] [reachability] lane={lane} — no cheap endpoint "
        f"check available (lanes that don't go through a stable HTTP proxy "
        f"are verified at spawn time).",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# P1-#3: model pins from SSOT
# ---------------------------------------------------------------------------

_DEFAULT_MODEL_PINS: dict[str, ModelPin] = {
    # T0 falls back to the canonical opus-5 registry key (model-ssot-en-ketenlink);
    # the provider_constraints.yaml t0-opus-only pin is the live SSOT and overrides
    # this when readable.
    "T0": ModelPin(model="opus-5", semantics="floor"),
    "T1": ModelPin(model="kimi-k3", semantics="default"),
    "T2": ModelPin(model="kimi-k3", semantics="default"),
    "T3": ModelPin(model="kimi-k3", semantics="default"),
}

# worker-claude-override (escape-hatch-worker-claude, 2026-07-23): gated, audited
# operator escape-hatch that routes ONE build-worker dispatch back to claude via
# the tmux-subscription lane. ALL of these must hold or the default kimi-k3
# hard-reject stands unchanged:
#   1. VNX_OVERRIDE_WORKER_CLAUDE=1 (explicit env override, per-dispatch)
#   2. VNX_OVERRIDE_WORKER_CLAUDE_REASON non-empty (audit; inert + blocking refusal without it)
#   3. spec.provider is explicitly claude
#   4. spec.target_slot is a build worker (T1/T2/T3)
_BUILD_WORKER_SLOTS = frozenset({"T1", "T2", "T3"})
WORKER_CLAUDE_OVERRIDE_ENV = "VNX_OVERRIDE_WORKER_CLAUDE"
WORKER_CLAUDE_OVERRIDE_REASON_ENV = "VNX_OVERRIDE_WORKER_CLAUDE_REASON"


_KNOWN_PIN_SEMANTICS = frozenset({"floor", "default"})


def _load_model_pins_from_yaml() -> dict[str, ModelPin]:
    """Load T0/T1/T2/T3 model pins (with pin semantics) from provider_constraints.yaml SSOT.

    Read/parse failures (missing file, invalid YAML, unsupported schema version) are NOT
    silently swallowed: they are logged loudly and fall back to _DEFAULT_MODEL_PINS, which
    itself carries explicit semantics — the fallback reproduces the intended pin state, it
    never silently softens it (dispatch_cli-part of OI-826).

    An unrecognized `pin_semantics` value on a present constraint is a config-authoring
    error, not a read failure, and is a categorically different case: it is NEVER silently
    interpreted as `floor` or `default` and is NOT caught here — it propagates so the
    dispatch fails loud (caught by run_dispatch's outer runtime-error handler).
    """
    yaml_path = _LIB_DIR / "providers" / "provider_constraints.yaml"
    try:
        import yaml  # noqa: PLC0415
        with yaml_path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except Exception as exc:
        logger.error(
            "[dispatch_cli] model-pins YAML unreadable at %s (%s) — falling back to "
            "_DEFAULT_MODEL_PINS",
            yaml_path, exc,
        )
        return dict(_DEFAULT_MODEL_PINS)

    if not isinstance(data, dict) or data.get("version") != 1:
        logger.error(
            "[dispatch_cli] model-pins YAML at %s has a missing/unsupported version "
            "(expected 1) — falling back to _DEFAULT_MODEL_PINS",
            yaml_path,
        )
        return dict(_DEFAULT_MODEL_PINS)

    pins: dict[str, ModelPin] = {}
    for constraint in (data.get("constraints") or []):
        cid = str(constraint.get("id", ""))
        required = constraint.get("required_route") or {}
        model = required.get("model")
        if not model:
            continue
        # Missing pin_semantics on a present constraint reads as "floor" — a
        # not-yet-migrated constraint must never silently soften.
        semantics = constraint.get("pin_semantics", "floor")
        if semantics not in _KNOWN_PIN_SEMANTICS:
            raise ValueError(
                f"provider_constraints.yaml constraint {cid!r} has unknown "
                f"pin_semantics {semantics!r} (expected 'floor' or 'default')"
            )
        pin = ModelPin(model=str(model), semantics=semantics)
        if cid == "t0-opus-only":
            pins["T0"] = pin
        elif cid == "workers-kimi-pinned":
            for slot in ("T1", "T2", "T3"):
                pins[slot] = pin
    return {**_DEFAULT_MODEL_PINS, **pins}


# ---------------------------------------------------------------------------
# P0-2: staging binding check helpers
# ---------------------------------------------------------------------------

# Mirrors _DISPATCH_ID_RE from staging_validator.py
import re as _re
_PENDING_ID_RE = _re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$')


def _check_pending_root_anchor_verdict(data_dir: Path) -> Optional[ConstraintVerdict]:
    """Return BLOCKING ConstraintVerdict if dispatches/pending escapes the data root.

    P0-2: the binding/existence checks resolve bundle paths *relative to* the
    pending root. If `dispatches` or `pending` is a symlink that hops outside the
    trusted data_dir, an external bundle resolves "inside" the (escaped) pending
    root and every downstream containment check passes — fail-OPEN. Anchor the
    pending root first: its fully-resolved path must stay under the resolved
    data_dir, else refuse to promote. Returns None on pass.

    PR-4d trust boundary: this anchor protects against symlinks PLANTED INSIDE the
    trusted data root (a dispatch-controlled `dispatches`/`dispatches/pending`
    that escapes it). The data root ITSELF is trusted operator config (see
    _resolve_data_dir) and is intentionally not rejected for being a symlink.
    """
    try:
        data_root = data_dir.resolve()
        pending_root = (data_dir / "dispatches" / "pending").resolve()
    except (ValueError, OSError) as exc:
        return ConstraintVerdict(
            code="ADR-006-untrusted-root",
            severity="blocking",
            message=f"pending root resolution failed: {exc}",
        )
    if not pending_root.is_relative_to(data_root):
        return ConstraintVerdict(
            code="ADR-006-untrusted-root",
            severity="blocking",
            message=(
                f"dispatches/pending escapes the trusted data root: resolved "
                f"{pending_root} is not under {data_root} — refusing to promote"
            ),
        )
    return None


def _check_staging_binding_verdict(
    spec_file: Path,
    instruction_file: Path,
    *,
    data_dir: Path,
    staging_id: str,
) -> Optional[ConstraintVerdict]:
    """Return BLOCKING ConstraintVerdict if spec_file or instruction_file escape the bundle.

    Follows symlinks via resolve() so symlink escapes are caught. Returns None on pass.

    P0-2: the bundle dir is anchored under the resolved data root before the
    per-file containment checks, so a symlinked `staging_id` (or any symlink in
    the pending path) that resolves outside the data root is rejected rather than
    silently trusted.
    """
    try:
        data_root = data_dir.resolve()
        bundle_dir = (data_dir / "dispatches" / "pending" / staging_id).resolve()
        if not bundle_dir.is_relative_to(data_root):
            return ConstraintVerdict(
                code="ADR-006-untrusted-root",
                severity="blocking",
                message=(
                    f"bundle pending/{staging_id}/ escapes the trusted data root: "
                    f"resolved {bundle_dir} is not under {data_root}"
                ),
            )
        sf_resolved = spec_file.resolve()
        if not sf_resolved.is_relative_to(bundle_dir):
            return ConstraintVerdict(
                code="ADR-006-binding",
                severity="blocking",
                message=(
                    f"spec_file is not inside bundle pending/{staging_id}/: "
                    f"got {sf_resolved}"
                ),
            )
        if_resolved = instruction_file.resolve()
        if not if_resolved.is_relative_to(bundle_dir):
            return ConstraintVerdict(
                code="ADR-006-binding",
                severity="blocking",
                message=(
                    f"instruction_file is not inside bundle pending/{staging_id}/: "
                    f"got {if_resolved}"
                ),
            )
    except (ValueError, OSError) as exc:
        return ConstraintVerdict(
            code="ADR-006-binding",
            severity="blocking",
            message=f"staging binding path resolution failed: {exc}",
        )
    return None


# ---------------------------------------------------------------------------
# TL-D1 — track_id door validation + persistence
#
# Structural link dispatch -> track, validated at the door (fail-closed on an
# invalid/nonexistent/done track_id) and staged advisory->required on absence
# via VNX_REQUIRE_DISPATCH_TRACK (mirrors the wiring_gate.py VNX_WIRING_GATE_REQUIRED
# shadow/blocking staging pattern). Tracks live in the same runtime_coordination.db
# as the dispatches table (schemas/migrations/0022_track_layer.sql).
# ---------------------------------------------------------------------------

_TRACKS_DB_FILENAME = "runtime_coordination.db"

_NO_TRACK_ESCAPE_RE = _re.compile(r"^no-track:.+$")


def _tracks_db_path(state_dir: Path) -> Path:
    return state_dir / _TRACKS_DB_FILENAME


def _has_col(conn, table: str, col: str) -> bool:
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def _has_no_track_escape(tags: "tuple[str, ...]") -> bool:
    return any(_NO_TRACK_ESCAPE_RE.match(t) for t in tags)


def _lookup_track_phase(db_path: Path, track_id: str, project_id: str) -> Optional[str]:
    """Return the track's phase for (track_id, project_id), or None if no such track.

    Read-only URI connection: a missing DB file raises immediately rather than
    silently creating an empty one. Caller degrades any exception to a WARN
    verdict (fail-open on tracks-DB unavailability; never crash the door).
    """
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    try:
        row = conn.execute(
            "SELECT phase FROM tracks WHERE track_id = ? AND project_id = ?",
            (track_id, project_id),
        ).fetchone()
        return row[0] if row is not None else None
    finally:
        conn.close()


def _check_track_link_verdict(spec: DispatchSpec, *, state_dir: Path) -> Optional[ConstraintVerdict]:
    """Validate spec.track_id at the door.

    - track_id present, references a nonexistent or already-done track -> blocking Reject
      (the tag-vs-link mistake becomes impossible).
    - track_id present, references a live track -> None (passes clean).
    - tracks DB unavailable while checking a present track_id -> WARN, never crash.
    - track_id absent -> staged advisory (VNX_REQUIRE_DISPATCH_TRACK OFF, default) WARN,
      or required (ON) blocking Reject unless tags carries a 'no-track:<reason>' escape.
    """
    track_id = (spec.track_id or "").strip()
    db_path = _tracks_db_path(state_dir)

    if track_id:
        try:
            phase = _lookup_track_phase(db_path, track_id, spec.project_id)
        except Exception as exc:
            return ConstraintVerdict(
                code="tracks-db-unavailable",
                severity="warn",
                message=(
                    f"tracks DB unavailable ({exc}); cannot verify track_id={track_id!r}, "
                    "degrading to warn"
                ),
            )
        if phase is None:
            return ConstraintVerdict(
                code="bad-track-link",
                severity="blocking",
                message=(
                    f"track_id={track_id!r} does not reference an existing track "
                    f"for project_id={spec.project_id!r}"
                ),
            )
        if phase == "done":
            return ConstraintVerdict(
                code="bad-track-link",
                severity="blocking",
                message=f"track_id={track_id!r} references a track already in phase='done'",
            )

        # Plan-first-gate enforcement (advisory-first). A live track whose OI-PLAN
        # plan-first gate is unresolved must not be dispatched: building before
        # planning is exactly what the gate exists to prevent, yet the gate used to
        # bind only closure bookkeeping. Shared read-only check lives in
        # plan_gate_enforcement so the merge gate applies the same rule.
        import plan_gate_enforcement as _pge  # noqa: PLC0415
        mode = _pge.enforce_mode()
        if mode != "off":
            try:
                pg_state = _pge.plan_gate_state(db_path, track_id, spec.project_id)
            except Exception:
                # DB race between the phase read and here; already fail-open above.
                pg_state = _pge.UNSUPPORTED
            if pg_state == _pge.UNRESOLVED:
                run_cmd = f"vnx horizon plan-gate run {track_id} --doc <plan-doc>"
                if mode == "required" and not _pge.override_active():
                    return ConstraintVerdict(
                        code="plan-gate-unresolved",
                        severity="blocking",
                        message=(
                            f"track_id={track_id!r} has not passed its plan-first gate "
                            f"(OI-PLAN-{track_id} unresolved). Plan before work: run "
                            f"`{run_cmd}` (or `vnx horizon plan-gate attest {track_id}`), or "
                            f"operator-override with VNX_OVERRIDE_PLAN_GATE=1."
                        ),
                    )
                overridden = mode == "required" and _pge.override_active()
                return ConstraintVerdict(
                    code="plan-gate-unresolved",
                    severity="warn",
                    message=(
                        f"track_id={track_id!r} plan-first gate unresolved "
                        f"(VNX_PLAN_GATE_ENFORCE={mode}"
                        + (", operator override applied" if overridden else "")
                        + f"); advisory. Run `{run_cmd}` before dispatching."
                    ),
                    override_applied=overridden,
                )
        return None

    import config_runtime
    required = config_runtime.get_bool("VNX_REQUIRE_DISPATCH_TRACK")
    if not required:
        return ConstraintVerdict(
            code="track_unlinked",
            severity="warn",
            message="dispatch has no track_id (VNX_REQUIRE_DISPATCH_TRACK is OFF; advisory-only)",
        )
    if _has_no_track_escape(spec.tags):
        logger.info(
            "[dispatch_cli] dispatch=%s: no-track escape applied (tags=%r)",
            spec.dispatch_id, spec.tags,
        )
        return None
    return ConstraintVerdict(
        code="track-required",
        severity="blocking",
        message=(
            "VNX_REQUIRE_DISPATCH_TRACK=1 requires a track_id; add a tags entry "
            "'no-track:<reason>' to opt out for a genuinely exploratory dispatch"
        ),
    )


def _persist_dispatch_row(
    spec: DispatchSpec,
    *,
    state_dir: Path,
    worker_claude_override_reason: Optional[str] = None,
) -> None:
    """Best-effort: create the dispatches tracker row for a door-accepted dispatch.

    The door is the single entry point for dispatches, yet historically never
    wrote a row to dispatches (runtime_coordination.db) — only the deliverable
    layer (planning_cli.py, `dlv-` ids) did. Three consumers read this table for
    door dispatches and all silently no-op without a row:

    1. ``_persist_track_id`` (UPDATE-only) — track linkage, symptom TL-D1;
    2. ``dispatch_outcome_classifier.reconcile_all_dispatch_outcomes`` — reads
       the dispatch-id population plus ``state``/``track``/``created_at`` here;
    3. ``receipt_provenance._link_pr_to_track`` — reads ``track_id`` by
       ``dispatch_id`` for the TL-D2 tracks.pr_ref auto-propagation on merge.

    Called from run_dispatch AFTER validation + plan compile and BEFORE the lane
    choice, so rejected dispatches never get a row and in-flight dispatches are
    visible to live queries.

    ``state='proposed'`` deliberately: 'queued' would be claimed by the worker
    pool (its claim query keys on state='queued') and, without a staged pool
    bundle, rot into timed_out/expired via the stuck sweep; the supervisor's
    stuck/ghost sweeps key on claimed/delivering/accepted/running. 'proposed'
    is invisible to all of those — the same state the deliverable layer uses.

    Idempotent per ADR-007's composite UNIQUE(dispatch_id, project_id): a retry
    or fix-forward with the same id finds the existing row and leaves it
    untouched. Never raises: tracker bookkeeping must never block the door.

    OI-943: persists target_slot and worker_claude_override_reason so the audit
    trail can distinguish ported from unported claude dispatches. target_slot is
    always present (required in DispatchSpec); worker_claude_override_reason is
    only present when a build-worker override was applied.
    """
    db_path = _tracks_db_path(state_dir)
    if not db_path.exists():
        return
    import sqlite3 as _sqlite3
    try:
        conn = _sqlite3.connect(str(db_path), timeout=10.0)
        try:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'dispatches'"
            ).fetchone() is None:
                return
            has_project = _has_col(conn, "dispatches", "project_id")
            track_id = (spec.track_id or "").strip()
            if track_id and not _has_col(conn, "dispatches", "track_id"):
                conn.execute("ALTER TABLE dispatches ADD COLUMN track_id TEXT")
                conn.commit()
            # Idempotency: an existing row (retry / fix-forward / deliverable
            # layer) is left untouched — never a second row, never a mutation.
            if has_project:
                existing = conn.execute(
                    "SELECT 1 FROM dispatches WHERE dispatch_id = ? AND project_id = ?",
                    (spec.dispatch_id, spec.project_id),
                ).fetchone()
            else:
                existing = conn.execute(
                    "SELECT 1 FROM dispatches WHERE dispatch_id = ?",
                    (spec.dispatch_id,),
                ).fetchone()
            if existing is not None:
                return
            cols = ["dispatch_id"]
            vals = [spec.dispatch_id]
            if has_project:
                cols.append("project_id")
                vals.append(spec.project_id)
            cols.append("state")
            vals.append("proposed")
            if track_id:
                cols.append("track_id")
                vals.append(track_id)
            # OI-943: persist target_slot (always present) and the worker-claude
            # override reason (only when an override was applied) so the audit
            # trail can distinguish ported from unported claude dispatches.
            target_slot = spec.target_slot.strip()
            if target_slot and not _has_col(conn, "dispatches", "target_slot"):
                conn.execute("ALTER TABLE dispatches ADD COLUMN target_slot TEXT")
                conn.commit()
            if target_slot:
                cols.append("target_slot")
                vals.append(target_slot)
            override_reason = (worker_claude_override_reason or "").strip()
            if override_reason:
                if not _has_col(conn, "dispatches", "worker_claude_override_reason"):
                    conn.execute(
                        "ALTER TABLE dispatches ADD COLUMN worker_claude_override_reason TEXT"
                    )
                    conn.commit()
                cols.append("worker_claude_override_reason")
                vals.append(override_reason)
            now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            )
            for ts_col in ("created_at", "updated_at"):
                if _has_col(conn, "dispatches", ts_col):
                    cols.append(ts_col)
                    vals.append(now)
            placeholders = ", ".join("?" for _ in cols)
            conn.execute(
                f"INSERT INTO dispatches ({', '.join(cols)}) VALUES ({placeholders})",
                vals,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("[dispatch_cli] dispatch row persist skipped: %s", exc)


def _register_gate_obligation(spec: DispatchSpec, *, state_dir: Path) -> None:
    """Best-effort: register the review-gate obligation for a door-accepted dispatch.

    OI-876/OI-881: before this hook, ``spec.gate`` was read exactly once (by
    ``load_spec``) and then never consumed — a dispatch could declare
    ``gate=codex_gate`` and produce zero request/result records while looking
    identical to one whose gate ran. The obligation record (one JSON file per
    dispatch under ``state/review_gates/obligations/``) is what
    ``scripts/gate_obligation_runner.py`` fulfils and what the producer
    freshness monitor asserts per gate key: declaration without evidence
    becomes visible instead of silent.

    Never raises: bookkeeping must never block the door (same contract as
    ``_persist_dispatch_row``).
    """
    gate = (spec.gate or "").strip()
    if not gate:
        return
    try:
        from gate_obligations import pr_number_from_pr_id, register_obligation
        register_obligation(
            state_dir,
            dispatch_id=spec.dispatch_id,
            gate=gate,
            project_id=spec.project_id,
            pr_number=pr_number_from_pr_id(spec.pr_id),
        )
    except Exception as exc:  # noqa: BLE001 — door bookkeeping must never raise
        logger.debug("[dispatch_cli] gate obligation register skipped: %s", exc)


def _persist_track_id(spec: DispatchSpec, *, state_dir: Path) -> None:
    """Best-effort: attach spec.track_id to an EXISTING dispatches row (UPDATE-only).

    Never INSERTs — row creation is the door's job (``_persist_dispatch_row``,
    invoked earlier in run_dispatch, right after validation + plan compile).
    When that row exists this UPDATE stamps the track_id onto it; D2 treats an
    absent/None track_id as a no-op, so a dispatch whose row could not be
    created (e.g. no runtime_coordination.db yet) is a safe, anticipated case
    here, not a partial failure. Adds the track_id column additively
    (_has_col-guarded) when missing. Never raises.
    """
    track_id = (spec.track_id or "").strip()
    if not track_id:
        return
    db_path = _tracks_db_path(state_dir)
    if not db_path.exists():
        return
    import sqlite3 as _sqlite3
    try:
        conn = _sqlite3.connect(str(db_path), timeout=10.0)
        try:
            if not _has_col(conn, "dispatches", "track_id"):
                conn.execute("ALTER TABLE dispatches ADD COLUMN track_id TEXT")
                conn.commit()
            conn.execute(
                "UPDATE dispatches SET track_id = ? WHERE dispatch_id = ? AND project_id = ?",
                (track_id, spec.dispatch_id, spec.project_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("[dispatch_cli] track_id persist skipped: %s", exc)


# ---------------------------------------------------------------------------
# build_runtime_snapshot — all I/O lives here
# ---------------------------------------------------------------------------

def _sub_provider_for(provider_value: str) -> Optional[str]:
    if provider_value.startswith("litellm:"):
        return provider_value.split(":", 1)[1].split(":", 1)[0] or None
    if provider_value == "deepseek-harness":
        return "deepseek"
    if provider_value == "glm-harness":
        # sub=zai lets a future glm-specific constraint match forbidden_route.provider=zai;
        # the distinct harness via (below) is what clears zai-via-openrouter-only AND
        # glm-via-harness-only (mirrors the deepseek-harness sub=deepseek/keyed-via pattern).
        return "zai"
    return None


def _via_for_provider(provider_value: str, sub_provider: Optional[str]) -> Optional[str]:
    via_per_sub = {
        "deepseek": "litellm",
        "moonshot": "moonshot",
        "openrouter": "openrouter",
        "zai": "openrouter",
    }
    if provider_value.startswith("litellm:") or provider_value == "litellm":
        return via_per_sub.get(sub_provider or "", "litellm")
    if provider_value == "deepseek-harness":
        return "claude_harness_keyed"
    if provider_value == "glm-harness":
        # Distinct harness via (NOT plain "openrouter"): the claude CLI pointed at the local
        # :4141 litellm proxy → OpenRouter. Clears zai-via-openrouter-only (via != direct) AND
        # glm-via-harness-only (via not in [openrouter, litellm]); plain litellm:zai (via=openrouter)
        # stays blocked. Must match provider_dispatch._constraint_via_for_provider for glm-harness.
        return "claude_harness_openrouter"
    if provider_value in ("claude", "codex", "gemini", "kimi"):
        return "cli"
    if provider_value == "local-gemma":
        return "local"
    return None


def _discover_valid_roles(agents_dir: Path) -> frozenset[str]:
    """Return the set of role names that exist in the agents/ registry.

    A role is valid when ``<agents_dir>/<role>/CLAUDE.md`` exists — the same role
    file the worker loads as its profile. Missing or unreadable registry → EMPTY
    set, so compile_plan's OI-921 membership check rejects every role (fail-closed):
    an undiscoverable registry must never silently accept an arbitrary role string.
    """
    try:
        if not agents_dir.is_dir():
            return frozenset()
        return frozenset(
            entry.name
            for entry in agents_dir.iterdir()
            if entry.is_dir() and (entry / "CLAUDE.md").is_file()
        )
    except OSError:
        return frozenset()


def _resolve_router_pre_validate(spec: DispatchSpec) -> "Optional[DoorRouteResult]":
    """Run the smart router on a DispatchSpec BEFORE validate().

    OI-962: the router must resolve provider+model BEFORE validate() tests
    the result against constraints.  When the spec carries provider=AUTO
    (including the empty/None→auto bridge alias), this reads the instruction
    file and consults the tier-routing engine.

    Returns a DoorRouteResult carrying (provider, model, route_reason) when the
    router has a recommendation, or a decline_reason (+ tier when the
    classifier ran) when routing should be skipped (T0, router disabled,
    classifier error). Returns None on an unexpected error outside the router
    (e.g. an unreadable instruction file).

    Fail-open for the classifier and other unexpected errors — but fail-loud for
    registry drift (ADR-036 §2): a RegistryLookupError (unknown provider/model)
    propagates so the door rejects instead of silently falling back to CLAUDE.
    """
    try:
        from providers.smart_router.door_routing import resolve_door_route  # noqa: PLC0415
        from providers.provider_registry import RegistryLookupError  # noqa: PLC0415

        # Read instruction text (same logic as validate Rule 5 — the file
        # has already passed staging validation so this is a cheap re-read).
        ifile = spec.instruction_file
        instruction_text = ifile.read_text(encoding="utf-8")

        file_paths = [str(dp.path) for dp in spec.dispatch_paths]
        return resolve_door_route(
            spec_provider=spec.provider,
            spec_model=spec.model,
            target_slot=spec.target_slot,
            instruction_text=instruction_text,
            file_paths=file_paths,
        )
    except RegistryLookupError:
        # ADR-036 §2: unknown provider/model is drift, not a routing bug — the
        # door must reject loudly rather than fall back to the default lane.
        raise
    except Exception as exc:
        logger.warning(
            "smart-router pre-validate: router call failed, dispatch "
            "falls through to default lane (fail-open). Error: %s",
            exc,
            exc_info=True,
        )
        return None


# OI-1187: tier-aware fallback for a router decline. Keys are the canonical
# cost_tier constants (TIER_HIGH / TIER_MID) kept as literals here because
# dispatch_cli avoids a module-level import of the smart_router package (the
# router is imported lazily inside _resolve_router_pre_validate). A tier-high
# decline must land on opus-5 (equal class), never silently on sonnet.
_TIER_FALLBACK_MODEL: dict[str, str] = {
    "tier-high": "opus-5",
    "tier-mid": "sonnet-5",
}


def _tier_aware_fallback_model(tier: Optional[str], spec_model: Optional[str]) -> str:
    """Pick the claude-lane fallback model when the router declines to route.

    An explicit spec.model always wins. Otherwise the fallback is tier-aware:
    a tier-high dispatch the classifier could not route lands on opus-5 (equal
    class) and tier-mid on sonnet-5, never silently a class down (OI-1187).
    A tier the classifier did not determine (None) keeps the historical
    "sonnet" last resort.
    """
    if spec_model:
        return spec_model
    if tier:
        return _TIER_FALLBACK_MODEL.get(tier, "sonnet")
    return "sonnet"


def _resolve_gate_via_router(vspec: ValidatedSpec) -> "tuple[ValidatedSpec, Optional[str]]":
    """Fill the spec's review-gate from the router when the spec is silent.

    Punt 7 (gate-weight-by-variant): the router derives a ``governance_variant``
    from the change (dispatch_paths + task_class) and maps it to a gate weight,
    so a docs dispatch and a dispatch-door rewrite no longer land on the same
    gate because one author chose it. An explicit gate on the spec always wins
    (worker-provider-free-choice, pin_semantics=default: the router fills in,
    it never overrides).

    Returns (vspec, gate_reason): a rebuilt ValidatedSpec carrying the filled
    gate (or the original when the spec already declared one), and a trace
    string for the dry-run output / receipt; never a bare None when the router
    ran. Fail-open: a broken derivation returns the original vspec unchanged
    with the gate left empty (today's baseline), logged at WARNING.
    """
    spec = vspec.spec
    if (spec.gate or "").strip():
        return vspec, None

    try:
        from smart_router import resolve_gate  # noqa: PLC0415

        resolution = resolve_gate(
            explicit_gate=spec.gate,
            dispatch_paths=[str(dp.path) for dp in spec.dispatch_paths],
            task_class=spec.task_class,
        )
    except Exception as exc:
        logger.warning(
            "smart-router gate resolution failed, gate left empty (fail-open): %s",
            exc,
            exc_info=True,
        )
        return vspec, None

    new_spec = dataclasses.replace(spec, gate=resolution.gate)
    new_vspec = dataclasses.replace(vspec, spec=new_spec)
    return new_vspec, f"smart-router:{resolution.reason}"


def main_checkout_lag(project_root: Path, *, ref: str = "origin/main") -> Optional[int]:
    """Number of commits the checkout at *project_root* is behind *ref*.

    OI-1214: the door builds every dispatch from the consumer's local main
    checkout; when that lags ``origin/main``, a post-merge verification runs the
    OLD code and reports a false negative. This returns the count — a NUMBER, not
    a boolean — so the door can log "3 commits behind" instead of "out of date".

    ``git rev-list --count HEAD..<ref>`` counts commits reachable from *ref* but
    not from HEAD (exactly "behind"). Returns None when the distance cannot be
    determined (not a git repo, no ``origin`` remote, or *ref* not yet fetched),
    so "0" always means a genuinely current checkout and "unknown" is never
    silently read as "up to date". Never raises.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(project_root), "rev-list", "--count", f"HEAD..{ref}"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    try:
        return int(out.stdout.strip())
    except ValueError:
        return None


def _post_merge_verification_lag_verdict(lag: int) -> ConstraintVerdict:
    """Fail-closed refusal for a post-merge-verification dispatch on a stale checkout.

    The message names the CONSEQUENCE (this verification would measure the old
    code and return a false negative) and the exact command that fixes it, rather
    than only stating the fact that the checkout lags.
    """
    return ConstraintVerdict(
        code="post-merge-verification-stale-checkout",
        severity="blocking",
        message=(
            f"dispatch is a post-merge verification but the local main checkout is "
            f"{lag} commit(s) behind origin/main — this verification would measure "
            f"the old code and report a false negative. Bring the checkout current "
            f"first: `git pull --ff-only` (or `git fetch origin && git merge "
            f"--ff-only origin/main`)"
        ),
    )


def _log_checkout_lag(spec: DispatchSpec, lag: Optional[int]) -> None:
    """Log the local-checkout lag at every dispatch as a NUMBER, never a flag.

    OI-1214: "3 commits behind" is actionable; "out of date" is not. A lag of 0
    (current) and a lag > 0 are both logged; only an unresolvable ref logs the
    honest "unknown" state, which callers must not read as "up to date".
    """
    if lag is None:
        logger.info(
            "[dispatch_cli] dispatch=%s: local main checkout behind origin/main: "
            "unknown (origin/main not resolvable — not a git repo or no origin remote)",
            spec.dispatch_id,
        )
    elif lag == 0:
        logger.info(
            "[dispatch_cli] dispatch=%s: local main checkout is 0 commits behind origin/main",
            spec.dispatch_id,
        )
    else:
        logger.warning(
            "[dispatch_cli] dispatch=%s: local main checkout is %d commits behind origin/main",
            spec.dispatch_id,
            lag,
        )


def build_runtime_snapshot(
    vspec: ValidatedSpec,
    *,
    data_dir: Path,
    spec_file: Path,
) -> RuntimeSnapshot:
    """Perform all I/O required by compile_plan.

    P0-1: instruction_text + check_registry=True (FAIL-CLOSED); effective model; SDK scan (warn via constraint engine).
    P0-2: staging binding verified via spec_file containment check.
    P1-#3: model_pins from provider_constraints.yaml SSOT.
    OI-921: valid_roles discovered from the engine's agents/ registry (fail-closed).
    """
    from providers.constraint_enforcer import check_constraints as _constraint_check  # noqa: PLC0415
    from staging_validator import _exists_in_dir as _staging_exists  # noqa: PLC0415

    spec = vspec.spec
    provider_value = spec.provider.value
    sub_provider = _sub_provider_for(provider_value)
    via = _via_for_provider(provider_value, sub_provider)

    # P1-#3: model_pins from SSOT
    model_pin_specs = _load_model_pins_from_yaml()  # dict[str, ModelPin]

    # P0-1: effective model — same computation compile_plan uses in D4
    #
    # worker-provider-kimi-flip (20260723): model_pin_specs now resolves T1/T2/T3 to
    # "kimi-k3" (workers-kimi-pinned). The "sonnet" fallback below is intentionally
    # UNCHANGED — it only fires when is_claude_lane is True (an explicit provider=
    # claude override, or a non-standard target_slot with no pin) and spec.model was
    # not given; it must stay a valid Claude model name. If an explicit claude
    # override lands on T1/T2/T3 under a `floor` pin, the resolved model is
    # "kimi-k3" (a non-Claude label) which correctly fails the check_registry gate
    # below (model-not-in-current-registry, blocking) instead of silently
    # dispatching sonnet — matching the "kimi-only, no fallback" policy (fail loud,
    # never a silent claude rescue). The ONLY sanctioned way past that reject is
    # the gated, audited operator escape-hatch directly below (worker-claude-
    # override); everything else about the default path is unchanged.
    is_claude_lane = spec.provider == Provider.CLAUDE

    # worker-claude-override gate (escape-hatch-worker-claude): evaluate the
    # override conditions BEFORE the effective-model computation. The override is
    # read from this process's env, which is per-dispatch (the door runs one
    # process per dispatch) — it is never global state. Default path (no override
    # env) is byte-for-byte unchanged.
    override_gate_verdicts: list[ConstraintVerdict] = []
    worker_claude_override_reason: Optional[str] = None
    if (
        is_claude_lane
        and spec.target_slot in _BUILD_WORKER_SLOTS
        and os.environ.get(WORKER_CLAUDE_OVERRIDE_ENV) == "1"
    ):
        reason = (os.environ.get(WORKER_CLAUDE_OVERRIDE_REASON_ENV) or "").strip()
        if reason:
            worker_claude_override_reason = reason
        else:
            # Override env set but no audit reason -> the override is INERT and the
            # dispatch is REFUSED (blocking). Fail loud, never silently fall back to
            # either kimi-k3 or claude.
            override_gate_verdicts.append(ConstraintVerdict(
                code="worker-claude-override-reason-required",
                severity="blocking",
                message=(
                    f"{WORKER_CLAUDE_OVERRIDE_ENV}=1 is set but "
                    f"{WORKER_CLAUDE_OVERRIDE_REASON_ENV} is empty/absent — the "
                    "worker-claude override is inert without an audit reason. "
                    "Refusing; the default kimi-k3 hard-reject stands."
                ),
            ))

    if is_claude_lane:
        if worker_claude_override_reason is not None:
            # Override granted: SKIP the kimi-k3 model_pins coercion for THIS
            # dispatch only, so effective_model is the requested claude model. The
            # constraint check below still runs against this model (registry,
            # kimi-via-cli-only, etc.) — the override is claude->claude, never a
            # bypass of the constraint engine.
            effective_model = spec.model or "sonnet"
        else:
            # worker-provider-free-choice PR-2: honor ModelPin.semantics instead of
            # always coercing to the pin. "floor" is today's behavior verbatim —
            # spec.model is ignored, the pin always wins. "default" is advisory —
            # spec.model wins when set, the pin only fills in when spec carries no
            # model at all. The `or "sonnet"` tail stays the last-resort fallback
            # on both branches.
            pin = model_pin_specs.get(spec.target_slot)
            if pin is None:
                effective_model = spec.model or "sonnet"
            elif pin.semantics == "floor":
                effective_model = pin.model or "sonnet"
            else:  # "default" — loader fails loud on any other value at load time
                effective_model = spec.model or pin.model or "sonnet"
    else:
        effective_model = spec.model or "default"

    # claude-headless enforcement: allow_headless=True sets via to 'headless', which
    # triggers the claude-headless forbid_route constraint. Normal tmux lane keeps via='cli'.
    if is_claude_lane and spec.allow_headless:
        via = "headless"

    # P0-1: constraint check with instruction_text + check_registry=True; FAIL-CLOSED on error
    constraint_verdicts: tuple[ConstraintVerdict, ...] = ()
    try:
        raw_violations = _constraint_check(
            provider=provider_value,
            sub_provider=sub_provider,
            model=effective_model,
            terminal_id=spec.target_slot,
            role=spec.role,
            via=via,
            env=os.environ,
            check_registry=True,
            instruction_text=vspec.instruction_text,
        )
        constraint_verdicts = tuple(
            ConstraintVerdict(
                code=v.code,
                severity=v.severity,
                message=v.message,
                override_applied=v.override_applied,
            )
            for v in raw_violations
        )
    except Exception as exc:
        constraint_verdicts = (ConstraintVerdict(
            code="registry-unavailable",
            severity="blocking",
            message=f"Constraint registry unavailable — fail-closed: {exc}",
        ),)

    # Defense-in-depth (dispatch-agent-lane-coercion, OI-LANECOERCE): a worker-model pin
    # (workers-kimi-pinned) replaces spec.model with effective_model BEFORE the check above
    # ever runs, so a cross-provider requested model (e.g. --model kimi resolved onto the claude
    # lane) is invisible to the kimi-via-cli-only guard by the time it inspects effective_model.
    # Since worker-provider-kimi-flip (2026-07-23) effective_model on a claude-lane T1/T2/T3 is
    # itself "kimi-k3" whenever a pin exists — that mismatch is already caught by check_registry
    # in the block above, so this raw-model re-check is belt-and-suspenders for any target_slot
    # outside the SSOT pin dict. Re-run the check against the RAW requested model too, so the pin
    # can never mask a mismatched provider. Only BLOCKING verdicts are folded in — warn-only pin
    # noise (e.g. --model opus pinned to kimi-k3) is already reported once via D4's own warning.
    raw_model = spec.model
    if raw_model and raw_model != effective_model:
        try:
            raw_violations = _constraint_check(
                provider=provider_value,
                sub_provider=sub_provider,
                model=raw_model,
                terminal_id=spec.target_slot,
                role=spec.role,
                via=via,
                env=os.environ,
                check_registry=False,
            )
        except Exception as exc:
            raw_violations = []
            constraint_verdicts = constraint_verdicts + (ConstraintVerdict(
                code="registry-unavailable",
                severity="blocking",
                message=f"Constraint registry unavailable (raw-model guard) — fail-closed: {exc}",
            ),)
        existing_codes = {v.code for v in constraint_verdicts}
        for v in raw_violations:
            if v.severity == "blocking" and v.code not in existing_codes:
                constraint_verdicts = constraint_verdicts + (ConstraintVerdict(
                    code=v.code,
                    severity=v.severity,
                    message=f"[raw-model guard] {v.message}",
                    override_applied=v.override_applied,
                ),)
                existing_codes.add(v.code)

    # worker-claude-override verdicts: a gate refusal (reason required) is
    # prepended so it surfaces as THE reject; an applied override is recorded as an
    # audited warn verdict (flows via D3 into plan.warnings — advisory, excluded
    # from the plan digest) carrying the reason, target_slot, and resolved model.
    if override_gate_verdicts:
        constraint_verdicts = tuple(override_gate_verdicts) + constraint_verdicts
    if worker_claude_override_reason is not None:
        constraint_verdicts = constraint_verdicts + (ConstraintVerdict(
            code="worker-claude-override-applied",
            severity="warn",
            message=(
                f"operator override {WORKER_CLAUDE_OVERRIDE_ENV}=1 applied for THIS "
                f"dispatch only: build worker {spec.target_slot} routes to claude "
                f"model {effective_model!r} via the tmux-subscription lane "
                f"(kimi-k3 pin skipped). Reason: {worker_claude_override_reason}"
            ),
            override_applied=True,
        ),)
        logger.warning(
            "[dispatch_cli] AUDIT worker-claude-override-applied target=%s model=%s reason=%s",
            spec.target_slot,
            effective_model,
            worker_claude_override_reason,
        )

    # P0-2: anchor the pending root BEFORE trusting any bundle path. A symlinked
    # dispatches/pending that escapes the data root cannot host a promoted bundle
    # (fail-closed) — checked unconditionally so it holds even if existence is faked.
    root_verdict = _check_pending_root_anchor_verdict(data_dir)
    if root_verdict is not None:
        constraint_verdicts = constraint_verdicts + (root_verdict,)

    # Staging existence check (belt-and-suspenders; binding check below is the specific gate)
    dispatches_dir = data_dir / "dispatches"
    staging_promoted = _staging_exists(dispatches_dir / "pending", spec.staging_id)

    # P0-2: staging binding — spec_file and instruction_file must be inside the bundle dir
    if staging_promoted:
        binding_verdict = _check_staging_binding_verdict(
            spec_file,
            spec.instruction_file,
            data_dir=data_dir,
            staging_id=spec.staging_id,
        )
        if binding_verdict is not None:
            constraint_verdicts = constraint_verdicts + (binding_verdict,)

    # TL-D1: track_id door validation (fail-closed on invalid/nonexistent/done;
    # staged advisory->required WARN/Reject when absent).
    track_verdict = _check_track_link_verdict(spec, state_dir=data_dir / "state")
    if track_verdict is not None:
        constraint_verdicts = constraint_verdicts + (track_verdict,)

    if is_claude_lane:
        target_health: dict[str, str] = {"ephemeral": "healthy"}
        target_capable: dict[str, bool] = {"ephemeral": True}
    else:
        target_id = spec.target_id_override or spec.target_slot
        target_health = {target_id: "healthy"}
        target_capable = {target_id: True}

    # worker-claude-override: strip the kimi-k3 pin for THIS dispatch's snapshot
    # only, so compile_plan's D4 resolves the requested claude model instead of the
    # pin. The loaded model_pin_specs dict itself is never mutated; every dispatch
    # that does not carry the override still sees the full pins (kimi stays default).
    snapshot_model_pins = model_pin_specs
    if worker_claude_override_reason is not None:
        snapshot_model_pins = {
            slot: pin for slot, pin in model_pin_specs.items() if slot != spec.target_slot
        }

    # OI-921: role-registry — the set of roles that exist in agents/ (the engine's
    # repo root, resolved exactly as run_dispatch does for validate). Empty set when
    # the registry is missing → compile_plan rejects every role (fail-closed).
    valid_roles = _discover_valid_roles(_resolve_repo_root() / "agents")

    # Chain-link (dispatch-20260802-model-ssot-en-ketenlink): computed here,
    # door-side (I/O + imports allowed), and passed through the snapshot so
    # compile_plan stays pure. task_class comes from the spec when set, else the
    # smart_router deterministic classifier (the existing vocabulary — never a
    # second one). tier_to falls back to the model->tier reverse map so a
    # dispatch that did not declare a tier still carries an escalation signal.
    chain_task_class = (spec.task_class or "").strip() or None
    if chain_task_class is None:
        try:
            from smart_router import classify_task  # noqa: PLC0415
            chain_task_class = classify_task(vspec.instruction_text, spec.role)
        except Exception:  # noqa: BLE001 — classifier is best-effort; default class is the safe fallback
            chain_task_class = "01_code_generation"
    chain_parent = (spec.parent_dispatch or "").strip() or None
    chain_tier_from = (spec.tier_from or "").strip() or None
    chain_tier_to = (spec.tier_to or "").strip() or None
    if chain_tier_to is None:
        try:
            from providers.model_normalizer import tier_for_model  # noqa: PLC0415
            chain_tier_to = tier_for_model(effective_model)
        except Exception:  # noqa: BLE001 — tier reverse-map is best-effort
            chain_tier_to = None

    # OI-1156: auth-derived billing signal — the claude lane's billing label must
    # follow the AUTH identity (own key / redirect = metered), not the lane. The
    # door owns env reads, so compute it here and hand it to the pure compile_plan
    # via the snapshot.
    claude_api_metered = claude_auth_is_api_metered(os.environ)

    # OI-1214: the door builds every dispatch from the consumer's local main
    # checkout. Measure how far that checkout lags origin/main — a NUMBER — and
    # fail-closed on a post-merge-verification dispatch that would run the old
    # code. The consumer root is resolved exactly as the worktree allocator does
    # (resolve_consumer_project_root), so the lag is measured against the same
    # checkout the worker will actually build in. Best-effort: an unresolvable
    # root/ref logs an unknown lag and never blocks (fail-open on unknown,
    # fail-closed on a KNOWN lag — a door that refused on "unknown" would block
    # every non-git consumer, which is worse than the problem it fixes).
    checkout_lag: Optional[int] = None
    try:
        from dispatch_worktree_isolation import resolve_consumer_project_root  # noqa: PLC0415
        checkout_lag = main_checkout_lag(resolve_consumer_project_root())
    except Exception as exc:  # vnx-silent-except: lag is advisory except for the refusal below; resolution must never block the door
        logger.debug("[dispatch_cli] checkout-lag resolution failed for dispatch=%s: %s", spec.dispatch_id, exc)
    _log_checkout_lag(spec, checkout_lag)
    if checkout_lag is not None and checkout_lag > 0 and spec.post_merge_verification:
        constraint_verdicts = constraint_verdicts + (
            _post_merge_verification_lag_verdict(checkout_lag),
        )

    return RuntimeSnapshot(
        constraint_verdicts=constraint_verdicts,
        staging_promoted=staging_promoted,
        target_health=target_health,
        target_capable=target_capable,
        model_pins=snapshot_model_pins,
        valid_roles=valid_roles,
        parent_dispatch=chain_parent,
        task_class=chain_task_class,
        tier_from=chain_tier_from,
        tier_to=chain_tier_to,
        worker_claude_override_reason=worker_claude_override_reason,
        claude_api_metered=claude_api_metered,
    )


# ---------------------------------------------------------------------------
# Lane executors
# ---------------------------------------------------------------------------

def _execute_claude(
    plan: ExecutionPlan,
    permit: ExecutionPermit,
    *,
    state_dir: Path,
    data_dir: Path,
    role: Optional[str] = None,
) -> int:
    """Execute a validated claude_tmux_subscription plan via TmuxInteractiveDispatch.

    require_permit is the first action — un-evadable. P0-3: sha256 of the instruction
    file is re-verified immediately before delivery to detect TOCTOU swaps.
    """
    from tmux_interactive_dispatch import (  # noqa: PLC0415
        TmuxInteractiveDispatch,
        _resolve_invocation_project_root,
    )

    require_permit(plan, permit)  # un-evadable gate — FIRST action, cannot be moved

    # P0-3 (PR-4c): REQUIRE a valid 64-hex plan hash before delivery — fail-CLOSED.
    # The old `if plan.instruction_sha256:` guard fell OPEN on an empty hash, letting
    # an empty-hash plan + valid permit spawn mutated content. No hash → no spawn.
    if not is_valid_instruction_hash(plan.instruction_sha256):
        raise PermissionError(
            f"plan.instruction_sha256 is not a valid 64-hex digest "
            f"(got {plan.instruction_sha256!r}); refusing to deliver (fail-closed)"
        )

    # TOCTOU verification — re-read and verify sha256 before delivering
    instruction = Path(plan.instruction_file).read_text(encoding="utf-8")
    actual = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
    if actual != plan.instruction_sha256:
        raise PermissionError(
            f"instruction file mutated after permit: sha256 mismatch "
            f"(expected {plan.instruction_sha256[:12]}…, got {actual[:12]}…)"
        )

    # Thread the PROJECT repo root from the invocation context (VNX_PROJECT_ROOT /
    # cwd-git), NOT the lane code's __file__: in central-install mode the code lives
    # under the shared keystone, so the constructor's __file__ fallback would spawn
    # the worker in the keystone instead of the operator's project.
    lane = TmuxInteractiveDispatch(
        state_dir, project_root=_resolve_invocation_project_root()
    )
    result = lane.dispatch(
        instruction,
        plan.dispatch_id,
        role=role,
        model=plan.model,
        dispatch_paths=[str(dp.path) for dp in plan.dispatch_paths],
        deadline_seconds=plan.deadline_seconds,
        base_ref=plan.base_ref,
        isolated_worktree=True,
        requires_mcp=plan.requires_mcp,
    )
    return 0 if result.success else 1


def _execute_claude_headless(
    plan: ExecutionPlan,
    permit: ExecutionPermit,
    *,
    state_dir: Path,
    data_dir: Path,
    role: Optional[str] = None,
) -> int:
    """Execute a validated claude_headless plan via ClaudeSubprocessAdapter.

    Billing is auth-derived, not lane-derived (OI-1156): without an own
    ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL this bills as ``subscription``, not
    ``api_metered`` — see dispatch_plan.claude_auth_is_api_metered.

    Delegates all permit verification, TOCTOU check, and GOVERN to
    run_envelope_headless_plan — same security contract as the provider lane.

    Fail-closed last gate before spawn: consults lane_safety.headless_block from
    routing_policy.yaml (OI-223) instead of a hardcoded env check, so the yaml stays
    the single source of truth for the block and its override var name.
    """
    from routing_policy import is_claude_headless_blocked, load_lane_safety  # noqa: PLC0415
    lane_safety = load_lane_safety()
    if is_claude_headless_blocked(lane_safety):
        override_env = (lane_safety.get("headless_block") or {}).get(
            "override_env", "VNX_OVERRIDE_CLAUDE_HEADLESS"
        )
        raise PermissionError(
            f"claude_headless lane blocked by default; set {override_env}=1 to opt in"
        )
    result = run_envelope_headless_plan(plan, permit, state_dir=state_dir, data_dir=data_dir, role=role)
    return result.returncode


# ---------------------------------------------------------------------------
# OI-1120 part 2 — dispatch register fill (the guard's reference source)
# ---------------------------------------------------------------------------

def _register_dispatch_created(
    plan: ExecutionPlan,
    permit: ExecutionPermit,
    spec: DispatchSpec,
    *,
    state_dir: Path,
) -> None:
    """Emit ``dispatch_created`` to the register at the moment the door
    commits to firing (permit issued, about to invoke the lane).

    OI-1105/OI-1120: none of the four lanes (tmux-spawn, provider, envelope,
    subprocess adapter) ever wrote this event, so
    ``report_to_receipt_converter._is_known_dispatch`` — the guard that cross-
    checks a report's dispatch_id against the register — had almost nothing
    to check against. One write site here, before the lane branch, covers
    every lane (``provider``, ``claude_tmux_subscription``, ``claude_headless``)
    so a future lane inherits the register entry for free instead of needing
    its own hook.

    Idempotent via ``dispatch_register.append_event_idempotent`` and never
    blocks the door: a register-write failure is an observability gap, not a
    reason to refuse work. The one exception is ``TestIsolationGuardError``
    (OI-1079), which must propagate so a test that lost its isolation fails
    loudly instead of silently writing into the real central store — the
    same contract ``dispatch_register.append_event`` already enforces
    internally; this call site must not re-swallow it.

    Deliberately omits ``project_id`` (no cross-project central-mirror
    write): ``state_dir`` here is already the door's own ADR-026-resolved
    per-project authority (``_resolve_data_dir`` / ``_authority_from_spec_path``),
    the same single write target ``_persist_route_decision`` and
    ``_persist_dispatch_row`` use. Passing ``project_id`` would additionally
    ask the register to mirror into ``~/.vnx-data/<project_id>`` — redundant
    in production (that IS ``state_dir`` there, so the mirror's own
    cutover guard no-ops it) and, under pytest with an isolated tmp
    ``state_dir``, indistinguishable from the exact leak class OI-1079
    closed even though this write was never headed to production.
    """
    from vnx_paths import TestIsolationGuardError  # noqa: PLC0415
    try:
        import dispatch_register  # noqa: PLC0415
        from gate_obligations import pr_number_from_pr_id  # noqa: PLC0415
        dispatch_register.append_event_idempotent(
            "dispatch_created",
            dispatch_id=plan.dispatch_id,
            pr_number=pr_number_from_pr_id(spec.pr_id),
            terminal=spec.target_slot,
            gate=spec.gate,
            extra={
                "lane": plan.lane,
                "provider": plan.provider.value,
                "model": plan.model,
                "permit_fingerprint": f"{permit.plan_digest[:12]}-{permit.dispatch_id}",
            },
            state_dir=state_dir,
        )
    except TestIsolationGuardError:  # vnx-silent-except: OI-1079 — must fail the test loudly, never swallowed here
        raise
    except Exception as exc:  # vnx-silent-except: register bookkeeping must never block the door; dispatch_created is observability, not a gate
        logger.warning(
            "[dispatch_cli] WARN dispatch_created register emit failed for dispatch=%s: %s",
            plan.dispatch_id, exc,
        )


# ---------------------------------------------------------------------------
# OI-849 — route decision persistence
# ---------------------------------------------------------------------------

def _persist_route_decision(
    plan: ExecutionPlan,
    permit: ExecutionPermit,
    *,
    state_dir: Path,
    isolation_note: "Optional[str]" = None,
) -> None:
    """Persist the canonical routing decision alongside the permit fingerprint.

    Writes to two locations in the existing route_decisions stream:
    1. state_dir/route_decisions.ndjson — append-locked NDJSON record
    2. state_dir/route_decisions/<dispatch_id>.json — per-dispatch atomic file

    The stored canonical dict is the same one digest() hashes, so the fingerprint
    can be verified against it later — a stored decision that can't be linked to
    its permit would repeat the same problem one layer higher (OI-849).

    ``isolation_note`` (OI-1158): an optional loud isolation-guarantee warning
    (see ``_headless_isolation_guard``) stored as a SIBLING of ``decision``, not
    inside it — ``canonical_dict()``/``digest()`` deliberately excludes advisory
    fields, so this never perturbs the permit fingerprint. This is the
    "receipt-visible field" half of OI-1158's fix: the audit trail this door
    writes for every dispatch, closest in spirit to a receipt for callers who
    only have write access to this module.

    Never blocks the door: any failure logs a WARN and the dispatch continues.
    """
    import json as _json
    from datetime import datetime, timezone

    from state_writer import append_locked

    try:
        canonical = plan.canonical_dict()
        timestamp = datetime.now(timezone.utc).isoformat()
        record = {
            "timestamp": timestamp,
            "dispatch_id": plan.dispatch_id,
            "fingerprint": f"{permit.plan_digest[:12]}-{permit.dispatch_id}",
            "plan_digest": permit.plan_digest,
            "decision": canonical,
        }
        if isolation_note:
            record["isolation_warning"] = isolation_note

        # 1. Append to shared NDJSON under the sentinel + data-file locks.
        ndjson_path = state_dir / "route_decisions.ndjson"
        append_locked(ndjson_path, record)

        # 2. Write per-dispatch JSON atomically so receipt-converter and other
        #    consumers can look up a single dispatch's decision by id.
        per_dispatch_dir = state_dir / "route_decisions"
        per_dispatch_dir.mkdir(parents=True, exist_ok=True)
        per_dispatch_path = per_dispatch_dir / f"{plan.dispatch_id}.json"
        tmp = per_dispatch_path.with_suffix(".tmp")
        tmp.write_text(_json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(per_dispatch_path)

        logger.info(
            "[dispatch_cli] route decision persisted: dispatch=%s fingerprint=%s",
            plan.dispatch_id,
            f"{permit.plan_digest[:12]}-{permit.dispatch_id}",
        )
    except Exception as exc:  # vnx-silent-except: route-decision persistence is best-effort; must never block the door
        logger.warning(
            "[dispatch_cli] WARN route decision persist failed for dispatch=%s: %s",
            plan.dispatch_id,
            exc,
        )


# ---------------------------------------------------------------------------
# run_dispatch — the single door
# ---------------------------------------------------------------------------

def run_dispatch(spec_file: Path, *, dry_run: bool = False) -> int:
    """Turn a spec file into a governed dispatch for BOTH lanes.

    Returns 0 on success, 1 on any reject or execution failure.
    When dry_run=True, prints plan + permit fingerprint and spawns nothing.
    """
    # Authority = where the bundle is PHYSICALLY staged, not ambient CWD/env. In a
    # central install the door's CWD is the shared engine tree (its stray
    # .vnx-project-id would mis-resolve every consumer to vnx-dev). Fall back to
    # ambient resolution only when the spec isn't under the staged-bundle layout
    # (ad-hoc/test specs).
    derived_pid, derived_data_dir = _authority_from_spec_path(spec_file)

    # ADR-007 independent-authority cross-check (codex gate PR #1093): when the OPERATOR
    # explicitly pins the tenant / data root (VNX_PROJECT_ID / VNX_DATA_DIR_EXPLICIT), the
    # staged-bundle authority MUST agree. This restores the anti-redirect guard whenever an
    # independent authority exists — a bundle physically staged under a different project's
    # store than the pinned one is a cross-project redirect and is rejected. Without an
    # explicit pin (the common central-install case) the store's filesystem write-access is
    # the trust boundary, per the PR-4d model ("resolved data root is OPERATOR config; the
    # threat model is our own agents, not an external adversary").
    if derived_pid:
        env_pid = (os.environ.get("VNX_PROJECT_ID") or "").strip()
        if env_pid and env_pid != derived_pid:
            _emit_reject(Reject(
                "project-mismatch",
                f"staged-bundle project_id={derived_pid!r} != pinned VNX_PROJECT_ID={env_pid!r}; "
                "caller cannot redirect state to another project",
            ))
            return 1
        if os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1":
            explicit = (os.environ.get("VNX_DATA_DIR") or "").strip()
            if (
                explicit
                and derived_data_dir is not None
                and Path(explicit).resolve() != derived_data_dir.resolve()
            ):
                _emit_reject(Reject(
                    "project-mismatch",
                    f"staged-bundle data_dir={derived_data_dir} != pinned VNX_DATA_DIR={explicit}; "
                    "refusing to write state outside the operator-pinned data root",
                ))
                return 1

    project_id = derived_pid or _resolve_project_id()
    repo_root = _resolve_repo_root()
    data_dir = derived_data_dir or _resolve_data_dir(derived_pid)
    state_dir = data_dir / "state"

    try:
        spec = load_spec(spec_file)
    except Exception as exc:
        print(f"[dispatch_cli] REJECT [spec-parse-error]: {exc}", file=sys.stderr)
        return 1

    # OI-962: resolve provider+model via smart router BEFORE validate().
    # The router fills in provider+model when the spec carries none (AUTO),
    # then validate() tests the resolved values against constraints.  This
    # keeps governance intact: a route that violates constraints is still
    # rejected by the full validation chain — only the order changed.
    # Deterministic fallback: when the router declines (T0, disabled,
    # classifier error), AUTO resolves to CLAUDE so compile_plan never sees an
    # unresolved AUTO.  This is the same hard-default the old bridge alias
    # provided, now applied AFTER the router had its chance to fill in a
    # cheaper provider.
    door_route_reason: Optional[str] = None
    if spec.provider == Provider.AUTO:
        try:
            from providers.provider_registry import RegistryLookupError  # noqa: PLC0415

            result = _resolve_router_pre_validate(spec)
        except RegistryLookupError as exc:
            # ADR-036 §2: an unknown provider/model is drift between the router
            # and the registry — reject loudly, never silently fall back to the
            # default lane. No dispatch, no receipt of a half-run.
            _emit_reject(Reject(
                "router-registry-drift",
                str(exc),
            ))
            return 1
        if result is not None and result.route is not None:
            new_provider, new_model, route_reason = result.route
            spec = dataclasses.replace(spec, provider=new_provider, model=new_model)
            door_route_reason = route_reason
        else:
            # Router declined or could not resolve (T0, VNX_SMART_ROUTER_DISABLE,
            # or a classifier exception) — fall back to CLAUDE. OI-1050:
            # this must set provider AND model TOGETHER, in the same replace() call
            # that resolves the router's own success path above. Setting provider
            # alone previously left spec.model=None, and the workers-kimi-pinned
            # "default" pin semantics then filled effective_model from ITS OWN pin
            # (kimi-k3) whenever spec.model was empty — producing a spec with
            # provider=claude and model=kimi-k3, which kimi-via-cli-only correctly
            # rejects.
            #
            # OI-1187: the fallback model is tier-aware. A tier-high dispatch whose
            # routing failed must never silently land on sonnet (a quality class
            # drop); it falls back to opus-5 (equal class) instead. Only when the
            # classifier did not determine a tier (or the router returned None on an
            # unexpected error) does the historical "sonnet" default apply.
            tier = result.tier if result is not None else None
            fallback_model = _tier_aware_fallback_model(tier, spec.model)
            spec = dataclasses.replace(spec, provider=Provider.CLAUDE, model=fallback_model)
            decline_reason = result.decline_reason if result is not None else None
            if decline_reason:
                door_route_reason = (
                    f"smart-router:no-route,reason={decline_reason},fallback={fallback_model}"
                )
            else:
                door_route_reason = f"smart-router:no-route,fallback={fallback_model}"

    vspec = validate(spec, project_id=project_id, repo_root=repo_root)
    if isinstance(vspec, Reject):
        _emit_reject(vspec)
        return 1

    # Punt 7 (gate-weight-by-variant): fill the review-gate from the router when
    # the spec is silent. The gate reason is merged into door_route_reason so the
    # chosen variant + reason are visible in the dry-run output and carried on
    # the plan (route_reason), never a silent lighter gate.
    vspec, gate_reason = _resolve_gate_via_router(vspec)
    if gate_reason:
        door_route_reason = (
            f"{door_route_reason};{gate_reason}" if door_route_reason else gate_reason
        )

    # P1-#1: wrap everything after validate in try/except — door never panics
    try:
        snapshot = build_runtime_snapshot(vspec, data_dir=data_dir, spec_file=spec_file)

        plan = compile_plan(vspec, snapshot)
        if isinstance(plan, Reject):
            _emit_reject(plan)
            return 1

        # Merge the door route reason into the plan so it's visible in dry-run
        # output and carried on the ExecutionPlan.
        if door_route_reason:
            plan = dataclasses.replace(
                plan,
                route_reason=f"{door_route_reason};{plan.route_reason}",
            )

        # OI-1158: loud isolation-guarantee warning for lanes the door cannot
        # structurally verify (currently: claude_headless). Merged onto
        # plan.warnings — the same mechanism door_route_reason and the D4
        # model-tier warnings already use — so it surfaces in dry-run output
        # via _print_plan AND is available below for the real-execution print
        # and the route-decision receipt field. None for every other lane.
        headless_isolation_warning = _headless_isolation_guard(plan)
        if headless_isolation_warning:
            plan = dataclasses.replace(
                plan,
                warnings=plan.warnings + (headless_isolation_warning,),
            )

        # Scout pre-pass (opt-in VNX_SCOUT_PREPASS, fail-open): a cheap key-auth
        # model ranks the deterministic anchors into a sidecar BEFORE the permit
        # is issued. It reads vspec.instruction_text in-memory and writes a
        # SEPARATE sidecar file — it never touches the instruction, so the
        # permit / instruction_sha256 TOCTOU below is untouched. Never blocks the
        # door (best-effort, never raises).
        if not dry_run:
            try:
                from scout_prepass import maybe_run_scout
                maybe_run_scout(
                    dispatch_id=plan.dispatch_id,
                    instruction_text=vspec.instruction_text,
                    dispatch_paths=[dp.path for dp in vspec.spec.dispatch_paths],
                    state_dir=state_dir,
                    task_class=getattr(vspec.spec, "task_class", None),
                    lane=plan.lane,
                )
            except Exception as exc:
                logger.debug("[dispatch_cli] scout pre-pass skipped: %s", exc)

            # The dispatch is irrevocably accepted here (validated, plan
            # compiled): create its dispatches tracker row BEFORE the lane
            # choice so _persist_track_id, reconcile_all_dispatch_outcomes and
            # the TL-D2 pr_ref propagation all have a row to read. Idempotent
            # (retry/fix-forward safe), state='proposed' (invisible to the
            # claim/stuck/ghost sweeps), best-effort — never blocks the door.
            _persist_dispatch_row(
                vspec.spec,
                state_dir=state_dir,
                worker_claude_override_reason=snapshot.worker_claude_override_reason,
            )

            # OI-876/OI-881: a declared gate is an obligation, not decoration.
            # Registered here — right after the dispatch is irrevocably
            # accepted — so every accepted dispatch with gate=<name> has a
            # checkable evidence trail from this point on.
            _register_gate_obligation(vspec.spec, state_dir=state_dir)

            # TL-D1: export the resolved track_id alongside VNX_CURRENT_DISPATCH_ID and
            # persist it onto the dispatch tracker row so D2 can propagate it to
            # track.pr_ref on merge. Best-effort — never blocks the door.
            if vspec.spec.track_id:
                os.environ["VNX_CURRENT_TRACK_ID"] = vspec.spec.track_id
                _persist_track_id(vspec.spec, state_dir=state_dir)

            # Chain-link (dispatch-20260802-model-ssot-en-ketenlink): export the
            # resolved fields so the tmux worker pane (and any worker-authored
            # receipt) inherits them — the receipt writers read these env vars as
            # a fallback when the caller did not pass explicit values. Only set
            # when present, so unrelated dispatches keep a clean env.
            if plan.parent_dispatch:
                os.environ["VNX_PARENT_DISPATCH"] = plan.parent_dispatch
            if plan.task_class:
                os.environ["VNX_TASK_CLASS"] = plan.task_class
            if plan.tier_from:
                os.environ["VNX_TIER_FROM"] = plan.tier_from
            if plan.tier_to:
                os.environ["VNX_TIER_TO"] = plan.tier_to
            # OI-1137: the work-ref / pr-id are exported so the tmux-lane phantom-guard can
            # weigh the pushed branch diff for a fix-forward dispatch (its own worktree reads
            # empty). Same fallback pattern as VNX_PARENT_DISPATCH above.
            if plan.work_ref:
                os.environ["VNX_WORK_REF"] = plan.work_ref
            if plan.pr_id:
                os.environ["VNX_PR_ID"] = plan.pr_id
            # The resolved model is exported too, so governance corrective
            # receipts (phantom_guard / pr_enforcement) can record the model the
            # dispatch ran without threading a parameter through every call site.
            os.environ["VNX_CURRENT_MODEL"] = plan.model

        permit = issue_permit(plan)
        try:
            require_permit(plan, permit)  # P1-#6: door backstop for BOTH lanes
        except PermissionError as exc:
            raise _InvariantViolation(f"permit invariant breached: {exc}") from exc
        fp = fingerprint(permit)
        logger.info("[dispatch_cli] permit fingerprint: %s", fp)

        # OI-849: persist the full canonical routing decision alongside the
        # permit fingerprint so "which model got this task and why" is
        # answerable after the fact. Best-effort — never blocks the door.
        # OI-1120 part 2: fill the register's dispatch_created event here too —
        # same "door commits to firing" moment, one write site for every lane.
        if not dry_run:
            _persist_route_decision(
                plan, permit, state_dir=state_dir,
                isolation_note=headless_isolation_warning,
            )
            _register_dispatch_created(plan, permit, vspec.spec, state_dir=state_dir)

        if dry_run:
            _print_plan(plan, fp)
            # OI-867: reachability check — verify the lane endpoint is
            # responsive BEFORE the dispatch is approved.  Never fail-closed
            # on a transient network hiccup; auth-rejection is a hard
            # warning.  Provider lane availability is cheap to check (one
            # HTTP call) and catches the exact class of silent failure that
            # the 20260730-phantom-branch-fallback dispatch hit (litellm
            # proxy 401 → 43ms dispatch death with no error trace).
            _check_reachability(plan, vspec.spec)
            return 0

        with serialize_lane(plan.serialization_class, dispatch_id=vspec.spec.dispatch_id):
            if plan.lane == "provider":
                result = run_envelope_plan(plan, permit, state_dir=state_dir, data_dir=data_dir)
                if result.status != "success":
                    # Fail-loud: the door must never swallow a provider-lane failure into a
                    # bare exit code — the caller (bin/vnx dispatch) prints nothing else.
                    print(
                        f"[dispatch_cli] provider lane {result.status}: "
                        f"{result.error or '(no error captured)'}",
                        file=sys.stderr,
                    )
                return result.returncode
            elif plan.lane == "claude_tmux_subscription":
                return _execute_claude(
                    plan,
                    permit,
                    state_dir=state_dir,
                    data_dir=data_dir,
                    role=vspec.spec.role,
                )
            elif plan.lane == "claude_headless":
                # OI-1158: the door never printed anything about isolation on a
                # real (non-dry-run) fire — _print_plan's warnings loop only
                # runs under --dry-run. Surface the same warning here, loud, so
                # a live headless dispatch is never silent about the gap.
                if headless_isolation_warning:
                    logger.warning("[dispatch_cli] %s", headless_isolation_warning)
                    print(
                        f"[dispatch_cli] [WARN] {headless_isolation_warning}",
                        file=sys.stderr,
                    )
                return _execute_claude_headless(
                    plan,
                    permit,
                    state_dir=state_dir,
                    data_dir=data_dir,
                    role=vspec.spec.role,
                )
            else:
                raise _InvariantViolation(
                    f"closed set violated — unknown lane: {plan.lane!r}"
                )

    except _InvariantViolation as exc:
        logger.error(
            "[dispatch_cli] INVARIANT VIOLATION dispatch=%s: %s",
            getattr(getattr(vspec, "spec", None), "dispatch_id", "?"), exc,
        )
        print(f"[dispatch_cli] REJECT [invariant-violation]: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[dispatch_cli] REJECT [runtime-error]: {exc}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="VNX single-entry dispatch gate (PR-4)"
    )
    parser.add_argument(
        "--spec-file", type=Path, dest="spec_file",
        help="Absolute path to dispatch-spec.json",
    )
    parser.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Print plan + fingerprint; spawn nothing",
    )
    parser.add_argument(
        "--force-release-lock", dest="force_release_class",
        metavar="CLASS", nargs="?", const="claude-tmux", default=None,
        help="Release stale lock for CLASS (default: claude-tmux); "
             "prints prior holder and removes lock file",
    )
    args = parser.parse_args(argv)

    if args.force_release_class is not None:
        force_release(args.force_release_class)
        return 0

    if args.spec_file is None:
        parser.error("--spec-file is required")

    return run_dispatch(args.spec_file, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
