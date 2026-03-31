# Conversation Resume Contract — Source-Of-Truth Boundaries And Operator Actions

**Feature**: Conversation Resume And Latest-First Timeline
**PR**: PR-0
**Status**: Canonical
**Purpose**: Defines what owns conversation data, how sessions link to worktrees, how latest-first ordering works, and what the operator can and cannot do when resuming a session. All downstream PRs (PR-1 through PR-4) build against this contract.

---

## 1. Source-Of-Truth Hierarchy

Conversation resume touches three independent data stores. Each owns a distinct slice of truth. The contract forbids any layer from guessing at or duplicating data owned by another.

### 1.1 Claude Code Conversation Index (Upstream Owner)

**Location**: `~/.claude/conversation-index.db` (SQLite)
**Owner**: Claude Code (Anthropic)
**Mutability by VNX**: Read-only. VNX must never write to this database.

This database is the single source of truth for:

| Field | Column | Notes |
|---|---|---|
| Session identity | `session_id` (UUID) | Primary key. Stable across resume. |
| Project scope | `project_path` | Relative path from `~`. Maps to the `.claude/projects/<encoded-path>/` directory. |
| Working directory | `cwd` | Absolute path at session creation time. |
| Last activity | `last_message` | ISO-8601 timestamp of last message. **The canonical sort key for latest-first.** |
| Session title | `title` | First user message or generated summary. |
| Message count | `message_count`, `user_message_count` | Volume indicators. |
| Token usage | `total_tokens` | Cumulative token count. |
| Conversation log | `file_path` | Path to the session JSONL file. |

**Invariant SOT-1**: Latest-first ordering is computed from `conversations.last_message DESC`. VNX must not maintain a shadow timestamp.

**Invariant SOT-2**: Session identity is `session_id`. VNX must not invent alternative session identifiers. All VNX-side linkage references `session_id` as the foreign key.

**Invariant SOT-3**: The `cwd` field records where the session was started. For VNX terminal sessions, this is `.claude/terminals/T{0,1,2,3}` under the project root. This path is the primary link between a conversation and its worktree context.

### 1.2 VNX Runtime State (VNX Owner)

**Location**: `.vnx-data/state/` (runtime, never committed)
**Owner**: VNX orchestration layer
**Mutability by Claude Code**: None. Claude Code does not know this exists.

VNX runtime state is the source of truth for:

| Data | File | Notes |
|---|---|---|
| Terminal-to-pane mapping | `panes.json` | Which tmux pane runs which terminal. |
| Session profile | `session_profile.json` | Full T0/T1/T2/T3 layout declaration. |
| Context pressure | `context_window_T{n}.json` | Per-terminal context usage tracking. |
| Rotation latch | `rotation_latch_T{n}` | Prevents double-rotation. |
| Dispatch state | `dispatches/` | Active dispatch lifecycle. |

**Invariant SOT-4**: VNX runtime state describes the current operational environment (which pane is which terminal, what dispatch is active). It does not describe conversation history. Conversation history lives in the Claude Code index.

**Invariant SOT-5**: The session profile declares terminal identity (T0/T1/T2/T3), not conversation identity. A terminal may host many conversations over its lifetime.

### 1.3 Context Rotation Handover (Bridging Layer)

**Location**: `.vnx-data/state/ROTATION-HANDOVER-T{n}.md` (transient)
**Owner**: VNX context rotation hooks
**Lifecycle**: Written at rotation trigger, consumed at session recovery, deleted after injection.

Rotation handovers bridge the gap between a dying session and its successor:

| Field | Source | Notes |
|---|---|---|
| Dispatch-ID | Extracted from handover document | Links the rotated session to its dispatch. |
| Completed work | Written by the agent before rotation | Task progress snapshot. |
| Remaining tasks | Written by the agent before rotation | What the successor must continue. |
| Context percentage | Hook measurement | The pressure level that triggered rotation. |

**Invariant SOT-6**: A rotation handover creates a new `session_id` in the Claude Code index. The old session and the new session are independent conversations from Claude Code's perspective. VNX must track the continuity chain explicitly — it is not inferable from the Claude Code index alone.

**Invariant SOT-7**: Rotation continuity metadata is a VNX concern. The read model (PR-1) must expose which sessions form a rotation chain for a given dispatch, using Dispatch-ID extracted from handover documents or receipt events (`context_rotation_continuation`).

