#!/usr/bin/env python3
"""Roadmap orchestration and auto-next feature loading for VNX."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from copy import deepcopy
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
from closure_verifier import verify_closure


ALLOWED_DRIFT_CATEGORIES = {"bugfix", "post_cleanup", "governance_gap", "path/runtime regression"}


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
        self.paths.state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def load_state(self) -> Dict[str, Any]:
        if not self.paths.state_file.exists():
            return {
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
        return json.loads(self.paths.state_file.read_text(encoding="utf-8"))

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
            roadmap_file=str(roadmap_file.resolve()),
            feature_count=len(features),
        )
        return state

    def _materialize_plan(self, plan_path: Path) -> None:
        shutil.copy2(plan_path, _root_file(self.project_root, "FEATURE_PLAN.md"))
        manager = PRQueueManager()
        manager.load_feature_plan(str(_root_file(self.project_root, "FEATURE_PLAN.md")))

    def load_feature(self, feature_id: str) -> Dict[str, Any]:
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
            feature_id=feature_id,
            title=feature["title"],
            branch_name=feature["branch_name"],
            risk_class=feature["risk_class"],
            merge_policy=feature["merge_policy"],
            review_stack=feature["review_stack"],
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
        result = {
            "verdict": "pass" if verification["verdict"] == "pass" and not drift_items else ("drift_blocked" if drift_items else "blocked"),
            "feature_id": current_id,
            "closure_verification": verification,
            "drift_items": drift_items,
        }
        state["last_closure_verification_result"] = result
        state["last_verified_merge_commit"] = (((verification.get("pr") or {}).get("mergeCommit") or {}).get("oid"))
        state["blocked_reason"] = "blocking_drift_detected" if drift_items else (None if verification["verdict"] == "pass" else "closure_verification_failed")
        state["updated_at"] = utc_now_iso()
        self._save_state(state)
        emit_governance_receipt(
            "closure_verification_result",
            status="success" if result["verdict"] == "pass" else "blocked",
            feature_id=current_id,
            verifier_result=result,
        )
        return result

    def advance(self) -> Dict[str, Any]:
        state = self.load_state()
        reconcile_result = self.reconcile()
        if reconcile_result["verdict"] == "blocked":
            return {"advanced": False, "reason": "closure_verification_failed", "reconcile": reconcile_result}

        if reconcile_result["verdict"] == "drift_blocked":
            state = self.load_state()
            state = self._insert_fixup_feature(state, reconcile_result["drift_items"])
            fixup_id = state["inserted_fixups"][-1]["feature_id"]
            self.load_feature(fixup_id)
            return {"advanced": True, "reason": "blocking_fixup_inserted", "next_feature": fixup_id}

        state = self.load_state()
        current_id = state.get("current_active_feature")
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
            emit_governance_receipt("roadmap_transition", status="success", action="advance_complete", merged_features=sorted(merged))
            return {"advanced": False, "reason": "no_remaining_features"}

        self.load_feature(next_feature["feature_id"])
        return {"advanced": True, "reason": "loaded_next_feature", "next_feature": next_feature["feature_id"]}

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

    reconcile_parser = sub.add_parser("reconcile")
    reconcile_parser.add_argument("--json", action="store_true")

    advance_parser = sub.add_parser("advance")
    advance_parser.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    manager = RoadmapManager()

    if args.command == "init":
        result = manager.init_roadmap(Path(args.roadmap_file))
    elif args.command == "status":
        result = manager.status()
    elif args.command == "load":
        result = manager.load_feature(args.feature_id)
    elif args.command == "reconcile":
        result = manager.reconcile()
    elif args.command == "advance":
        result = manager.advance()
    else:
        raise AssertionError("unreachable")

    print(json.dumps(result, indent=2) if getattr(args, "json", False) else json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
