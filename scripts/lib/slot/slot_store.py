"""Host-wide capacity store and operator policy (ADR-062 besluit 3, 7, 14; RS-2).

Every refusal is a SlotError with exit code 78. A wrong right or an invalid policy is
never mode ``off``. Paths are parameters: no environment variable chooses a path or budget.

A corrupt or missing state is state_corrupt, never an empty state. The module writes
only the state file and the control lock inside the store: the policy is read-only here.
State changes go through transaction() (control lock, atomic write); initialize_state()
is the only way an empty state comes into being.
"""
from __future__ import annotations

import contextlib
import fcntl
import grp
import json
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Tuple

from .host_paths import DEFAULT_POLICY_PATH, DEFAULT_STORE_DIR

STORE_GROUP = "vnxslot"
EXIT_INVALID = 78
ACL_LINE = re.compile(r"^\s*\d+:\s+(.+?)\s+(allow|deny)\b")
MODES = ("off", "observe", "enforce")
STATE_MODES = ("off", "observe", "enforce_pending", "enforce")


class SlotError(Exception):
    """A refusal with a reason code from the ADR-062 table; exit code 78."""

    exit_code = EXIT_INVALID

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def stat_path(path: Path) -> Dict[str, Any]:
    """Default rights seam: uid, group name, mode bits and the ACL lines of ``ls -led``."""
    st = os.stat(path)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    out = subprocess.run(["/bin/ls", "-led", str(path)], capture_output=True, text=True, check=True).stdout
    return {"uid": st.st_uid, "group": group, "mode": st.st_mode & 0o7777, "acl": "\n".join(out.splitlines()[1:])}


