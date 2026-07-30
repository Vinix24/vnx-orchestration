# Runtime Liveness

**Status**: Active
**Last measured**: 2026-07-30
**Purpose**: The rest of `docs/` records *intent* — what a mechanism is designed to do. This document records *liveness* — whether it is actually running right now, on this machine, as measured. The two are not the same claim. A doc can accurately describe a system that is dormant; this file exists so nobody has to re-derive that gap from scratch.

Every entry below carries the exact command used to measure it and the raw output. Re-run the command before trusting the verdict — this file records a point-in-time measurement, not a guarantee.

Two dormancy classes matter, and they look identical in a doc that only describes intent:

- **Dormant BY DESIGN** — an opt-in flag nobody has turned on yet, or a code path scoped to only fire in a specific context. Expected, not a bug.
- **Dormant BY DEFECT** — something that is supposed to be running continuously, isn't, and nothing gates it off on purpose.

---

## 1. Receipt processor

**What it is / full doc**: `docs/operations/RECEIPT_PIPELINE.md`. Drains `unified_reports/` into `t0_receipts.ndjson`; without it, worker reports never become receipts and the audit trail has no entries for that work.

**Check command**:
```bash
ps aux | grep -i receipt_processor | grep -v grep
cat /Users/vincentvandeth/.vnx-data/vnx-dev/pids/receipt_processor_supervisor.pid 2>/dev/null
ps -p "$(cat /Users/vincentvandeth/.vnx-data/vnx-dev/pids/receipt_processor_supervisor.pid 2>/dev/null)" 2>&1
```

**Measured 2026-07-30**:
```
$ ps aux | grep -i receipt_processor | grep -v grep
(no output)

$ cat .../pids/receipt_processor_supervisor.pid
2880
$ ps -p 2880
  PID TTY           TIME CMD
(no matching row — PID 2880 is not alive)
```
`receipt_processor.log` last write: 2026-06-27 16:13 (repeated `[ERROR] Could not find T0 pane`). `receipt_processor_supervisor.log` last write: 2026-07-24 09:06:27, ending in an explicit shutdown: `[INFO] Shutting down receipt processor (PID: 3036)...`. That supervisor log file is itself 6.7GB — a separate, unbounded-growth symptom on the same subsystem.

**Verdict: no process running.** Last confirmed alive 2026-07-24. **BY DEFECT** — nothing opts this off; there is no `PAUSED` marker (`ls .../state/PAUSED` → does not exist) and no operator flag disabling it. It stopped and nothing restarted it or alerted on the gap.

---

## 2. Heartbeat ACK monitor

**What it is / full doc**: `docs/core/00_VNX_ARCHITECTURE.md` §"Heartbeat ACK Monitor" — processes `task_ack` receipts and timeout management; listed there as one of the daemons the (legacy) supervisor starts and pid-tracks (`heartbeat_ack_monitor.pid`).

**Check command**:
```bash
ps aux | grep -i heartbeat_ack_monitor | grep -v grep
cat /Users/vincentvandeth/.vnx-data/vnx-dev/pids/heartbeat_ack_monitor.pid
ps -p "$(cat /Users/vincentvandeth/.vnx-data/vnx-dev/pids/heartbeat_ack_monitor.pid 2>/dev/null)" 2>&1
```

**Measured 2026-07-30**:
```
$ ps aux | grep -i heartbeat_ack_monitor | grep -v grep
(no output)

$ cat .../pids/heartbeat_ack_monitor.pid
77320
$ ps -p 77320
  PID TTY           TIME CMD
(no matching row)
```
`heartbeat_ack_monitor.log` last write: 2026-06-27 16:13. `heartbeat_ack_daemon.log`: 0 bytes since 2026-06-23.

**Verdict: never started this session; not started since 2026-06-27.** **BY DEFECT** for the same reason as the receipt processor — it is documented as a continuously-supervised daemon, no flag turns it off, and it simply isn't running.

---

## 3. Unified supervisor

**What it is / full doc**: `docs/operations/UNIFIED_SUPERVISOR.md`. Opt-in (`VNX_SUPERVISOR_MODE=unified`) auto-respawn + lease-sweep (30s) + runtime-supervise (60s) tick layer, per the project `CLAUDE.md`: *"Default (unset or `legacy`): no behavior change."*