---

## 2. Worktree And Session Linkage

### 2.1 Linking Rule

A conversation belongs to a worktree when its `cwd` resolves to a path inside that worktree's root directory.

```
worktree_root = /Users/operator/Development/project-wt/
session.cwd   = /Users/operator/Development/project-wt/.claude/terminals/T1
→ session belongs to worktree "project-wt"
```

**Invariant LINK-1**: Worktree membership is derived from path containment (`cwd starts with worktree_root`). VNX must not maintain a separate worktree-to-session mapping table. The derivation is deterministic and always recomputable.

**Invariant LINK-2**: Terminal identity is derived from the last path segment of `cwd` (e.g., `T1` from `.claude/terminals/T1`). When `cwd` does not follow the `.claude/terminals/T{n}` pattern, terminal identity is unknown and must be displayed as such.

### 2.2 Multi-Worktree Resolution

When multiple worktrees exist for the same repository, the read model must group sessions by worktree root, not by repository. Two worktrees of the same repo are independent scopes.

**Invariant LINK-3**: The main repo root and a worktree of that repo are separate grouping contexts. A session in `/project-wt/` is never grouped with sessions in `/project/` even though they share git history.

### 2.3 Stale Worktree Handling

Worktrees may be deleted after sessions were created in them. The read model must:
1. Still display sessions from deleted worktrees (the conversation data is intact in the Claude Code index).
2. Mark the worktree as absent when the path no longer exists on disk.
3. Block resume-to-worktree actions for absent worktrees (resume-to-conversation-only remains available via `claude --resume <session_id>`).

---

## 3. Latest-First As Canonical Default

### 3.1 Sort Contract

**Invariant ORDER-1**: The default operator view sorts sessions by `last_message DESC` (latest-first). This is a presentation default, not a data transformation. The underlying data is unordered.

**Invariant ORDER-2**: The operator can toggle to `last_message ASC` (oldest-first). Toggling sort order must not change the selected session, filter state, or any other view state.

**Invariant ORDER-3**: Within a rotation chain (multiple sessions for one dispatch), sessions are ordered by `last_message DESC` within the chain. The chain itself is positioned by the `last_message` of its most recent member.

### 3.2 Timestamp Fidelity

The `last_message` timestamp comes from Claude Code and reflects message arrival time. VNX must not adjust, round, or recompute this value. If a session has no messages (edge case: interrupted before first response), `last_message` may be NULL — sort these to the bottom.

---

## 4. Operator Actions

### 4.1 Defined Actions

| Action | Mechanism | Preconditions |
|---|---|---|
| **List recent sessions** | Query `conversations` table, filter by `project_path` or `cwd` prefix, order by `last_message DESC` | None |
| **Filter by worktree** | Add `WHERE cwd LIKE '<worktree_root>%'` | Worktree root must be resolvable |
| **Filter by terminal** | Add `WHERE cwd LIKE '%/terminals/T{n}'` | Terminal ID must be valid (T0–T3) |
| **Flip sort order** | Toggle between `DESC` and `ASC` on `last_message` | None |
| **View rotation chain** | Group sessions sharing a Dispatch-ID from rotation events | Rotation metadata must be indexed |
| **Resume conversation** | Execute `claude --resume <session_id>` in the correct worktree cwd | Worktree path must exist on disk |
| **Resume (read-only)** | Execute `claude --resume <session_id>` from any directory | Always available — reviews history only, no worktree guarantee |

### 4.2 Resume Semantics

**Invariant RESUME-1**: "Resume" means reopening a Claude Code conversation with its full message history using `claude --resume <session_id>`. This is a Claude Code native capability. VNX adds worktree-context validation on top.

**Invariant RESUME-2**: Resume is not tmux attach. `tmux attach` reconnects to a terminal multiplexer session. `claude --resume` reopens a conversation. These are independent operations. The operator may need both (attach to the right pane, then resume the right conversation), but VNX conversation resume only handles the conversation layer.

**Invariant RESUME-3**: Resume with worktree context requires the operator's shell to be in the correct working directory before `claude --resume` executes. The resume action must `cd` to the worktree-appropriate terminal directory first, or refuse with an actionable error if the path is absent.

**Invariant RESUME-4**: Cross-worktree resume must be blocked by default. If `session.cwd` belongs to worktree A but the operator is in worktree B, the resume action must warn and require explicit confirmation. Accidental cross-worktree resume corrupts the mental model of which code the agent is working on.

