"""Permission-event readers (ADR-075 decisions 2, 5, 8): B1 refused tool calls, B2 answered prompts, B4a copy blocks.

Pure over one transcript's lines; every event is built by ``permission_events.build_event``, so nothing raw
leaves this module. ``origin`` comes from the transcript dir slug and ``account`` from comparing the
``session-owner`` uuid with configured ``oauthAccount`` files; neither slug, path, uuid nor e-mail is kept.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import permission_events as pe
from permission_account_dirs import account_dirs

ACCOUNT_SLOTS = ("vnx", "sm")
ASK_PHRASES = ("draai dit", "draai deze", "draai het", "plak dit", "in een terminal", "in een eigen terminal",
               "bij jou", "met een !", "jij moet")
USER_SOURCES = {"user_temporary": "approved_once", "user_permanent": "approved_permanent", "user_reject": "refused_user"}
KNOWN_SOURCES = frozenset(("", "config", "hook", "classifier"))
_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_BASH_INPUT = re.compile(r"<bash-input>(.*?)</bash-input>", re.DOTALL)
_DISPATCH = re.compile(r"\bdlv-[0-9a-f]{6,}(?:-[a-z0-9]+)*|\b\d{8}-\d{6}-[\w-]+")
_CACHE: Dict[Tuple[str, int, int], Tuple[Optional[str], Optional[str]]] = {}


@dataclass
class Ctx:
    project_id: str
    session_id: str
    account: str
    origin: str
    slug_dispatch: str


@dataclass
class TranscriptScan:
    events: List[Dict[str, str]] = field(default_factory=list)
    skipped: Dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))


def origin_of(slug: str) -> str:
    """worker (dispatch worktree), t0, operator (the project root slug) or unknown; only this word is kept."""
    if "-vnx-data-worktrees-" in slug or "-.vnx-data-worktrees-" in slug:
        return "worker"
    if slug.endswith("-claude-terminals-T0") or slug.endswith("-T0"):
        return "t0"
    return "unknown" if any(m in slug for m in ("-vnx-data-", "-claude-", "worktrees")) else "operator"


def _config_uuid(path: Any) -> Tuple[Optional[str], Optional[str]]:
    """(uuid, failure class). The class is ``unreadable``, ``unparseable`` or ``wrong_type``; both None is "no uuid in it"."""
    try:
        p = Path(path).expanduser()
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
        if key not in _CACHE:
            _CACHE[key] = _parse_config(p.read_text(encoding="utf-8"))
        return _CACHE[key]
    except (OSError, UnicodeDecodeError):  # a message may carry the path: only the class is kept
        return None, "unreadable"


def _parse_config(text: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        data = json.loads(text)
    except ValueError:  # JSONDecodeError; its message may quote config content, so only the class is kept
        return None, "unparseable"
    if not isinstance(data, dict):
        return None, "wrong_type"
    acct = data.get("oauthAccount")  # absent is a config without a login: no uuid, no failure
    if acct is not None and not isinstance(acct, dict):
        return None, "wrong_type"
    uuid = (acct or {}).get("accountUuid")
    return (uuid, None) if isinstance(uuid, str) and uuid else (None, None if uuid is None else "wrong_type")


def default_account_configs() -> Dict[str, Path]:
    """Resolve ADR-090 folders to account files; invalid mappings raise ValueError."""
    default = (Path.home() / ".claude").resolve()
    return {slot: Path.home() / ".claude.json" if folder == default else folder / ".claude.json"
            for slot, folder in account_dirs().items()}


def resolve_account_checked(owner_uuid: Any, config_paths=None) -> Tuple[str, List[str]]:
    """(account, reasons). A reason is ``config_<class>_<slot>``: never a path, uuid or e-mail; empty when all configs read."""
    reasons: List[str] = []
    account = "unknown"
    valid = isinstance(owner_uuid, str) and bool(owner_uuid)
    configs = default_account_configs() if config_paths is None else config_paths
    pairs = configs.items() if isinstance(configs, Mapping) else zip(ACCOUNT_SLOTS, configs)
    for slot, path in pairs:
        uuid, failure = _config_uuid(path)
        if failure:
            reasons.append(f"config_{failure}_{slot}")
        elif valid and account == "unknown" and uuid == owner_uuid:
            account = slot
    return account, reasons


def resolve_account(owner_uuid: Any, config_paths=None) -> str:
    """The configured slot when the uuid matches, else ``unknown``. The uuid never leaves."""
    return resolve_account_checked(owner_uuid, config_paths)[0]


def _texts(obj: Dict[str, Any], kinds: Tuple[str, ...] = ("text",)) -> Iterator[str]:
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    for b in content if isinstance(content, list) else []:
        if isinstance(b, dict) and b.get("type") in kinds and isinstance(b.get("text"), str):
            yield b["text"]


def _result_text(block: Dict[str, Any]) -> str:
    c = block.get("content")
    if isinstance(c, list):
        return " ".join(b["text"] for b in c if isinstance(b, dict) and isinstance(b.get("text"), str))
    return c if isinstance(c, str) else ""


def _field(key: str, block: Dict[str, Any], obj: Dict[str, Any]) -> Any:
    """The field on the tool_result block, the line or its ``toolUseResult``."""
    for holder in (block, obj, obj.get("toolUseResult")):
        if isinstance(holder, dict) and key in holder:
            return holder[key]
    return None


def classify(decision: Any, kind: Any) -> Optional[Tuple[Optional[str], str]]:
    """(bron, besluit), (None, skip reason) or None when the result is neither a refusal nor an answered prompt."""
    d = decision if isinstance(decision, dict) else {}
    src, code, kind = (v if isinstance(v, str) else "" for v in (d.get("source"), d.get("reasonCode"), kind))
    if src in USER_SOURCES:
        return "B2", USER_SOURCES[src]
    if not kind and d.get("decision") != "reject":
        return None  # an automatic accept
    if src not in KNOWN_SOURCES:
        return None, "unknown_source"
    if src == "hook" or code.startswith("hook"):
        return "B1", "refused_hook"
    if src == "classifier" or kind == "automode-blocked" or code.startswith("classifier"):
        return "B1", "refused_classifier"
    if src == "config" or kind == "permission-rule":
        return "B1", "refused_config"
    return None, "unproven_user_rejection"  # user-rejected without a user source is no proven click of Vincent


def find_dispatch_id(text: str) -> str:
    m = _DISPATCH.search(text or "")
    return m.group() if m else ""


def _emit(skipped: Counter, ctx: Ctx, ts: Any, bron: str, besluit: str, command: str, **kw: Any) -> Optional[Dict[str, str]]:
    try:
        return pe.build_event(
            project_id=ctx.project_id, session_id=ctx.session_id, ts=ts, account=ctx.account, origin=ctx.origin,
            bron=bron, besluit=besluit, command=command, dispatch_id=find_dispatch_id(command) or ctx.slug_dispatch, **kw)
    except ValueError:
        skipped["bad_timestamp"] += 1
        return None


def _decisions(objs: Sequence[Dict[str, Any]]) -> Iterator[Tuple[Dict[str, Any], Dict[str, Any], str, Optional[str]]]:
    """(line, tool_result block, stored form, raw form for the hard stop, ADR-082) of the matching tool_use. A Bash use
    without a non-empty str command stores "Bash" with raw None (evidence_unavailable); a non-Bash tool stores its name, raw is the name with "_" as a space."""
    uses: Dict[str, Tuple[str, Optional[str]]] = {}
    for obj in objs:
        msg = obj.get("message") if obj.get("type") == "assistant" else None
        for b in (msg.get("content") if isinstance(msg, dict) and isinstance(msg.get("content"), list) else []):
            if isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(b.get("id"), str):
                cmd = (b.get("input") or {}).get("command") if isinstance(b.get("input"), dict) else None
                name = str(b.get("name") or "")
                uses[b["id"]] = (cmd, cmd) if name == "Bash" and isinstance(cmd, str) and cmd else (name, None if name == "Bash" else name.replace("_", " "))
    for obj in objs:
        msg = obj.get("message") if obj.get("type") == "user" else None
        for b in (msg.get("content") if isinstance(msg, dict) and isinstance(msg.get("content"), list) else []):
            if isinstance(b, dict) and b.get("type") == "tool_result":
                yield (obj, b, *uses.get(b.get("tool_use_id"), ("", None)))


def _read_decisions(objs: Sequence[Dict[str, Any]], ctx: Ctx, skipped: Counter, want: str) -> Iterator[Dict[str, str]]:
    for obj, block, command, raw in _decisions(objs):
        decision, kind = _field("permissionDecision", block, obj), _field("toolDenialKind", block, obj)
        if (decision is not None and not isinstance(decision, dict)) or (kind is not None and not isinstance(kind, str)):
            if want == "B1":  # a malformed line is counted once, under B1
                skipped["malformed_permission_metadata"] += 1
            continue
        verdict = classify(decision, kind)
        if verdict is None or (verdict[0] is not None and verdict[0] != want):
            continue
        if verdict[0] is None:
            if want == "B1":  # the unmapped reasons are counted once, under B1
                skipped[verdict[1]] += 1
            continue
        if not command:
            skipped["no_matching_tool_use"] += 1
            continue
        code = (decision or {}).get("reasonCode")  # decision is a dict or None here (checked above); None has no code
        refused = verdict[1].startswith("refused")
        ev = _emit(skipped, ctx, obj.get("timestamp"), want, verdict[1], command, reason_code=code if isinstance(code, str) else None,
                   raw=raw, reason_text=_result_text(block) if refused else None)  # an approval's text is tool output: never kept
        if ev:
            yield ev


def read_b1(objs: Sequence[Dict[str, Any]], ctx: Ctx, skipped: Counter) -> Iterator[Dict[str, str]]:
    """Refused tool calls: config, classifier or hook (never a user click)."""
    return _read_decisions(objs, ctx, skipped, "B1")


def read_b2(objs: Sequence[Dict[str, Any]], ctx: Ctx, skipped: Counter) -> Iterator[Dict[str, str]]:
    """Answered permission prompts: ``user_temporary``, ``user_permanent``, ``user_reject``."""
    return _read_decisions(objs, ctx, skipped, "B2")


def read_b4a(objs: Sequence[Dict[str, Any]], ctx: Ctx, skipped: Counter) -> Iterator[Dict[str, str]]:
    """One ``asked`` event per fenced block whose surrounding text asks the operator to run it."""
    for obj in objs:
        if obj.get("type") != "assistant":
            continue
        for text in _texts(obj):
            if not any(p in _FENCE.sub(" ", text).lower() for p in ASK_PHRASES):
                continue
            for block in _FENCE.findall(text):
                first = next((ln.strip() for ln in block.splitlines() if ln.strip() and not ln.strip().startswith("#")), "")
                first = re.sub(r"^[!$]\s+", "", first)
                if not first:
                    skipped["no_command_line"] += 1
                    continue
                ev = _emit(skipped, ctx, obj.get("timestamp"), "B4a", "asked", first, raw=block)
                if ev:
                    yield ev


def read_b3(objs: Sequence[Dict[str, Any]], ctx: Ctx, skipped: Counter) -> Iterator[Dict[str, str]]:
    """One ``executed`` event per ``<bash-input>`` user line: a command the operator typed with `!`."""
    for obj in objs:
        if obj.get("type") != "user":
            continue
        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        texts = [content] if isinstance(content, str) else list(_texts(obj))
        for text in texts:
            m = _BASH_INPUT.match(text.strip())
            if not m:
                continue
            if not m.group(1).strip():
                skipped["no_command_line"] += 1
                continue
            ev = _emit(skipped, ctx, obj.get("timestamp"), "B3", "executed", m.group(1).strip())
            if ev:
                yield ev


def scan_transcript(lines: Iterable[str], *, project_id: str, slug: str, stem: str,
                    account_configs=None) -> TranscriptScan:
    """All B1, B2, B3 and B4a events of one transcript; ``skipped`` maps a source to its skip reasons."""
    out, objs = TranscriptScan(), []
    for raw in lines:
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            objs.append(obj)
        elif raw.strip():
            for bron in ("B1", "B2", "B3", "B4a"):
                out.skipped[bron]["malformed_json"] += 1
    owner = next((o for o in objs if o.get("type") == "session-owner"), {})
    sid = owner.get("sessionId")
    account, reasons = resolve_account_checked(owner.get("accountUuid"), account_configs)
    for reason in reasons:
        out.skipped["B1"][reason] += 1  # under B1 like the unmapped reasons: a broken config never reads as 0
    ctx = Ctx(project_id, sid if isinstance(sid, str) and sid else stem, account, origin_of(slug), find_dispatch_id(slug))
    for bron, reader in (("B1", read_b1), ("B2", read_b2), ("B3", read_b3), ("B4a", read_b4a)):
        out.events.extend(reader(objs, ctx, out.skipped[bron]))
    return out
