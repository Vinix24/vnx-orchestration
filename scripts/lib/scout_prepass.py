"""scout_prepass — sidecar contract for the cheap-model scout pre-pass.

The scout pre-pass is a cheap-model recon that ranks the deterministic
code-anchor candidates for a dispatch into INCLUDE/MAYBE/EXCLUDE verdicts plus a
short plan sketch, written to a per-dispatch ``scout_context.json`` sidecar
BEFORE the permit is issued. This module owns both ends: the producer
(``maybe_run_scout`` — the door pre-pass + key-auth provider invocation, 5b) and
the consumer (sidecar location, fail-open read, and the rendering of the injected
sketch, 5a) so the intelligence layer consumes a sidecar the moment one exists.

Design contract:
  - The sidecar is a SEPARATE file keyed by dispatch_id — it never mutates the
    instruction, so the permit / instruction_sha256 TOCTOU is untouched.
  - Reading is best-effort and fail-open: a missing / malformed sidecar yields
    None and the worker falls back to the deterministic code_anchor injection.
  - The rendered sketch is bounded (SCOUT_SKETCH_MAX_CHARS) and pointer-only —
    file:line ranges, never code bodies.

Sidecar schema (v1):
    {
      "schema_version": 1,
      "dispatch_id": "<id>",
      "generated_at": "<ISO8601>",
      "provider": "deepseek",
      "model": "deepseek-v4-flash",
      "include": [{"ref": "scripts/lib/foo.py:10-20", "why": "..."}],
      "maybe":   [{"ref": "scripts/lib/foo.py:50-60", "why": "..."}],
      "exclude": [{"ref": "...", "why": "..."}],
      "tests":   ["tests/test_foo.py"],
      "docs":    ["docs/foo.md"],
      "plan_sketch": "one or two line sketch"
    }
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SCOUT_SUBDIR = "scout"
SCOUT_SIDECAR_SUFFIX = ".json"
SCOUT_RECEIPTS_FILE = "scout_receipts.ndjson"
SCHEMA_VERSION = 1

try:
    import fcntl
except ImportError:  # pragma: no cover - non-posix
    fcntl = None  # type: ignore[assignment]

# Path-containment contract: a dispatch_id becomes a filesystem path segment, so
# it must not carry separators, '..', or absolute-path markers. Mirrors
# worker_permission_relay._safe_dispatch_id so the scout sidecar can never escape
# <state_dir>/scout/ via a malformed / hostile id (kimi-gate finding).
_SAFE_DISPATCH_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_dispatch_id(dispatch_id: str) -> str:
    """Return *dispatch_id* unchanged if safe as a path segment, else raise ValueError."""
    if not isinstance(dispatch_id, str) or not dispatch_id:
        raise ValueError("dispatch_id must be a non-empty string")
    if dispatch_id in (".", ".."):
        raise ValueError(f"unsafe dispatch_id {dispatch_id!r}: '.'/'..' are path-traversal segments")
    if not _SAFE_DISPATCH_ID.match(dispatch_id):
        raise ValueError(
            f"unsafe dispatch_id {dispatch_id!r}: must match [A-Za-z0-9._-]+ "
            "(no path separators, '..', or absolute paths)"
        )
    return dispatch_id

# The rendered sketch competes for the 2000-char direct-injection payload budget,
# so keep it well under that. Pointers + a one-line plan fit comfortably.
SCOUT_SKETCH_MAX_CHARS = 1200

# Defensive caps so a malformed / oversized sidecar can never blow the budget.
_MAX_REFS_PER_BUCKET = 8
_MAX_AUX_ITEMS = 5
_MAX_WHY_CHARS = 120
_MAX_PLAN_CHARS = 300


def scout_sidecar_path(state_dir: "Path | str", dispatch_id: str) -> Path:
    """Return the per-dispatch sidecar path: ``<state_dir>/scout/<dispatch_id>.json``.

    Raises ValueError when dispatch_id is not a safe path segment (traversal
    guard). Callers on the read path catch this and fail open.
    """
    return Path(state_dir) / SCOUT_SUBDIR / f"{_safe_dispatch_id(dispatch_id)}{SCOUT_SIDECAR_SUFFIX}"


def read_scout_sidecar(state_dir: "Path | str | None", dispatch_id: str) -> Optional[Dict[str, Any]]:
    """Best-effort read of a scout sidecar. Returns a dict, or None on any miss.

    Fail-open: a missing file, unreadable file, malformed JSON, or non-object
    payload all return None so the caller degrades to the deterministic anchors.
    """
    if state_dir is None or not dispatch_id:
        return None
    try:
        path = scout_sidecar_path(state_dir, dispatch_id)
    except ValueError as exc:
        # Hostile / malformed dispatch_id — fail open, never read outside scope.
        logger.debug("read_scout_sidecar: rejected unsafe dispatch_id %r: %s", dispatch_id, exc)
        return None
    try:
        if not path.is_file():
            return None
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("read_scout_sidecar: unreadable sidecar %s: %s", path, exc)
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.debug("read_scout_sidecar: malformed sidecar %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    # Defense-in-depth (kimi-gate advisory): ignore a sidecar from a future
    # schema or a different dispatch — a misplaced / stale file must never be
    # misread as this dispatch's context.
    sv = data.get("schema_version")
    if sv is not None and sv != SCHEMA_VERSION:
        logger.debug("read_scout_sidecar: unsupported schema_version %r for %s", sv, dispatch_id)
        return None
    sid = data.get("dispatch_id")
    if sid is not None and str(sid) != str(dispatch_id):
        logger.debug("read_scout_sidecar: sidecar dispatch_id %r != requested %r", sid, dispatch_id)
        return None
    return data


def _coerce_ref_list(value: Any) -> List[Dict[str, str]]:
    """Normalize an include/maybe/exclude bucket to a list of {ref, why} dicts."""
    out: List[Dict[str, str]] = []
    if not isinstance(value, list):
        return out
    for entry in value[:_MAX_REFS_PER_BUCKET]:
        if isinstance(entry, dict):
            ref = str(entry.get("ref") or "").strip()
            why = str(entry.get("why") or "").strip()
        elif isinstance(entry, str):
            ref, why = entry.strip(), ""
        else:
            continue
        if not ref:
            continue
        out.append({"ref": ref, "why": why[:_MAX_WHY_CHARS]})
    return out


def _coerce_str_list(value: Any) -> List[str]:
    out: List[str] = []
    if not isinstance(value, list):
        return out
    for entry in value[:_MAX_AUX_ITEMS]:
        s = str(entry).strip()
        if s:
            out.append(s)
    return out


def normalize_sidecar(sidecar: Dict[str, Any]) -> Dict[str, Any]:
    """Return a defensively-normalized view of the sidecar buckets.

    Coerces shapes, caps list lengths and string sizes, and drops junk — so the
    renderer (and any other consumer) works against a known-bounded structure
    regardless of what the producing model emitted.
    """
    return {
        "provider": str(sidecar.get("provider") or "").strip(),
        "include": _coerce_ref_list(sidecar.get("include")),
        "maybe": _coerce_ref_list(sidecar.get("maybe")),
        "exclude": _coerce_ref_list(sidecar.get("exclude")),
        "tests": _coerce_str_list(sidecar.get("tests")),
        "docs": _coerce_str_list(sidecar.get("docs")),
        "plan_sketch": str(sidecar.get("plan_sketch") or "").strip()[:_MAX_PLAN_CHARS],
    }


def sidecar_evidence_count(sidecar: Dict[str, Any]) -> int:
    """Number of ranked pointers the sketch carries (include + maybe)."""
    norm = normalize_sidecar(sidecar)
    return len(norm["include"]) + len(norm["maybe"])


def format_scout_sketch(sidecar: Dict[str, Any]) -> str:
    """Render the injected scout sketch markdown (pointer-only, bounded).

    EXCLUDE verdicts are intentionally NOT rendered — they only shrink the
    INCLUDE set and would add noise the worker should not anchor on. Returns ""
    when there is nothing useful to inject.
    """
    norm = normalize_sidecar(sidecar)
    if not (norm["include"] or norm["maybe"] or norm["plan_sketch"]
            or norm["tests"] or norm["docs"]):
        return ""

    provider = norm["provider"] or "cheap recon model"
    lines: List[str] = [
        "## SCOUT PRE-PASS (cheap-model recon — ranked grounding)",
        "",
        f"> A {provider} pre-pass ranked the candidate ranges below. Start with "
        "INCLUDE, skim MAYBE. These are pointers to live code — open each range "
        "and re-read if it looks stale.",
        "",
    ]
    if norm["include"]:
        lines.append("**Start here (INCLUDE):**")
        for it in norm["include"]:
            lines.append(_ref_line(it))
        lines.append("")
    if norm["maybe"]:
        lines.append("**Maybe relevant:**")
        for it in norm["maybe"]:
            lines.append(_ref_line(it))
        lines.append("")
    if norm["tests"]:
        lines.append("**Relevant tests:** " + ", ".join(f"`{t}`" for t in norm["tests"]))
    if norm["docs"]:
        lines.append("**Relevant docs:** " + ", ".join(f"`{d}`" for d in norm["docs"]))
    if norm["plan_sketch"]:
        lines.append("")
        lines.append(f"**Plan sketch:** {norm['plan_sketch']}")

    rendered = "\n".join(lines).rstrip()
    if len(rendered) <= SCOUT_SKETCH_MAX_CHARS:
        return rendered
    # Hard cap: truncate on a line boundary so we never emit a half-pointer.
    clipped = rendered[:SCOUT_SKETCH_MAX_CHARS]
    nl = clipped.rfind("\n")
    if nl > 0:
        clipped = clipped[:nl]
    return clipped.rstrip()


def _ref_line(item: Dict[str, str]) -> str:
    ref = item.get("ref", "")
    why = item.get("why", "")
    return f"- `{ref}` — {why}" if why else f"- `{ref}`"


# ---------------------------------------------------------------------------
# Producer — the door pre-pass (build-step 5b)
# ---------------------------------------------------------------------------
#
# Runs AFTER compile_plan / BEFORE issue_permit in dispatch_cli.run_dispatch.
# A cheap, key-auth model (DeepSeek-Flash via the classifier harness — NEVER a
# claude/subscription lane) ranks the deterministic code-anchor candidates into
# INCLUDE/MAYBE/EXCLUDE + a plan sketch, written to the sidecar. Opt-in, gated,
# fail-open (any miss → no sidecar → the deterministic code_anchor injection
# stands). It never reads or rewrites the instruction file, so the permit /
# instruction_sha256 TOCTOU is untouched.

# Pathless dispatches are scouted via instruction-driven discovery (git-grep over
# the repo for the instruction's salient terms), so they need a richer instruction
# to discover from than a path-bearing dispatch does.
_MIN_INSTRUCTION_CHARS_PATHLESS = 80
_MAX_DISCOVERED_FILES = 5
_MAX_DISCOVERY_TERMS = 6
_MAX_FILES_PER_DISCOVERY_TERM = 40
# Discovery prefers code files over docs at equal hit-count: a code task wants code
# anchors; a docs task hits mostly docs anyway and still gets them.
_CODE_EXTS = frozenset({
    "py", "js", "ts", "tsx", "jsx", "go", "rs", "java", "rb", "sh", "sql",
    "c", "cpp", "cc", "h", "hpp", "php", "kt", "swift", "scala",
})
_MIN_INSTRUCTION_CHARS = 40
# task_classes where a code-anchor recon adds little (no source to rank).
_SKIP_TASK_CLASSES = frozenset({"docs_synthesis", "channel_response", "ops_watchdog"})
# Dispatch lanes the scout skips — claude_headless is the rare API-metered opt-in
# path; do not spend a scout call there.
_SKIP_LANES = frozenset({"claude_headless"})
# Allowlist of cheap key-auth classifier lanes the scout may use. Default-deny so
# a subscription lane (e.g. 'haiku') can NEVER run the scout, per the hard
# constraint "never a claude/subscription lane for the scout".
_ALLOWED_SCOUT_PROVIDERS = frozenset({"deepseek", "ollama", "gemini", "codex"})


def scout_prepass_enabled() -> bool:
    """Opt-in flag for the scout producer (default OFF). Resolved through config_runtime so an
    operator's dashboard toggle is honoured; absent a UI value this is exactly the env/default."""
    import config_runtime
    return config_runtime.get_bool("VNX_SCOUT_PREPASS")


