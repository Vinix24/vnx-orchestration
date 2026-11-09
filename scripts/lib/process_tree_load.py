"""Pure parsers and tree summaries for process-tree load sampling (RS-1).

Text in, data out: this module never runs a command, touches the filesystem or
parses arguments, so its tests need no live process. ``parse_ps``,
``build_tree``, ``classify_command``, ``sample_tree``, ``parse_swapusage``,
``parse_vm_stat``, ``_percentile`` and ``summarize`` are moved verbatim here
from round 2; ``scripts/measure_process_tree_load.py`` (split 2, RS-1b) is
their caller and owns the sampler, the CLI and the exit codes.

An unreadable value is ``None`` (null in the NDJSON sample), never ``0``.
``sample_tree`` returns counts, CPU sums (100 = one core) and RSS bytes over
the root plus every descendant; ``other_cpu`` includes the root's own ``%cpu``
(a root is never an xdist worker), so ``xdist_cpu + other_cpu`` is the CPU load
of the whole tree, like ``rss``.
"""

from __future__ import annotations

import re
import statistics

SAMPLE_FIELDS = ("n_xdist", "n_other", "xdist_cpu", "other_cpu", "rss",
                 "swap_used", "compressor")
EXECNET_MARKER = "exec(eval(sys.stdin.readline()))"
_GW_RE = re.compile(r"\bgw\d+\b")
_SWAP_USED_RE = re.compile(r"used\s*=\s*([0-9.]+)([KMG])", re.IGNORECASE)
_PAGE_SIZE_RE = re.compile(r"page size of (\d+) bytes")
_COMPRESSOR_RE = re.compile(r"^Pages occupied by compressor:\s+(\d+)\.", re.MULTILINE)
_SWAP_MULTIPLIER = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}


def parse_ps(text: str) -> list[dict]:
    """Parse `ps -A -o pid=,ppid=,pgid=,pcpu=,rss=,command=` into records."""
    procs = []
    for line in text.splitlines():
        parts = line.split(None, 5)
        if len(parts) != 6:
            continue
        try:
            procs.append({"pid": int(parts[0]), "ppid": int(parts[1]),
                          "pgid": int(parts[2]), "cpu": float(parts[3]),
                          "rss_kb": int(parts[4]), "command": parts[5]})
        except ValueError:
            continue
    return procs


def build_tree(procs: list[dict], root_pid: int) -> list[dict]:
    """Every descendant of root_pid, walking the ppid chain."""
    children: dict[int, list[dict]] = {}
    for proc in procs:
        children.setdefault(proc["ppid"], []).append(proc)
    descendants, seen, stack = [], {root_pid}, list(children.get(root_pid, []))
    while stack:
        proc = stack.pop()
        if proc["pid"] in seen:
            continue
        seen.add(proc["pid"])
        descendants.append(proc)
        stack.extend(children.get(proc["pid"], []))
    return descendants


def classify_command(command: str) -> str:
    """`xdist` for an execnet/pytest-xdist worker, `other` for everything else."""
    if EXECNET_MARKER in command or "--dist" in command or _GW_RE.search(command):
        return "xdist"
    return "other"


def sample_tree(procs: list[dict], root_pid: int) -> dict:
    """Counts, CPU sums (100 = one core) and RSS bytes over root plus descendants.

    `other_cpu` includes the root's own `%cpu` (a root is never an xdist worker),
    so `xdist_cpu + other_cpu` is the CPU load of the whole tree, like `rss`.
    """
    descendants = build_tree(procs, root_pid)
    xdist = [p for p in descendants if classify_command(p["command"]) == "xdist"]
    other = [p for p in descendants if classify_command(p["command"]) == "other"]
    root = next((p for p in procs if p["pid"] == root_pid), None)
    other_cpu = sum(p["cpu"] for p in other) + (root["cpu"] if root else 0)
    return {
        "n_xdist": len(xdist), "n_other": len(other),
        "xdist_cpu": round(sum(p["cpu"] for p in xdist), 2),
        "other_cpu": round(other_cpu, 2),
        "rss": (sum(p["rss_kb"] for p in descendants)
                + (root["rss_kb"] if root else 0)) * 1024,
    }


def parse_swapusage(text: str) -> int | None:
    """Used swap in bytes from `sysctl vm.swapusage`, or None when unreadable."""
    if "vm.swapusage" not in text:
        return None
    match = _SWAP_USED_RE.search(text)
    if not match:
        return None
    try:
        return int(float(match.group(1)) * _SWAP_MULTIPLIER[match.group(2).upper()])
    except ValueError:  # a number token the regex allowed but float rejects
        return None


def parse_vm_stat(text: str) -> int | None:
    """Compressor bytes from `vm_stat` (pages times the header page size), or None."""
    page_size = _PAGE_SIZE_RE.search(text)
    pages = _COMPRESSOR_RE.search(text)
    if not page_size or not pages:
        return None
    return int(pages.group(1)) * int(page_size.group(1))


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = int(position), min(int(position) + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize(samples: list[dict], duration: float, failed: int = 0) -> dict:
    """Per-field count/mean/p50/p95/max, the failed-snapshot count and the slot number."""
    fields = {}
    for name in SAMPLE_FIELDS:
        values = [s[name] for s in samples if s.get(name) is not None]
        fields[name] = {
            "n": len(values),
            "mean": round(statistics.fmean(values), 3) if values else None,
            "p50": round(_percentile(values, 0.50), 3) if values else None,
            "p95": round(_percentile(values, 0.95), 3) if values else None,
            "max": round(max(values), 3) if values else None,
        }
    combined = [s["xdist_cpu"] + s["other_cpu"] for s in samples
                if s.get("xdist_cpu") is not None and s.get("other_cpu") is not None]
    p95 = _percentile(combined, 0.95)
    return {
        "note": ("one run of one workload: this is a measurement, not a budget; "
                 "decide the slot number after at least three runs"),
        "samples": len(samples), "failed_snapshots": failed,
        "duration_s": round(duration, 2), "fields": fields,
        "cores_p95": round(p95 / 100, 2) if p95 is not None else None,
    }
