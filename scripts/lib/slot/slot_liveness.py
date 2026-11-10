"""Alive, gone or unknown for a slot lease (RS-4, ADR-062 besluiten 2, 7 and 9).

A lease frees capacity only when its work is positively gone. Everything here
is pure over one process snapshot; ``snapshot`` is the only impure seam and
takes an injected runner. No table, a missing identity field or an
unresolvable part gives ``UNKNOWN``, never ``GONE``.

Interface for RS-3 (admission) and RS-6 (supervisor):
``snapshot(run)`` once per check, then ``lease_liveness(lease, table, holders)``
-> ``(verdict, descendants)`` (store ``descendants`` on an acquire lease),
``supervisor_gone_tree_alive(lease, table, holders)`` for ``lease_held`` /
``owner_gone_tree_alive``, and ``validate_acquire_owner(owner_pid)``.
"""

from __future__ import annotations

import subprocess

ALIVE = "alive"
GONE = "gone"
UNKNOWN = "unknown"
PS_ARGV = ["ps", "-A", "-o", "pid=,ppid=,sess=,lstart="]


class OwnerPidRequired(ValueError):
    """A loose acquire without a usable --owner-pid (exit 78)."""

    code = "owner_pid_required"
    exit_code = 78


def validate_acquire_owner(owner_pid):
    if isinstance(owner_pid, bool) or not isinstance(owner_pid, int) or owner_pid <= 0:
        raise OwnerPidRequired(f"owner_pid_required: got {owner_pid!r}")
    return owner_pid


def _norm(start_time):
    return " ".join(str(start_time).split())


def parse_process_table(text):
    """Parse ``ps -A -o pid=,ppid=,sess=,lstart=`` output; ValueError if unreadable."""
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 8:
            raise ValueError(f"unparseable ps line: {line!r}")
        rows.append({"pid": int(parts[0]), "ppid": int(parts[1]),
                     "session": parts[2], "start_time": _norm(" ".join(parts[3:]))})
    if not rows:
        raise ValueError("empty process table")
    return rows


def _run_ps(argv):
    return subprocess.run(argv, capture_output=True, text=True, check=True, timeout=30).stdout


def snapshot(run=_run_ps):
    """One process table, or None when it could not be read."""
    try:
        return parse_process_table(run(PS_ARGV))
    except Exception:  # vnx-silent-except: an unreadable table is UNKNOWN for every verdict, never GONE
        return None


def _matches(row, pid, start_time):
    return row["pid"] == pid and row["start_time"] == _norm(start_time)


def process_alive(table, pid, start_time):
    if table is None or not isinstance(pid, int) or isinstance(pid, bool) or not start_time:
        return UNKNOWN
    return ALIVE if any(_matches(r, pid, start_time) for r in table) else GONE


def _combine(verdicts):
    if ALIVE in verdicts:
        return ALIVE
    return UNKNOWN if (UNKNOWN in verdicts or not verdicts) else GONE


def _session_verdict(table, session_id):
    # sess=0 is not an identity: it would match every process of the host.
    if table is None or session_id in (None, "", 0, "0"):
        return UNKNOWN
    return ALIVE if any(r["session"] == str(session_id) for r in table) else GONE


def _is_child_of(lease_id, candidate, holders):
    seen = set()
    parent = candidate.get("parent_lease")
    while parent is not None and parent not in seen:
        if parent == lease_id:
            return True
        seen.add(parent)
        parent = (holders.get(parent) or {}).get("parent_lease")
    return False


def _acquire_verdict(lease, table):
    """Owner plus remembered descendants; walks ppid from every live member."""
    known = [dict(d) for d in (lease.get("descendants") or [])]
    if table is None:
        return UNKNOWN, known
    members = [(lease.get("owner_pid"), lease.get("owner_start_time"))]
    members += [(d.get("pid"), d.get("start_time")) for d in known]
    verdicts = [process_alive(table, p, s) for p, s in members]
    frontier = [p for (p, s), v in zip(members, verdicts) if v == ALIVE]
    seen = {(p, _norm(s)) for p, s in members if s}
    while frontier:
        parent = frontier.pop()
        for row in table:
            key = (row["pid"], row["start_time"])
            if row["ppid"] == parent and key not in seen:
                seen.add(key)
                known.append({"pid": row["pid"], "start_time": row["start_time"]})
                verdicts.append(ALIVE)
                frontier.append(row["pid"])
    return _combine(verdicts), known


def _own_verdict(lease, table):
    if lease.get("owner_kind") == "acquire":
        return _acquire_verdict(lease, table)
    return _session_verdict(table, lease.get("session_id")), list(lease.get("descendants") or [])


def lease_liveness(lease, table, holders):
    """Return ``(verdict, descendants)`` for one lease; descendants only change for acquire."""
    kind = lease.get("owner_kind")
    if kind == "acquire":
        return _acquire_verdict(lease, table)
    if kind not in ("run", "runner"):
        return UNKNOWN, list(lease.get("descendants") or [])
    verdicts = [_session_verdict(table, lease.get("session_id"))]
    lease_id = lease.get("lease_id")
    if lease_id is None:
        verdicts.append(UNKNOWN)
    else:
        for child in (holders or {}).values():
            if child is not lease and _is_child_of(lease_id, child, holders):
                verdicts.append(_own_verdict(child, table)[0])
    return _combine(verdicts), []


def supervisor_gone_tree_alive(lease, table, holders):
    """True when the supervisor is positively gone while the lease is positively alive."""
    gone = process_alive(table, lease.get("supervisor_pid"), lease.get("supervisor_start_time")) == GONE
    return gone and lease_liveness(lease, table, holders)[0] == ALIVE