def _scout_provider_name() -> str:
    name = (os.environ.get("VNX_SCOUT_PROVIDER") or "deepseek").strip().lower()
    if name not in _ALLOWED_SCOUT_PROVIDERS:
        # Default-deny: anything not a sanctioned key-auth lane (incl. the
        # subscription 'haiku') falls back to the key-auth default.
        logger.debug("scout: provider %r is not an allowed key-auth lane — using deepseek", name)
        return "deepseek"
    return name


def _scout_gate_ok(
    dispatch_paths: "List[str] | None",
    instruction_text: str,
    task_class: "str | None",
    lane: "str | None",
) -> bool:
    """Scope/lane/task_class gate. Paths are OPTIONAL: a pathless dispatch passes so
    the scout can DISCOVER candidate files from the instruction — it just needs a
    richer instruction to discover from. Still skips non-code and headless work."""
    instr = (instruction_text or "").strip()
    min_chars = _MIN_INSTRUCTION_CHARS if dispatch_paths else _MIN_INSTRUCTION_CHARS_PATHLESS
    if len(instr) < min_chars:
        return False
    if task_class and str(task_class).strip().lower() in _SKIP_TASK_CLASSES:
        return False
    if lane and str(lane).strip().lower() in _SKIP_LANES:
        return False
    return True


