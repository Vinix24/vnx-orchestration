#!/usr/bin/env python3
"""Roadmap orchestration and auto-next feature loading for VNX."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

from vnx_paths import ensure_env
from governance_receipts import emit_governance_receipt, utc_now_iso
from pr_queue_manager import PRQueueManager
from closure_verifier import verify_closure, _find_gate_result
from gate_status import is_pass as gate_is_pass
from project_scope import current_project_id
from vnx_worktree import worktree_start


ALLOWED_DRIFT_CATEGORIES = {"bugfix", "post_cleanup", "governance_gap", "path/runtime regression"}

APPROVALS_SUBDIR = "roadmap_approvals"


def _root_file(project_root: Path, name: str) -> Path:
    return project_root / name


@dataclass
class RoadmapPaths:
    project_root: Path
    state_dir: Path
    state_file: Path
    generated_dir: Path


class RoadmapManager:
    def __init__(self) -> None:
        paths = ensure_env()
        self.project_root = Path(paths["PROJECT_ROOT"]).resolve()
        self.state_dir = Path(paths["VNX_STATE_DIR"]).resolve()
        self.project_id = current_project_id()
        self.paths = RoadmapPaths(
            project_root=self.project_root,
            state_dir=self.state_dir,
            state_file=self.state_dir / "roadmap_state.json",
            generated_dir=self.state_dir / "roadmap_generated_features",
        )
        self.paths.generated_dir.mkdir(parents=True, exist_ok=True)

    def _load_yaml(self, path: Path) -> Dict[str, Any]:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    def _save_state(self, state: Dict[str, Any]) -> None:
        self.paths.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.paths.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(self.paths.state_file))

    def _blank_state(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "roadmap_file": None,
            "current_active_feature": None,
            "features": [],
            "inserted_fixups": [],
            "merged_features": [],
            "last_verified_merge_commit": None,
            "last_closure_verification_result": None,
            "blocked_reason": None,
            "updated_at": utc_now_iso(),
        }

    def load_state(self) -> Dict[str, Any]:
        if not self.paths.state_file.exists():
            return self._blank_state()

        state = json.loads(self.paths.state_file.read_text(encoding="utf-8"))

        file_pid = state.get("project_id")
        if file_pid is None:
            # One-shot migration: stamp unstamped legacy state, preserve feature progress.
            state["project_id"] = self.project_id
            self._save_state(state)
            return state

        if file_pid != self.project_id:
            # ADR-007: cross-tenant contamination guard — re-initialize rather than leak.
            return self._blank_state()

        return state

    def _normalize_feature(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        review_stack = raw.get("review_stack") or []
        if isinstance(review_stack, str):
            review_stack = [item.strip() for item in review_stack.split(",") if item.strip()]
        depends_on = raw.get("depends_on") or []
        return {
            "feature_id": raw["feature_id"],
            "title": raw["title"],
            "plan_path": raw["plan_path"],
            "branch_name": raw["branch_name"],
            "risk_class": raw.get("risk_class", "medium"),
            "merge_policy": raw.get("merge_policy", "human"),
            "review_stack": review_stack,
            "depends_on": depends_on,
            "status": raw.get("status", "planned"),
            "inserted": bool(raw.get("inserted", False)),
        }

    def _load_roadmap(self, roadmap_file: Path) -> List[Dict[str, Any]]:
        data = self._load_yaml(roadmap_file)
        features = data.get("features") or []
        normalized = [self._normalize_feature(feature) for feature in features]
        ids = [feature["feature_id"] for feature in normalized]
        if len(ids) != len(set(ids)):
            raise ValueError("ROADMAP.yaml contains duplicate feature_id values")
        return normalized

    def init_roadmap(self, roadmap_file: Path) -> Dict[str, Any]:
        features = self._load_roadmap(roadmap_file)
        state = self.load_state()
        state.update(
            {
                "project_id": self.project_id,
                "roadmap_file": str(roadmap_file.resolve()),
                "features": features,
                "current_active_feature": None,
                "inserted_fixups": [],
                "merged_features": [],
                "last_verified_merge_commit": None,
                "last_closure_verification_result": None,
                "blocked_reason": None,
                "updated_at": utc_now_iso(),
            }
        )
        self._save_state(state)
        emit_governance_receipt(
            "roadmap_transition",
            status="success",
            action="init",
            project_id=self.project_id,
            roadmap_file=str(roadmap_file.resolve()),
            feature_count=len(features),
        )
        return state

    def _materialize_plan(self, plan_path: Path) -> None:
        shutil.copy2(plan_path, _root_file(self.project_root, "FEATURE_PLAN.md"))
        manager = PRQueueManager()
        manager.load_feature_plan(str(_root_file(self.project_root, "FEATURE_PLAN.md")))

    def _ensure_feature_branch(self, branch_name: str) -> bool:
        """Create feature branch from origin/main if absent. Idempotent; returns True if created."""
        cwd = str(self.project_root)
        probe = subprocess.run(["git", "-C", cwd, "rev-parse", "--git-dir"], capture_output=True)
        if probe.returncode != 0:
            return False
        subprocess.run(["git", "-C", cwd, "fetch", "origin", "main"], capture_output=True)
        exists = subprocess.run(
            ["git", "-C", cwd, "show-ref", "--verify", f"refs/heads/{branch_name}"],
            capture_output=True,
        )
        if exists.returncode == 0:
            return False
        for base in ("origin/main", "HEAD"):
            r = subprocess.run(["git", "-C", cwd, "branch", branch_name, base], capture_output=True)
            if r.returncode == 0:
                return True
        return False

    def _provision_feature_worktree(self, branch_name: str) -> str:
        """Create a git worktree for branch_name and initialize .vnx-data isolation. Returns path or empty."""
        slug = branch_name.replace("/", "-")
        wt_path = self.state_dir.parent / "worktrees" / slug
        cwd = str(self.project_root)
        if not wt_path.exists():
            r = subprocess.run(
                ["git", "-C", cwd, "worktree", "add", str(wt_path), branch_name],
                capture_output=True,
            )
            if r.returncode != 0:
                return ""
        result = worktree_start(project_root=str(wt_path))
        return str(wt_path) if result.success else ""

    def load_feature(self, feature_id: str, no_worktree: bool = False) -> Dict[str, Any]:
        state = self.load_state()
        features = state.get("features") or []
        feature = next((item for item in features if item["feature_id"] == feature_id), None)
        if not feature:
            raise ValueError(f"Unknown feature_id: {feature_id}")

        plan_path = Path(feature["plan_path"])
        if not plan_path.is_absolute():
            plan_path = self.project_root / plan_path
        if not plan_path.exists():
            raise FileNotFoundError(f"Feature plan not found: {plan_path}")

        self._materialize_plan(plan_path)

        branch_name = feature["branch_name"]
        branch_created = self._ensure_feature_branch(branch_name)
        worktree_path = "" if no_worktree else self._provision_feature_worktree(branch_name)

        for item in features:
            if item["feature_id"] == feature_id:
                item["status"] = "active"
            elif item["status"] == "active":
                item["status"] = "planned"

        state["current_active_feature"] = feature_id
        state["blocked_reason"] = None
        state["updated_at"] = utc_now_iso()
        self._save_state(state)
        emit_governance_receipt(
            "roadmap_transition",
            status="success",
            action="load_feature",
            project_id=self.project_id,
            feature_id=feature_id,
            title=feature["title"],
            branch_name=branch_name,
            risk_class=feature["risk_class"],
            merge_policy=feature["merge_policy"],
            review_stack=feature["review_stack"],
            branch_created=branch_created,
            worktree_path=worktree_path,
        )
        return state

    def _detect_blocking_drift(self) -> List[Dict[str, Any]]:
        drift_path = self.paths.state_dir / "post_feature_drift.json"
        items: List[Dict[str, Any]] = []
        if drift_path.exists():
            try:
                payload = json.loads(drift_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
            for item in payload.get("items", []):
                category = str(item.get("category", "")).strip()
                if item.get("blocking") and category in ALLOWED_DRIFT_CATEGORIES:
                    items.append(item)

        open_items_path = self.paths.state_dir / "open_items.json"
        if open_items_path.exists():
            try:
                payload = json.loads(open_items_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
            for item in payload.get("items", []):
                category = str(item.get("category", "")).strip()
                if item.get("status") == "open" and item.get("severity") == "blocker" and category in ALLOWED_DRIFT_CATEGORIES:
                    items.append(item)

        seen = set()
        deduped = []
        for item in items:
            key = (item.get("id"), item.get("title"), item.get("category"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    def _insert_fixup_feature(self, state: Dict[str, Any], drift_items: List[Dict[str, Any]]) -> Dict[str, Any]:
        fixup_id = f"fixup-{utc_now_iso().replace(':', '').replace('-', '').lower()}"
        plan_dir = self.paths.generated_dir / fixup_id
        plan_dir.mkdir(parents=True, exist_ok=True)
        plan_path = plan_dir / "FEATURE_PLAN.md"
        bullet_list = "\n".join(f"- {item.get('title') or item.get('id') or item.get('category')}" for item in drift_items)
        plan_path.write_text(
            f"""# Feature: Runtime Fix-up — {fixup_id}