**Check command**:
```bash
echo "VNX_SUPERVISOR_MODE=${VNX_SUPERVISOR_MODE:-<unset>}"
ps aux | grep -iE "supervisor" | grep -v grep
find /Users/vincentvandeth/.vnx-data/vnx-dev -iname "*lease_sweep*" -o -iname "*runtime_supervise*"
```

**Measured 2026-07-30**:
```
$ echo "VNX_SUPERVISOR_MODE=${VNX_SUPERVISOR_MODE:-<unset>}"
VNX_SUPERVISOR_MODE=<unset>

$ ps aux | grep -iE "supervisor" | grep -v grep
(no output)

$ find ... -iname "*lease_sweep*" -o -iname "*runtime_supervise*"
(no output — no tick evidence found)
```

**Verdict: not running, no tick evidence.** **BY DESIGN** — the flag is explicitly opt-in and unset here; this is the documented default, not a gap. (Note: this also means nothing is currently auto-respawning #1 or #2 above — their dormancy and this flag's off-state are related but are two separate facts. Turning this flag on is exactly the kind of activation this dispatch was told not to perform.)

---

## 4. Worker permission scoping (`VNX_WORKER_SCOPED` / `VNX_ENFORCE_WORKER_PERMISSIONS`)

**What it is / full doc**: `docs/operations/WORKER_PERMISSIONS.md`; summarized in `docs/core/DISPATCH_RULES.md` §5 — both default `0`/unset, meaning detached workers launch with blanket `--dangerously-skip-permissions` rather than a scoped allow-list.

**Check command**:
```bash
echo "VNX_WORKER_SCOPED=${VNX_WORKER_SCOPED:-<unset>}"
echo "VNX_ENFORCE_WORKER_PERMISSIONS=${VNX_ENFORCE_WORKER_PERMISSIONS:-<unset>}"
grep -n 'os.environ.get("VNX_WORKER_SCOPED"\|os.environ.get("VNX_ENFORCE_WORKER_PERMISSIONS"' scripts/lib/worker_permissions.py
```

**Measured 2026-07-30**:
```
$ echo "VNX_WORKER_SCOPED=${VNX_WORKER_SCOPED:-<unset>}"
VNX_WORKER_SCOPED=<unset>
$ echo "VNX_ENFORCE_WORKER_PERMISSIONS=${VNX_ENFORCE_WORKER_PERMISSIONS:-<unset>}"
VNX_ENFORCE_WORKER_PERMISSIONS=<unset>

$ grep -n 'os.environ.get("VNX_WORKER_SCOPED"\|os.environ.get("VNX_ENFORCE_WORKER_PERMISSIONS"' scripts/lib/worker_permissions.py
227:    return os.environ.get("VNX_WORKER_SCOPED", "0").strip().lower() in (...)
246:    return os.environ.get("VNX_ENFORCE_WORKER_PERMISSIONS", "0").strip().lower() in (...)
```
Both env vars unset in this session; both default to `"0"` (falsey) in code, confirmed at four independent call sites (`dispatch_govern.py`, `provider_dispatch.py`, `subprocess_adapter.py`, `tmux_interactive_dispatch.py`, `worker_permissions.py`).

**Verdict: both off.** **BY DESIGN** — `docs/core/DISPATCH_RULES.md` §5 documents this default explicitly and gives the reasoning (an already-isolated-per-worktree worker gains nothing from a scoped allow-list except stalled prompts). This is the one entry in this document that is unambiguously intentional.

---

## 5. `dispatch_guard.sh`

**What it is / full doc**: `docs/core/DISPATCH_RULES.md` §10 (pointer only — it's an operational script, not narrated in prose). Read-only pre-dispatch go/no-go check T0 runs before dispatching.

**Check command**:
```bash
bash skills/t0-orchestrator/scripts/dispatch_guard.sh; echo "exit: $?"
```

**Measured 2026-07-30** (run from this dispatch's worktree):
```
$ bash skills/t0-orchestrator/scripts/dispatch_guard.sh
Missing file: /Users/vincentvandeth/Development/vnx-orchestration/.vnx-data/worktrees/.vnx-data/state/t0_brief.json
exit: 1
```
The script resolves its own root as `$SCRIPT_DIR/../../../..` (four `cd ..` from `skills/t0-orchestrator/scripts/`), which assumes it always runs from the main checkout. Run from inside a nested worktree (as this dispatch did — worktrees live under `.vnx-data/worktrees/<id>/`), that traversal overshoots into the *worktrees* directory itself, producing a path with `.vnx-data` appearing twice and pointing nowhere real.

**Verdict: reads a repo-local path that does not exist in this context, exit 1.** **BY DEFECT** — the relative-path assumption breaks specifically in the isolated-worktree topology every tmux-spawn dispatch runs in (`--isolated-worktree` is the documented default per §5), which is the majority of how dispatches execute today, not an edge case.

---

## 6. SessionStart hook chain

**What it is / full doc**: four hooks registered under `SessionStart` in `.claude/settings.json`. No single doc narrates all four; this is the first place they're inventoried together.

**Check command**:
```bash
cat .claude/settings.json | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['hooks']['SessionStart'], indent=2))"
```

**Measured 2026-07-30** — which of the four leave evidence they ran, checked against *this session* (a tmux-spawn worker dispatch, `VNX_DISPATCH_ID=20260730-docs-liveness` set):

| # | Hook | Fires when | Evidence it left | Verdict |
|---|---|---|---|---|
| 1 | `scripts/hooks/session_reconcile_autoclose.sh` | `VNX_DISPATCH_ID` **unset** (interactive/operator session only) | `.vnx-data/logs/objective_reconcile.log` — **repo-root-relative**, so per-checkout, not central. In the main checkout: `tail -3` shows `[2026-07-30T13:50:23Z] session-reconcile tick: mode=apply streak_met=no`, file mtime 15:50 today. In *this* worktree: no-op by design (`VNX_DISPATCH_ID` is set), file absent. | Runs for interactive sessions; **no-op by design** for worker dispatches (explicit guard in the script, not a bug). |
| 2 | `scripts/build_t0_state.py` (matcher: `terminals/T0`) | cwd matches `terminals/T0` | Would write `.vnx-data/state/t0_state.json` + `.vnx-data/logs/build_t0_state.err`. This session's cwd is a worktree, not `terminals/T0`, so it never fired here — `ls .vnx-data/state/t0_state.json` → not found in this worktree. | Matcher-scoped by design; not evaluated for a real T0 session in this pass. |
| 3 | `scripts/hooks/tmux_signal_session_ready.sh` | `VNX_TMUX_SIGNAL_DIR` **and** `VNX_DISPATCH_ID` both set (tmux-spawn workers) | Confirmed fired for this exact session: `$VNX_TMUX_SIGNAL_DIR/session_ready` contains `20260730-docs-liveness`, `session_id` file present, both timestamped 15:50 today. | **Fired and left verifiable evidence**, self-confirmed in this run. |
| 4 | `hooks/sessionstart.sh` | always (matcher `""`) | Pure read-and-print (builds a context banner from existing state files) — no `>`, `tee`, `mkdir`, or log write anywhere in the script (`grep -n '>>\|tee\|mkdir\|\.log' hooks/sessionstart.sh` → no matches). | Runs every session, but **leaves no persisted evidence** — the only way to confirm it ran is to have seen its banner in that session's own transcript. |

**Verdict: all four are wired up; none are broken.** The variation is by design (context-scoped matchers/guards, not defects) — but #4 is structurally unverifiable after the fact, which is itself worth knowing before trusting "it always runs."

---

## 7. Conversation-analyzer launchagent

**What it is / full doc**: `dashboard/README.md` ("populated nightly by `conversation_analyzer_nightly.sh` (launchd, 02:00)"); analyzer internals fixed today by PR #1248.

**Check command**:
```bash
launchctl print gui/$(id -u)/com.vnx.conversation-analyzer
tail -10 /tmp/vnx-conversation-analyzer.err
```

**Measured 2026-07-30**:
```
$ launchctl print gui/501/com.vnx.conversation-analyzer
state = not running
program = /bin/bash
arguments = { /bin/bash /Users/vincentvandeth/Development/vnx-orchestration/scripts/conversation_analyzer_nightly.sh }
runs = 0
last exit code = (never exited)

$ tail -6 /tmp/vnx-conversation-analyzer.err   # from 02:00 today, BEFORE the plist fix
/bin/bash: /Users/vincentvandeth/Development/SEOcrawler_v2/.vnx/scripts/conversation_analyzer_nightly.sh: No such file or directory
(repeated identically ~6x)
```
The plist itself was rewritten today: `/Users/vincentvandeth/Library/LaunchAgents/com.vnx.conversation-analyzer.plist.bak-20260730-123520` (the pre-fix version, still on disk) points at `SEOcrawler_v2/.vnx/scripts/conversation_analyzer_nightly.sh` — a path removed by the 2026-07-04 single-VNX cutover. The current plist points at `vnx-orchestration/scripts/conversation_analyzer_nightly.sh` (the real location) and adds `VNX_ANALYZER_LLM=ollama-only`, matching #1248's new default.

**Verdict: was BY DEFECT from 2026-06-21 through today 12:35 — every 02:00 run failed at the shell level on a path removed three weeks earlier, never reaching the Python fixed by #1248.** The plist was corrected today (source unclear from this dispatch's evidence — no commit in this repo touches `~/Library/LaunchAgents/`); it has **not yet been re-verified by an actual run** — `runs = 0` since the fix, and the next scheduled trigger is tonight at 02:00. Re-run the check command tomorrow after 02:00 to confirm the fix holds.

---

## 8. Event-stream truncation

**What it is / full doc**: `docs/operations/EVENT_STREAMS.md` — see that doc's "Correction (2026-07-30, measured, not designed)" section for the full account; not duplicated here.

**Check command**:
```bash
ls -la /Users/vincentvandeth/.vnx-data/vnx-dev/events/T1.ndjson /Users/vincentvandeth/.vnx-data/vnx-dev/events/T2.ndjson /Users/vincentvandeth/.vnx-data/vnx-dev/events/T3.ndjson
grep -o '"dispatch_id":"[^"]*"' /Users/vincentvandeth/.vnx-data/vnx-dev/events/archive/T1/20260730-131817-oversized-rescue.ndjson | sort -u
```

**Measured 2026-07-30**:
```
$ ls -la .../events/T1.ndjson .../events/T2.ndjson .../events/T3.ndjson
T1.ndjson  0 bytes  (Jul 30 13:18)
T2.ndjson  0 bytes  (Jul 30 12:59)
T3.ndjson  0 bytes  (Jul 10 22:29)

$ grep -o '"dispatch_id":"[^"]*"' .../archive/T1/20260730-131817-oversized-rescue.ndjson | sort -u
"dispatch_id":"20260730-sfp2-analyzer-chain"
"dispatch_id":"20260730-sfp4-diagnostics"
```
Live files are currently 0 bytes — but only because two ~18-20MB files were manually archived out under an `oversized-rescue` name earlier today (`20260730-125955-oversized-rescue.ndjson` 19.97MB, `20260730-131817-oversized-rescue.ndjson` 17.36MB). The second one alone contains events from two unrelated dispatches, proving the live file was never truncated between them.

**Verdict: archiving works; truncation does not, reliably.** **BY DEFECT** — `event_store.clear()`'s truncation call is wrapped in a bare `try/except Exception` logged at `DEBUG` only (`scripts/lib/subprocess_dispatch_internals/delivery.py:411-416`), so a failing truncation is invisible by default. Current 0-byte state reflects a manual rescue earlier today, not a fixed root cause — re-run the check command after the next few provider-lane dispatches on T1/T2 to see whether it grows again.

---

## Summary table

| Mechanism | Running now? | Class |
|---|---|---|
| Receipt processor | No (dead since 2026-07-24) | BY DEFECT |
| Heartbeat ACK monitor | No (dead since ≤2026-06-27) | BY DEFECT |
| Unified supervisor | No (flag unset) | BY DESIGN |
| Worker permission scoping | Off (both vars unset) | BY DESIGN |
| `dispatch_guard.sh` | Errors in worktree context | BY DEFECT |
| SessionStart hooks 1–3 | Fire correctly per their own scope | BY DESIGN (scoped) |
| SessionStart hook 4 | Fires, leaves no evidence | Not a defect, but unverifiable after the fact |
| Conversation-analyzer launchagent | Fixed today, unconfirmed by a real run | Was BY DEFECT; pending re-verification |
| Event-stream truncation | Archiving yes, truncation no | BY DEFECT |

Three of nine rows are explicitly-gated, working-as-designed dormancy. The other six are either active defects or defects patched today but not yet proven by an actual unattended run — re-run each command above rather than trusting this table past its measurement date.