def _resolve_repo_root() -> Path:
    """The project repo root the scout ranks/greps against (VNX_PROJECT_ROOT-aware)."""
    try:
        from vnx_paths import resolve_paths
        return Path(resolve_paths()["PROJECT_ROOT"])
    except Exception:
        return Path(".")


def _discover_candidate_paths(instruction_text: str, repo_root: Path) -> List[str]:
    """Discover candidate files for a PATHLESS dispatch by git-grepping the repo for
    the instruction's salient terms. Returns repo-relative paths ranked by how many
    distinct terms hit each file. Best-effort — returns [] on any failure.

    This is what lets the scout give an instruction-only dispatch ("fix the tagger")
    warm file:line starting points instead of no scout at all.
    """
    try:
        from code_anchor_finder import extract_terms
    except ImportError:
        return []
    import subprocess
    from collections import Counter

    terms = [t for t in (extract_terms(instruction_text) or []) if len(t) >= 3]
    if not terms:
        return []
    hits: "Counter[str]" = Counter()
    for term in terms[:_MAX_DISCOVERY_TERMS]:
        try:
            out = subprocess.run(
                ["git", "grep", "-l", "-I", "--fixed-strings", "-e", term],
                cwd=str(repo_root), capture_output=True, text=True, timeout=5,
            )
        except Exception:
            continue
        if out.returncode not in (0, 1):  # 1 = no match (fine); other = git error
            continue
        files = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
        if len(files) > _MAX_FILES_PER_DISCOVERY_TERM:
            continue  # term too common to be a useful anchor
        for f in files:
            hits[f] += 1

    def _rank(item: "tuple[str, int]") -> "tuple[int, int]":
        f, count = item
        ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
        return (1 if ext in _CODE_EXTS else 0, count)

    return [f for f, _ in sorted(hits.items(), key=_rank, reverse=True)[:_MAX_DISCOVERED_FILES]]