**Status**: Draft
**Priority**: P0
**Branch**: `feature/{fixup_id}`
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional

Primary objective:
Close the blocking post-feature drift discovered during roadmap reconciliation.

## Blocking Drift
{bullet_list}

## Dependency Flow
```text
PR-0 (no dependencies)
```

## PR-0: Blocking Runtime Fix-up
**Track**: C
**Priority**: P0
**Complexity**: Medium
**Risk**: Medium
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 2-6 hours
**Dependencies**: []

### Description
Implement the minimum blocking fix required before the roadmap may advance.

### Scope
{bullet_list}

### Success Criteria
- blocking drift is resolved
- no unrelated roadmap scope is added
- closure verifier passes after the fix-up merges

### Quality Gate
`gate_fixup_blocking_drift`:
- [ ] Blocking drift is fully addressed
- [ ] No unrelated feature work was folded into the fix-up
- [ ] Verification evidence exists for the addressed issue
""",
            encoding="utf-8",
        )

        feature = {
            "feature_id": fixup_id,
            "title": f"Runtime Fix-up — {fixup_id}",
            "plan_path": str(plan_path),
            "branch_name": f"feature/{fixup_id}",
            "risk_class": "medium",
            "merge_policy": "human",
            "review_stack": ["gemini_review", "codex_gate", "claude_github_optional"],
            "depends_on": [],
            "status": "planned",
            "inserted": True,
        }

        current_id = state.get("current_active_feature")
        features = state.get("features") or []
        insert_at = next((idx + 1 for idx, item in enumerate(features) if item["feature_id"] == current_id), len(features))
        features.insert(insert_at, feature)
        state["features"] = features
        state.setdefault("inserted_fixups", []).append(
            {"feature_id": fixup_id, "items": drift_items, "plan_path": str(plan_path), "inserted_at": utc_now_iso()}
        )
        state["updated_at"] = utc_now_iso()
        state["blocked_reason"] = "blocking_drift_fixup_inserted"
        self._save_state(state)

        emit_governance_receipt(
            "drift_fixup_inserted",
            status="success",
            feature_id=fixup_id,
            plan_path=str(plan_path),
            drift_items=drift_items,
        )
        return state

    def _gates_incomplete(self, feature: Dict[str, Any], gate_results_dir: Path) -> bool:
        """Return True when any required gate lacks a PASS result for the feature's PRs.

        Required gates: every entry in review_stack except claude_github_optional.
        ADR-007: project_id stamping is preserved — this check is additive.
        """
        review_stack = feature.get("review_stack") or []
        required_gates = [g for g in review_stack if g != "claude_github_optional"]
        if not required_gates:
            return False

        feature_plan_path = _root_file(self.project_root, "FEATURE_PLAN.md")
        pr_ids: List[str] = []
        if feature_plan_path.exists():
            content = feature_plan_path.read_text(encoding="utf-8")
            pr_ids = re.findall(r"^##\s+(PR-\d+):", content, re.MULTILINE)

        if not pr_ids:
            return True

        if not gate_results_dir.exists():
            return True

        branch = feature.get("branch_name", "")
        for pr_id in pr_ids:
            for gate in required_gates:
                # ADR-007 + ADR-005: scope lookup to current project_id and branch.
                result = _find_gate_result(gate, pr_id, gate_results_dir, branch=branch, project_id=self.project_id)
                if result is None:
                    return True
                passed, _ = gate_is_pass(result)
                if not passed:
                    return True
                # Hole 1: status-only pass is insufficient (T0 7-invariant closure contract).
                # Evidence must exist on disk and carry a contract_hash.
                report_path = (result.get("report_path") or "").strip()
                contract_hash = (result.get("contract_hash") or "").strip()
                if not report_path or not Path(report_path).exists():
                    return True
                if not contract_hash:
                    return True

        return False

    def reconcile(self) -> Dict[str, Any]:
        state = self.load_state()
        current_id = state.get("current_active_feature")
        if not current_id:
            result = {"verdict": "idle", "reason": "no_active_feature"}
            state["last_closure_verification_result"] = result
            state["updated_at"] = utc_now_iso()
            self._save_state(state)
            return result

        feature = next((item for item in state.get("features", []) if item["feature_id"] == current_id), None)
        if not feature:
            raise ValueError(f"Active feature missing from roadmap state: {current_id}")

        verification = verify_closure(
            project_root=self.project_root,
            feature_plan=_root_file(self.project_root, "FEATURE_PLAN.md"),
            pr_queue=_root_file(self.project_root, "PR_QUEUE.md"),
            branch=feature["branch_name"],
            mode="post_merge",
            claim_file=(self.paths.state_dir / "closure_claim.json") if (self.paths.state_dir / "closure_claim.json").exists() else None,
        )
        drift_items = self._detect_blocking_drift() if verification["verdict"] == "pass" else []
        if verification["verdict"] == "pass" and drift_items:
            verdict = "drift_blocked"
        elif verification["verdict"] == "pass":
            gate_results_dir = self.state_dir / "review_gates" / "results"
            verdict = "gates_incomplete" if self._gates_incomplete(feature, gate_results_dir) else "pass"
        else:
            verdict = "blocked"
        result = {
            "verdict": verdict,
            "feature_id": current_id,
            "closure_verification": verification,
            "drift_items": drift_items,
        }
        state["last_closure_verification_result"] = result
        state["last_verified_merge_commit"] = (((verification.get("pr") or {}).get("mergeCommit") or {}).get("oid"))
        state["blocked_reason"] = (
            "blocking_drift_detected" if drift_items else
            ("gates_incomplete" if verdict == "gates_incomplete" else
             (None if verdict == "pass" else "closure_verification_failed"))
        )
        state["updated_at"] = utc_now_iso()
        self._save_state(state)
        emit_governance_receipt(
            "closure_verification_result",
            status="success" if result["verdict"] == "pass" else "blocked",
            feature_id=current_id,
            verifier_result=result,
        )
        return result

    def _approvals_dir(self) -> Path:
        d = self.state_dir / APPROVALS_SUBDIR
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _approval_token_path(self, feature_id: str) -> Path:
        safe_id = feature_id.replace("/", "_")
        return self._approvals_dir() / f"{safe_id}.json"

    def _feature_requires_human_approval(self, feature: Dict[str, Any]) -> bool:
        """ADR-007: human gate required when merge_policy=human OR risk_class=high."""
        return feature.get("merge_policy") == "human" or feature.get("risk_class") == "high"

    def _load_valid_approval_token(self, feature_id: str) -> Optional[Dict[str, Any]]:
        """Return an unconsumed, project_id-stamped, feature-pinned token — or None."""
        token_path = self._approval_token_path(feature_id)
        if not token_path.exists():
            return None
        try:
            token = json.loads(token_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if token.get("project_id") != self.project_id:
            return None
        if token.get("feature_id") != feature_id:
            return None
        if token.get("consumed"):
            return None
        return token

    def _consume_approval_token(self, feature_id: str) -> None:
        """Invalidate the token (single-use). Atomic write."""
        token_path = self._approval_token_path(feature_id)
        try:
            token = json.loads(token_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            return
        token["consumed"] = True
        token["consumed_at"] = utc_now_iso()
        tmp = token_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(token, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(token_path))

    def approve(self, feature_id: str, actor: str, justification: str) -> Dict[str, Any]:
        """Issue a single-use human approval token for feature_id.

        ADR-007: token is project_id-stamped and feature-pinned.
        """
        state = self.load_state()
        features = state.get("features") or []
        feature = next((f for f in features if f["feature_id"] == feature_id), None)
        if not feature:
            raise ValueError(f"Unknown feature_id: {feature_id}")

        issued_at = utc_now_iso()
        token: Dict[str, Any] = {
            "project_id": self.project_id,
            "feature_id": feature_id,
            "actor": actor,
            "justification": justification,
            "issued_at": issued_at,
            "consumed": False,
            "consumed_at": None,
        }
        token_path = self._approval_token_path(feature_id)
        tmp = token_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(token, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(token_path))

        emit_governance_receipt(
            "roadmap_human_approval",
            status="success",
            action="approve",
            project_id=self.project_id,
            feature_id=feature_id,
            actor=actor,
            justification=justification,
            issued_at=issued_at,
        )
        return token

    def advance(self) -> Dict[str, Any]:
        state = self.load_state()
        reconcile_result = self.reconcile()
        if reconcile_result["verdict"] == "blocked":
            return {"advanced": False, "reason": "closure_verification_failed", "reconcile": reconcile_result}

        if reconcile_result["verdict"] == "gates_incomplete":
            return {"advanced": False, "reason": "gates_incomplete", "reconcile": reconcile_result}

        if reconcile_result["verdict"] == "drift_blocked":
            state = self.load_state()
            state = self._insert_fixup_feature(state, reconcile_result["drift_items"])
            fixup_id = state["inserted_fixups"][-1]["feature_id"]
            self.load_feature(fixup_id)
            return {"advanced": True, "reason": "blocking_fixup_inserted", "next_feature": fixup_id}

        state = self.load_state()
        current_id = state.get("current_active_feature")

        # Human approval gate: merge_policy=human OR risk_class=high requires a valid token.
        # conditional_auto + low delegates to auto_merge_policy (no token required).
        if current_id:
            cur_feature = next((f for f in state.get("features", []) if f["feature_id"] == current_id), None)
            if cur_feature and self._feature_requires_human_approval(cur_feature):
                token = self._load_valid_approval_token(current_id)
                if not token:
                    return {"advanced": False, "reason": "awaiting_human_approval", "feature_id": current_id}
                self._consume_approval_token(current_id)
                emit_governance_receipt(
                    "roadmap_human_approval",
                    status="success",
                    action="consumed",
                    project_id=self.project_id,
                    feature_id=current_id,
                    actor=token.get("actor"),
                )

        if current_id and current_id not in state.get("merged_features", []):
            state.setdefault("merged_features", []).append(current_id)
            for item in state.get("features", []):
                if item["feature_id"] == current_id:
                    item["status"] = "merged"
            self._save_state(state)

        merged = set(state.get("merged_features", []))
        next_feature = None
        for feature in state.get("features", []):
            if feature["status"] not in {"planned", "blocked"}:
                continue
            if all(dep in merged for dep in feature.get("depends_on", [])):
                next_feature = feature
                break

        if not next_feature:
            state["current_active_feature"] = None
            state["blocked_reason"] = None
            state["updated_at"] = utc_now_iso()
            self._save_state(state)
            emit_governance_receipt("roadmap_transition", status="success", action="advance_complete", project_id=self.project_id, merged_features=sorted(merged))
            return {"advanced": False, "reason": "no_remaining_features"}

        self.load_feature(next_feature["feature_id"])
        return {"advanced": True, "reason": "loaded_next_feature", "next_feature": next_feature["feature_id"]}

    def run_feature_step(self) -> Dict[str, Any]:
        """Dispatch the next dependency-ready PR for the active feature.

        ADR-007: emits project_id-stamped roadmap_dispatch_step receipt.
        Respects VNX_QUEUE_POPUP_ENABLED env switch via promote_dispatch.
        Returns dispatch_id + pr_id on success, or a no_ready_pr status without
        side effects when no queued PR is dependency-ready.
        """
        state = self.load_state()
        current_id = state.get("current_active_feature")
        if not current_id:
            return {"status": "no_active_feature", "reason": "no feature is currently active"}

        pr_manager = PRQueueManager()
        next_pr = pr_manager.get_next_pr()

        if not next_pr:
            emit_governance_receipt(
                "roadmap_dispatch_step",
                status="no_ready_pr",
                project_id=self.project_id,
                feature_id=current_id,
            )
            return {
                "status": "no_ready_pr",
                "feature_id": current_id,
                "reason": "no dependency-ready PR in queue",
            }

        pr_id = next_pr["id"]
        dispatch_id = pr_manager.create_dispatch_from_pr(pr_id)
        if not dispatch_id:
            return {
                "status": "failed",
                "feature_id": current_id,
                "pr_id": pr_id,
                "reason": "dispatch creation failed",
            }

        promoted = pr_manager.promote_dispatch(dispatch_id)
        if not promoted:
            return {
                "status": "failed",
                "feature_id": current_id,
                "pr_id": pr_id,
                "dispatch_id": dispatch_id,
                "reason": "dispatch promotion failed",
            }

        pr_manager.update_pr_status(pr_id, "in_progress")

        emit_governance_receipt(
            "roadmap_dispatch_step",
            status="success",
            project_id=self.project_id,
            feature_id=current_id,
            pr_id=pr_id,
            dispatch_id=dispatch_id,
        )
        return {
            "status": "dispatched",
            "feature_id": current_id,
            "pr_id": pr_id,
            "dispatch_id": dispatch_id,
        }

    def autopilot_tick(self) -> Dict[str, Any]:
        """Single-iteration autopilot tick. Feature-flag gated (VNX_ROADMAP_AUTOPILOT=1).

        Sequence: run_feature_step → (if queue drained) advance.
        advance() runs reconcile() internally (RA-3/3b gate enforcement) then the
        human approval check (RA-4), so this tick composes the full RA-1..5 loop.

        ADR-007: project_id-scoped via existing primitives — no extra scoping needed.
        ADR-018 Rule 2: single-claim, no loop. Caller (silence_watchdog) schedules repetition.
        """
        if os.environ.get("VNX_ROADMAP_AUTOPILOT", "0") not in ("1", "true", "True"):
            return {"status": "disabled", "reason": "VNX_ROADMAP_AUTOPILOT not set"}

        state = self.load_state()
        if not state.get("current_active_feature"):
            return {"status": "idle", "reason": "no_active_feature"}

        step_result = self.run_feature_step()

        if step_result["status"] == "dispatched":
            return {
                "status": "stepped",
                "feature_id": step_result["feature_id"],
                "pr_id": step_result["pr_id"],
                "dispatch_id": step_result["dispatch_id"],
            }

        if step_result["status"] in ("failed", "no_active_feature"):
            return {"status": step_result["status"], "step": step_result}

        # Queue drained (no_ready_pr) → reconcile (via advance) + approval gate.
        advance_result = self.advance()
        if advance_result.get("advanced"):
            return {"status": "advanced", "advance": advance_result}

        return {
            "status": "blocked",
            "reason": advance_result.get("reason"),
            "advance": advance_result,
        }

    def status(self) -> Dict[str, Any]:
        state = self.load_state()
        return {
            "roadmap_file": state.get("roadmap_file"),
            "current_active_feature": state.get("current_active_feature"),
            "merged_features": state.get("merged_features", []),
            "inserted_fixups": state.get("inserted_fixups", []),
            "blocked_reason": state.get("blocked_reason"),
            "features": state.get("features", []),
            "last_closure_verification_result": state.get("last_closure_verification_result"),
        }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="VNX roadmap manager")
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init")
    init_parser.add_argument("roadmap_file")
    init_parser.add_argument("--json", action="store_true")

    status_parser = sub.add_parser("status")
    status_parser.add_argument("--json", action="store_true")

    load_parser = sub.add_parser("load")
    load_parser.add_argument("feature_id")
    load_parser.add_argument("--json", action="store_true")
    load_parser.add_argument("--no-worktree", action="store_true", dest="no_worktree")

    reconcile_parser = sub.add_parser("reconcile")
    reconcile_parser.add_argument("--json", action="store_true")

    advance_parser = sub.add_parser("advance")
    advance_parser.add_argument("--json", action="store_true")

    approve_parser = sub.add_parser("approve")
    approve_parser.add_argument("feature_id")
    approve_parser.add_argument("--actor", required=True)
    approve_parser.add_argument("--justification", required=True)
    approve_parser.add_argument("--json", action="store_true")

    step_parser = sub.add_parser("step")
    step_parser.add_argument("--json", action="store_true")

    autopilot_parser = sub.add_parser("autopilot")
    autopilot_parser.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    manager = RoadmapManager()

    if args.command == "init":
        result = manager.init_roadmap(Path(args.roadmap_file))
    elif args.command == "status":
        result = manager.status()
    elif args.command == "load":
        result = manager.load_feature(args.feature_id, no_worktree=getattr(args, "no_worktree", False))
    elif args.command == "reconcile":
        result = manager.reconcile()
    elif args.command == "advance":
        result = manager.advance()
    elif args.command == "approve":
        result = manager.approve(args.feature_id, actor=args.actor, justification=args.justification)
    elif args.command == "step":
        result = manager.run_feature_step()
    elif args.command == "autopilot":
        result = manager.autopilot_tick()
    else:
        raise AssertionError("unreachable")

    print(json.dumps(result, indent=2) if getattr(args, "json", False) else json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