def _int(v: Any, minimum: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= minimum


def _num(v: Any) -> bool:
    """A usable non-negative threshold: a finite float, or an int that converts to one."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    try:
        return math.isfinite(float(v)) and v >= 0
    except OverflowError:
        return False


def _no_constant(name: str) -> Any:
    raise ValueError(f"non-finite JSON constant {name}")


def _load(path: Path, code: str) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"), parse_constant=_no_constant)
    except (OSError, ValueError) as exc:
        raise SlotError(code, f"{path.name} unreadable or not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SlotError(code, f"{path.name} is not a JSON object")
    return data


def _validate_policy(p: Dict[str, Any]) -> None:
    def bad(msg: str) -> SlotError:
        return SlotError("policy_invalid", msg)

    if not (_int(p.get("total"), 1) and _int(p.get("reserved"), 0) and _int(p.get("runner_reserve"), 0)):
        raise bad("total/reserved/runner_reserve")
    if p["reserved"] + p["runner_reserve"] >= p["total"]:
        raise bad("reserved + runner_reserve must stay below total")
    profiles = p.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise bad("profiles")
    for name, prof in profiles.items():
        ok = isinstance(prof, dict) and _int(prof.get("slots"), 1) and _int(prof.get("max_slots"), 1)
        if not (ok and prof["slots"] <= prof["max_slots"] and _int(prof.get("max_wait_s"), 1)
                and isinstance(prof.get("heavy"), bool) and isinstance(prof.get("children"), list)):
            raise bad(f"profile {name!r}")
        if not all(isinstance(c, str) for c in prof["children"]):
            raise bad(f"profile {name!r} has a non-string child")
        unknown = [c for c in prof["children"] if c not in profiles]
        if unknown:
            raise bad(f"profile {name!r} references unknown profiles {unknown}")
    mem = p.get("memory")
    if not isinstance(mem, dict):
        raise bad("memory")
    for key in ("swap_growth_mb", "swap_level_mb", "compressor_gb"):
        if not _num(mem.get(key)):
            raise bad(f"memory.{key} is not a finite non-negative number")
    if not (_int(p.get("max_bypass"), 0) and _int(p.get("max_transition"), 1) and _int(p.get("max_queue"), 1)):
        raise bad("max_bypass/max_transition/max_queue")
    if p.get("mode") not in MODES or not isinstance(p.get("enforce_activation"), dict):
        raise bad("mode/enforce_activation")


def _validate_state(s: Dict[str, Any]) -> None:
    shadow, rec = s.get("shadow"), s.get("memory_recovery")
    ok = (isinstance(s.get("holders"), dict) and isinstance(s.get("queue"), list)
          and isinstance(s.get("samples"), list) and s.get("mode") in STATE_MODES
          and _int(s.get("fencing_counter"), 0) and "transition" in s
          and (s["transition"] is None or isinstance(s["transition"], dict))
          and isinstance(shadow, dict) and isinstance(shadow.get("holders"), dict)
          and isinstance(shadow.get("queue"), list)
          and isinstance(rec, dict) and isinstance(rec.get("blocked"), bool)
          and isinstance(rec.get("clean_buckets"), list))
    if not ok:
        raise SlotError("state_corrupt", "state.json misses or mistypes a required field")


class SlotStore:
    def __init__(self, store_dir: Path = DEFAULT_STORE_DIR, policy_path: Path = DEFAULT_POLICY_PATH,
                 stat_fn: Callable[[Path], Dict[str, Any]] = stat_path) -> None:
        self.store_dir, self.policy_path, self._stat = Path(store_dir), Path(policy_path), stat_fn

    def _stats(self, code: str, *paths: Path) -> List[Dict[str, Any]]:
        try:
            return [self._stat(p) for p in paths]
        except (OSError, subprocess.SubprocessError, KeyError) as exc:
            raise SlotError(code, f"rights unreadable: {exc}") from exc

    def check_store(self) -> List[str]:
        try:
            (st,) = self._stats("store_invalid", self.store_dir)
        except SlotError as exc:
            return [str(exc)]
        acl, reasons = str(st.get("acl", "")), []
        if st.get("group") != STORE_GROUP:
            reasons.append(f"store_invalid: group is {st.get('group')!r}, not {STORE_GROUP}")
        if not isinstance(st.get("mode"), int) or st["mode"] & 0o007:
            reasons.append("store_invalid: rights for others")
        entries = [(ACL_LINE.match(ln), ln) for ln in acl.splitlines() if ln.strip()]
        if any(m is None for m, _ in entries):
            reasons.append("store_invalid: unparsable ACL line")
        grants = [(m.group(1), ln) for m, ln in entries if m and m.group(2) == "allow"]
        reasons += [f"store_invalid: ACL grant for {who}" for who in dict.fromkeys(w for w, _ in grants)
                    if who != f"group:{STORE_GROUP}"]
        if not any(who == f"group:{STORE_GROUP}" and "file_inherit" in ln and "directory_inherit" in ln
                   for who, ln in grants):
            reasons.append("store_invalid: inheritable group ACL entry missing")
        return reasons

    def check_policy(self) -> List[str]:
        try:
            store, pdir, pfile = self._stats("policy_invalid", self.store_dir, self.policy_path.parent, self.policy_path)
        except SlotError as exc:
            return [str(exc)]
        reasons = []
        if pdir.get("uid") != store.get("uid") or pfile.get("uid") != store.get("uid"):
            reasons.append("policy_invalid: owner uid differs from the store's")
        if pdir.get("mode") != 0o755:
            reasons.append("policy_invalid: directory is not 0755")
        if pfile.get("mode") != 0o644:
            reasons.append("policy_invalid: file is not 0644")
        if any(isinstance(x.get("mode"), int) and x["mode"] & 0o020 for x in (pdir, pfile)):
            reasons.append("policy_invalid: group write right")
        if str(pdir.get("acl", "")).strip() or str(pfile.get("acl", "")).strip():
            reasons.append("policy_invalid: ACL entry present")
        return reasons

    def read_policy(self) -> Dict[str, Any]:
        policy = _load(self.policy_path, "policy_invalid")
        _validate_policy(policy)
        return policy

    def read_state(self) -> Dict[str, Any]:
        state = _load(self.store_dir / "state.json", "state_corrupt")
        _validate_state(state)
        return state

    def _guard(self) -> None:
        reasons = self.check_store() + self.check_policy()
        if reasons:
            raise SlotError(reasons[0].split(":")[0], "; ".join(reasons))

    def _write_state(self, state: Dict[str, Any]) -> None:
        try:
            payload = json.dumps(state, sort_keys=True, allow_nan=False)
        except ValueError as exc:
            raise SlotError("state_corrupt", f"state holds a non-finite number: {exc}") from exc
        tmp = self.store_dir / f".state.json.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o660)
        try:
            os.fchmod(fd, 0o660)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fd = -1
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.store_dir / "state.json")  # vnx-atomic-write: temp file + replace under the lock
        except BaseException:
            if fd >= 0:
                os.close(fd)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        self._guard()
        fd = os.open(self.store_dir / "ledger.lock", os.O_RDWR | os.O_CREAT, 0o660)
        try:
            os.fchmod(fd, 0o660)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing the descriptor releases the flock

    @contextlib.contextmanager
    def transaction(self) -> Iterator[Dict[str, Any]]:
        """Read state under the control lock; write it back atomically on a clean exit only."""
        with self._lock():
            state = self.read_state()
            yield state
            _validate_state(state)
            self._write_state(state)

    def initialize_state(self) -> None:
        """The only way an empty state comes into being; refuses over an existing state.json."""
        policy_mode = self.read_policy()["mode"]
        with self._lock():
            if (self.store_dir / "state.json").exists():
                raise SlotError("state_corrupt", "state.json already exists; not re-initialising")
            self._write_state({
                "holders": {}, "queue": [], "samples": [], "mode": "off" if policy_mode == "off" else "observe",
                "fencing_counter": 0, "transition": None, "shadow": {"holders": {}, "queue": []},
                "memory_recovery": {"blocked": False, "clean_buckets": []}})

    def doctor(self) -> Tuple[int, List[str]]:
        reasons = self.check_store() + self.check_policy()
        for reader in (self.read_policy, self.read_state):
            try:
                reader()
            except SlotError as exc:
                reasons.append(str(exc))
        return (EXIT_INVALID, reasons) if reasons else (0, [])