def _candidate_refs(dispatch_paths: List[str], instruction_text: str) -> List[str]:
    """Deterministic code-anchor candidate refs (file:line ranges), pointer-only.

    A path-bearing dispatch is ranked within its own paths. A PATHLESS dispatch first
    has its candidate files discovered from the instruction, so it still gets warm
    anchors instead of no scout at all.
    """
    try:
        import code_anchor_finder as _caf
    except ImportError:
        return []
    repo_root = _resolve_repo_root()
    paths = list(dispatch_paths or [])
    if not paths:
        paths = _discover_candidate_paths(instruction_text, repo_root)
    if not paths:
        return []
    anchors = _caf.fetch_code_anchors(
        paths, instruction_text, refs_only=True, repo_root=repo_root
    )
    return [f"{a.file_path}:{a.line_start}-{a.line_end}" for a in anchors]


def _build_scout_prompt(instruction_text: str, candidate_refs: List[str]) -> str:
    """Bounded recon prompt — rank the candidates, JSON-only output."""
    refs_block = "\n".join(f"- {r}" for r in candidate_refs)
    instr = instruction_text.strip()
    if len(instr) > 2000:
        instr = instr[:2000] + " […]"
    return (
        "You are a fast code-recon scout. Rank the candidate code ranges below by "
        "how relevant each is to the task. Do NOT write code or summarize file "
        "contents — only rank the given pointers and name relevant tests/docs.\n\n"
        f"TASK:\n{instr}\n\n"
        f"CANDIDATE RANGES (use these exact refs, do not invent files):\n{refs_block}\n\n"
        "Respond with ONLY a JSON object of this shape:\n"
        '{"include":[{"ref":"<one of the candidates>","why":"<=12 words"}],'
        '"maybe":[{"ref":"...","why":"..."}],'
        '"exclude":[{"ref":"..."}],'
        '"tests":["tests/..."],"docs":["docs/..."],'
        '"plan_sketch":"<=2 sentence approach"}\n'
        "Only use refs from the candidate list. Keep it short."
    )