### 4.3 Fork-On-Resume

Claude Code supports `--fork-session` to create a new session ID when resuming. VNX should expose this as an explicit operator choice ("resume and continue" vs. "resume as new branch"). The default is plain resume (same session ID).

---

## 5. Rotation-Summary Continuity

### 5.1 Chain Model

A rotation chain is a sequence of sessions created by context rotation for a single dispatch:

```
Session A (original, Dispatch-ID: X)
  → rotation at 67% context
    → Session B (continuation, Dispatch-ID: X)
      → rotation at 65% context
        → Session C (continuation, Dispatch-ID: X)
```

Sessions A, B, and C form a chain. The chain is identified by the shared Dispatch-ID.

### 5.2 Chain Discovery

The read model discovers chains through two sources (in priority order):

1. **Receipt events**: `context_rotation_continuation` NDJSON events in the receipt log contain the Dispatch-ID and link predecessor/successor session IDs.
2. **Handover documents**: `ROTATION-HANDOVER-T{n}.md` files contain the Dispatch-ID in their body. These are transient but may be archived.

**Invariant ROTATE-1**: Chain discovery must not depend on session title matching, timestamp proximity, or any heuristic. Only explicit Dispatch-ID linkage from receipts or handover documents is accepted.

### 5.3 Chain Display

When displaying a rotation chain:
- Show the most recent session prominently (this is what the operator wants to resume).
- Indicate chain depth (e.g., "3 rotations" or "session 3 of 3").
- Allow expanding to see all sessions in the chain with their individual timestamps and context-pressure values.
- The chain collapses to a single entry in the default list view, positioned by the most recent session's `last_message`.

---

## 6. Non-Goals (Scope Lock)

The following are explicitly out of scope for this feature. Any PR that drifts into these areas must be rejected at the quality gate.

| Non-Goal | Rationale |
|---|---|
| **Chat UI rewrite** | VNX is a CLI orchestration system. Conversation resume surfaces data for the operator to act on via CLI. It does not render messages, format markdown, or provide a chat interface. |
| **Message search / RAG** | The Claude Code index has FTS5 tables. VNX may use them for filtering but must not build a search product on top. |
| **Cross-machine sync** | Conversation data is local to `~/.claude/`. Multi-machine resume requires Anthropic-side sync (not a VNX concern). |
| **Conversation editing** | VNX does not modify, delete, or annotate conversations in the Claude Code index. Read-only access only (SOT-invariant). |
| **Automatic resume on crash** | VNX may detect that a session ended unexpectedly but must not auto-resume without operator confirmation. The operator decides when to resume. |
| **tmux session management** | tmux attach/detach is an orthogonal concern. The `vnx jump` command handles terminal navigation. Conversation resume handles conversation selection. They compose but do not merge. |
| **Real-time conversation monitoring** | Watching live message streams is an observability feature (smart tap), not a resume feature. |

---

## 7. Integration Points

### 7.1 Downstream PR Dependencies

| PR | Consumes From This Contract |
|---|---|
| **PR-1** (Read Model) | SOT hierarchy (§1), linkage rules (§2), chain discovery (§5.2) |
| **PR-2** (Timeline UI) | Sort contract (§3), chain display (§5.3), operator actions (§4.1) |
| **PR-3** (One-Click Resume) | Resume semantics (§4.2–4.4), cross-worktree blocking (RESUME-4) |
| **PR-4** (Certification) | All invariants — certification verifies contract compliance |

### 7.2 Existing System Touchpoints

| System | Relationship |
|---|---|
| Context rotation (hooks) | Produces rotation chain data consumed by §5 |
| Dispatch broker | Dispatch-ID is the chain key per §5.1 |
| Session profile | Terminal identity per LINK-2, but not conversation identity per SOT-5 |
| Smart tap | Orthogonal — monitors live sessions, not historical resume |
| `vnx jump` | Handles terminal navigation, composes with but does not replace resume |

---

## 8. Contract Versioning

This contract is versioned by git. Breaking changes to invariants require:
1. An explicit migration plan in the PR description.
2. Review by Track C (architecture/security).
3. Updates to all downstream PRs that depend on the changed invariant.

Additive changes (new fields, new actions) do not require migration if they do not violate existing invariants.