def _invoke_scout_model(
    prompt: str, provider_name: str
) -> "tuple[Optional[Dict[str, Any]], str, float, int]":
    """Call the cheap key-auth classifier.

    Returns (parsed JSON | None, model, cost_usd, latency_ms) — the cost/latency feed
    the scout receipt so the model call is cost-tracked in the governed audit trail.
    """
    try:
        from classifier_providers import get_provider
    except ImportError:
        return None, "", 0.0, 0
    try:
        provider = get_provider(provider_name)
    except ValueError:
        logger.debug("scout: unknown provider %r", provider_name)
        return None, "", 0.0, 0
    if not provider.is_available():
        logger.debug("scout: provider %r unavailable (no key / CLI)", provider_name)
        return None, "", 0.0, 0
    result = provider.classify(prompt)
    model = str((result.extra or {}).get("model") or "")
    cost = float(result.cost_usd or 0.0)
    latency_ms = int(result.latency_ms or 0)
    if result.error or not result.parsed_json:
        logger.debug("scout: provider %r returned no usable JSON (%s)", provider_name, result.error)
        return None, model, cost, latency_ms
    return result.parsed_json, model, cost, latency_ms


def _snap_refs(bucket: Any, allowed: "set[str]") -> List[Dict[str, str]]:
    """Keep only model verdicts whose ref is a real candidate (anti-hallucination).

    Producer-side caps (bucket length, why length) mirror normalize_sidecar so the
    on-disk sidecar is bounded even before the consumer re-normalizes it.
    """
    out: List[Dict[str, str]] = []
    if not isinstance(bucket, list):
        return out
    for entry in bucket:
        if len(out) >= _MAX_REFS_PER_BUCKET:
            break
        if isinstance(entry, dict):
            ref = str(entry.get("ref") or "").strip()
            why = str(entry.get("why") or "").strip()
        elif isinstance(entry, str):
            ref, why = entry.strip(), ""
        else:
            continue
        if ref in allowed:
            out.append({"ref": ref, "why": why[:_MAX_WHY_CHARS]})
    return out


def _assemble_sidecar(
    dispatch_id: str,
    parsed: Dict[str, Any],
    candidate_refs: List[str],
    provider_name: str,
    model: str,
    instruction_sha256: str = "",
) -> Dict[str, Any]:
    """Build the sidecar from the model verdicts, snapped to real candidates."""
    allowed = set(candidate_refs)
    return {
        "schema_version": SCHEMA_VERSION,
        "dispatch_id": dispatch_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": provider_name,
        "model": model,
        "scouted_instruction_sha256": instruction_sha256,
        "include": _snap_refs(parsed.get("include"), allowed),
        "maybe": _snap_refs(parsed.get("maybe"), allowed),
        "exclude": _snap_refs(parsed.get("exclude"), allowed),
        "tests": [str(t).strip() for t in (parsed.get("tests") or []) if str(t).strip()][:_MAX_AUX_ITEMS],
        "docs": [str(d).strip() for d in (parsed.get("docs") or []) if str(d).strip()][:_MAX_AUX_ITEMS],
        "plan_sketch": str(parsed.get("plan_sketch") or "").strip()[:_MAX_PLAN_CHARS],
    }


def write_scout_sidecar(state_dir: "Path | str", dispatch_id: str, sidecar: Dict[str, Any]) -> Path:
    """Atomically write the sidecar (tmp + os.replace). Raises ValueError on unsafe id."""
    path = scout_sidecar_path(state_dir, dispatch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(sidecar, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)
    return path


def scout_receipts_path(state_dir: "Path | str") -> Path:
    """The dispatch-linkable scout audit trail: ``<state_dir>/scout_receipts.ndjson``."""
    return Path(state_dir) / SCOUT_RECEIPTS_FILE


def _emit_scout_receipt(
    state_dir: "Path | str",
    dispatch_id: str,
    provider: str,
    model: str,
    cost_usd: float,
    latency_ms: int,
    sidecar: Dict[str, Any],
    sidecar_path: Path,
    instruction_sha256: str,
) -> None:
    """Append a scout receipt to ``scout_receipts.ndjson`` — the governed audit entry
    for the scout model call, keyed to ``dispatch_id`` and pointing at the sidecar.

    Kept SEPARATE from ``t0_receipts.ndjson`` (option B) so the receipt processor's
    dispatch-lifecycle logic is untouched; joining on ``dispatch_id`` reunites the
    scout cost/anchors with the dispatch's own receipt. Best-effort — an audit-write
    failure must never break the (fail-open) scout.
    """
    try:
        receipt = {
            "event_type": "scout_prepass",
            "dispatch_id": dispatch_id,
            "provider": provider,
            "model": model,
            "cost_usd": round(float(cost_usd or 0.0), 6),
            "duration_seconds": round((latency_ms or 0) / 1000.0, 3),
            "sidecar_path": str(sidecar_path),
            "scouted_instruction_sha256": instruction_sha256,
            "anchors": {
                "include": len(sidecar.get("include") or []),
                "maybe": len(sidecar.get("maybe") or []),
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        path = scout_receipts_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(receipt, separators=(",", ":")) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.write(line)
                fh.flush()
            finally:
                if fcntl is not None:
                    fcntl.flock(fh, fcntl.LOCK_UN)
    except Exception as exc:  # audit is best-effort, never blocks the scout
        logger.debug("scout receipt emit failed for %s: %s", dispatch_id, exc)


def _instruction_sha256(instruction_text: str) -> str:
    """SHA-256 of the instruction the scout ran against — recorded in the sidecar so
    a later instruction change (new digest) triggers a re-scout while an unchanged
    one is recognised as already scouted."""
    return hashlib.sha256((instruction_text or "").encode("utf-8")).hexdigest()


def is_scouted(
    state_dir: "Path | str | None",
    dispatch_id: str,
    instruction_sha256: "str | None" = None,
) -> bool:
    """True when a fresh scout sidecar exists for *dispatch_id* — the scouted flag.

    The sidecar's existence IS the flag. When *instruction_sha256* is given, the flag
    only holds if the sidecar was produced against that same instruction, so a changed
    instruction reads as not-yet-scouted (re-scout) rather than stale-clean. Fail-open:
    any read problem returns False.
    """
    sidecar = read_scout_sidecar(state_dir, dispatch_id)
    if not sidecar:
        return False
    if instruction_sha256 is None:
        return True
    return sidecar.get("scouted_instruction_sha256") == instruction_sha256


def maybe_run_scout(
    *,
    dispatch_id: str,
    instruction_text: str,
    dispatch_paths: "List[str] | None",
    state_dir: "Path | str",
    task_class: "str | None" = None,
    lane: "str | None" = None,
) -> "Optional[Path]":
    """Run the scout pre-pass and write a sidecar. Best-effort — NEVER raises.

    Returns the sidecar path on success, else None (fail-open → the deterministic
    code_anchor injection stands). Opt-in (``VNX_SCOUT_PREPASS=1``) and gated on
    scope/lane/task_class. Uses a cheap key-auth lane only (never the subscription).
    """
    if not scout_prepass_enabled():
        return None
    sha = _instruction_sha256(instruction_text)
    try:
        # Idempotent, and the source of the scouted flag: a fresh sidecar for this
        # exact instruction already stands (produced by the async pending sweep or a
        # prior pass), so skip the model call — a pre-scouted dispatch adds no
        # dispatch-time latency. A changed instruction (new sha) falls through and
        # re-scouts.
        if is_scouted(state_dir, dispatch_id, sha):
            return scout_sidecar_path(state_dir, dispatch_id)
        if not _scout_gate_ok(dispatch_paths, instruction_text, task_class, lane):
            return None
        candidate_refs = _candidate_refs(list(dispatch_paths or []), instruction_text)
        if not candidate_refs:
            return None
        provider_name = _scout_provider_name()
        prompt = _build_scout_prompt(instruction_text, candidate_refs)
        parsed, model, cost_usd, latency_ms = _invoke_scout_model(prompt, provider_name)
        if not parsed:
            return None
        sidecar = _assemble_sidecar(dispatch_id, parsed, candidate_refs, provider_name, model, sha)
        if not (sidecar["include"] or sidecar["maybe"] or sidecar["plan_sketch"]):
            return None  # nothing useful — don't write an empty sidecar
        path = write_scout_sidecar(state_dir, dispatch_id, sidecar)
        _emit_scout_receipt(
            state_dir, dispatch_id, provider_name, model,
            cost_usd, latency_ms, sidecar, path, sha,
        )
        return path
    except Exception as exc:  # fail-open: a scout failure must never block the door
        logger.debug("maybe_run_scout: fail-open for %s (%s)", dispatch_id, exc)
        return None


def scout_pending_sweep(
    data_dir: "Path | str",
    state_dir: "Path | str",
    *,
    limit: "int | None" = None,
) -> Dict[str, Any]:
    """Async pre-dispatch scout over the pending queue (lane-agnostic).

    For every staged bundle under ``<data_dir>/dispatches/pending/<dispatch_id>/``
    that is not already scouted against its current instruction, run the scout
    producer and write the sidecar BEFORE any lane picks the dispatch up. This is
    where the scout does its real work: it enriches the backlog ahead of time, so
    execution never waits on recon and the executing lane is irrelevant.

    Idempotent (skips already-scouted bundles via the sidecar sha flag), best-effort
    (a bad bundle is skipped, never raised), and a no-op when the scout is disabled.
    Returns a summary dict.
    """
    summary: Dict[str, Any] = {
        "scanned": 0, "produced": 0, "already": 0, "gated_out": 0, "errors": 0,
    }
    if not scout_prepass_enabled():
        summary["disabled"] = True
        return summary
    pending = Path(data_dir) / "dispatches" / "pending"
    if not pending.is_dir():
        return summary
    for bundle in sorted(p for p in pending.iterdir() if p.is_dir()):
        if limit is not None and summary["produced"] >= limit:
            break
        summary["scanned"] += 1
        try:
            dispatch_id = bundle.name
            instr_file = bundle / "instruction.md"
            if not instr_file.is_file():
                continue
            instruction_text = instr_file.read_text(encoding="utf-8")
            if is_scouted(state_dir, dispatch_id, _instruction_sha256(instruction_text)):
                summary["already"] += 1
                continue
            paths: List[str] = []
            task_class: Optional[str] = None
            lane: Optional[str] = None
            spec_file = bundle / "dispatch-spec.json"
            if spec_file.is_file():
                spec = json.loads(spec_file.read_text(encoding="utf-8"))
                for dp in spec.get("dispatch_paths") or []:
                    p = dp.get("path") if isinstance(dp, dict) else str(dp)
                    if p:
                        paths.append(p)
                task_class = spec.get("task_class") or spec.get("role")
                lane = spec.get("lane") or spec.get("provider")
            produced = maybe_run_scout(
                dispatch_id=dispatch_id,
                instruction_text=instruction_text,
                dispatch_paths=paths,
                state_dir=state_dir,
                task_class=task_class,
                lane=lane,
            )
            if produced is None:
                summary["gated_out"] += 1
            else:
                summary["produced"] += 1
        except Exception as exc:  # never let one bad bundle break the sweep
            summary["errors"] += 1
            logger.debug("scout_pending_sweep: skipped %s (%s)", bundle.name, exc)
    return summary
