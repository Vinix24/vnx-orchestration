#!/usr/bin/env python3
"""
Feature Plan Validator (PR 1.4)

Validates FEATURE_PLAN.md documents for:
- PR size constraints (150-300 lines)
- Acyclic dependency graphs
- Required fields presence
"""

import re
import sys
from pathlib import Path
from typing import Dict, List, Set

try:
    import yaml as _yaml
except ImportError:
    _yaml = None  # type: ignore


class FeaturePlanValidator:
    """Validates feature plan structure and constraints."""

    MIN_LINES = 150
    MAX_LINES = 300

    def __init__(self, plan_path: str):
        self.plan_path = Path(plan_path)
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.prs: Dict[str, Dict] = {}

    def validate(self) -> bool:
        """Run all validation checks. Returns True if valid."""
        if not self.plan_path.exists():
            self.errors.append(f"Plan file not found: {self.plan_path}")
            return False

        content = self.plan_path.read_text()

        # Extract PR information
        self._extract_prs(content)

        # Run validation checks
        self._validate_pr_sizes()
        self._validate_dependencies()
        self._validate_required_fields()
        self._validate_skills()

        return len(self.errors) == 0

    def _extract_prs(self, content: str):
        """Extract PR information from plan content."""
        # Match PR sections: ### PR-X: Title (XXX-XXX lines)
        pr_pattern = r'### (PR-\d+): (.+?) \((\d+)-(\d+) lines\)'
        matches = re.finditer(pr_pattern, content)

        for match in matches:
            pr_id = match.group(1)
            title = match.group(2)
            min_lines = int(match.group(3))
            max_lines = int(match.group(4))

            # Find the PR section content
            pr_start = match.end()
            next_pr = re.search(r'\n### PR-\d+:', content[pr_start:])
            pr_end = pr_start + next_pr.start() if next_pr else len(content)
            pr_section = content[pr_start:pr_end]

            # Extract dependencies
            deps_match = re.search(r'\*\*Dependencies\*\*: (.+)', pr_section)
            dependencies = []
            if deps_match:
                deps_text = deps_match.group(1).strip()
                if deps_text.lower() != 'none':
                    dependencies = [d.strip() for d in deps_text.split(',')]

            # Extract skill
            skill_match = re.search(r'\*\*Skill\*\*: (@[\w-]+)', pr_section)
            skill = skill_match.group(1) if skill_match else None

            # Extract verification
            verify_match = re.search(r'\*\*Verification\*\*: (.+)', pr_section)
            verification = verify_match.group(1).strip() if verify_match else None

            # Extract estimated lines from Files section
            estimated_lines = self._extract_line_estimate(pr_section)

            self.prs[pr_id] = {
                'title': title,
                'min_lines': min_lines,
                'max_lines': max_lines,
                'dependencies': dependencies,
                'skill': skill,
                'verification': verification,
                'estimated_lines': estimated_lines
            }

    def _extract_line_estimate(self, pr_section: str) -> int:
        """Extract total line estimate from Files section."""
        # Match lines like: - `file.py` (~50 lines)
        file_pattern = r'- `[^`]+` \(~(\d+) lines\)'
        matches = re.finditer(file_pattern, pr_section)
        return sum(int(m.group(1)) for m in matches)

    def _validate_pr_sizes(self):
        """Validate all PRs are within 150-300 line constraint."""
        for pr_id, pr_data in self.prs.items():
            estimated = pr_data['estimated_lines']

            if estimated == 0:
                self.warnings.append(
                    f"{pr_id}: No line estimate found in Files section"
                )
                continue

            if estimated < self.MIN_LINES:
                self.errors.append(
                    f"{pr_id}: Estimated {estimated} lines < minimum {self.MIN_LINES}"
                )
            elif estimated > self.MAX_LINES:
                self.errors.append(
                    f"{pr_id}: Estimated {estimated} lines > maximum {self.MAX_LINES}"
                )

    def _validate_dependencies(self):
        """Validate dependency graph is acyclic."""
        # Build adjacency list
        graph: Dict[str, List[str]] = {pr: [] for pr in self.prs}
        for pr_id, pr_data in self.prs.items():
            graph[pr_id] = pr_data['dependencies']

        # Check for circular dependencies using DFS
        visited: Set[str] = set()
        rec_stack: Set[str] = set()

        def has_cycle(node: str) -> bool:
            visited.add(node)
            rec_stack.add(node)

            for neighbor in graph.get(node, []):
                if neighbor not in self.prs:
                    self.errors.append(
                        f"{node}: References non-existent dependency '{neighbor}'"
                    )
                    continue

                if neighbor not in visited:
                    if has_cycle(neighbor):
                        return True
                elif neighbor in rec_stack:
                    self.errors.append(
                        f"Circular dependency detected: {node} → {neighbor}"
                    )
                    return True

            rec_stack.remove(node)
            return False

        for pr_id in self.prs:
            if pr_id not in visited:
                has_cycle(pr_id)

    def _validate_required_fields(self):
        """Validate all required fields are present."""
        required_fields = ['skill', 'verification']

        for pr_id, pr_data in self.prs.items():
            for field in required_fields:
                if not pr_data.get(field):
                    self.errors.append(
                        f"{pr_id}: Missing required field '{field}'"
                    )

    @staticmethod
    def _load_valid_skills() -> set:
        """Load valid skill names from skills.yaml (single source of truth)."""
        skills_yaml = Path(__file__).parent.parent / "skills" / "skills.yaml"
        if _yaml is not None and skills_yaml.exists():
            with open(skills_yaml) as f:
                data = _yaml.safe_load(f)
            return {
                name.lstrip('@')
                for name in (
                    entry.get('name', key)
                    for key, entry in data.get('skills', {}).items()
                )
            }
        # Fallback: derive from directory names if yaml unavailable
        skills_dir = Path(__file__).parent.parent / "skills"
        return {
            p.name for p in skills_dir.iterdir()
            if p.is_dir() and (p / "SKILL.md").exists()
        }

    def _validate_skills(self):
        """Validate skill references are valid."""
        valid_skills = self._load_valid_skills()

        for pr_id, pr_data in self.prs.items():
            skill = pr_data.get('skill')
            if skill and skill.lstrip('@') not in valid_skills:
                self.warnings.append(
                    f"{pr_id}: Unknown skill '{skill}'. Valid: {', '.join(sorted(valid_skills))}"
                )

    def report(self) -> str:
        """Generate validation report."""
        lines = ["=" * 60]
        lines.append("FEATURE PLAN VALIDATION REPORT")
        lines.append("=" * 60)
        lines.append(f"Plan: {self.plan_path.name}")
        lines.append(f"PRs Found: {len(self.prs)}")
        lines.append("")

        # PR summary
        if self.prs:
            lines.append("PR Summary:")
            lines.append("-" * 60)
            for pr_id, pr_data in sorted(self.prs.items()):
                est = pr_data['estimated_lines']
                status = "✅" if self.MIN_LINES <= est <= self.MAX_LINES else "❌"
                lines.append(
                    f"{status} {pr_id}: {est} lines | "
                    f"Skill: {pr_data.get('skill', 'MISSING')} | "
                    f"Deps: {', '.join(pr_data['dependencies']) or 'None'}"
                )
            lines.append("")

        # Errors
        if self.errors:
            lines.append("❌ ERRORS:")
            lines.append("-" * 60)
            for error in self.errors:
                lines.append(f"  • {error}")
            lines.append("")

        # Warnings
        if self.warnings:
            lines.append("⚠️  WARNINGS:")
            lines.append("-" * 60)
            for warning in self.warnings:
                lines.append(f"  • {warning}")
            lines.append("")

        # Overall result
        lines.append("=" * 60)
        if self.errors:
            lines.append("RESULT: ❌ VALIDATION FAILED")
            lines.append(f"Errors: {len(self.errors)} | Warnings: {len(self.warnings)}")
        else:
            lines.append("RESULT: ✅ VALIDATION PASSED")
            if self.warnings:
                lines.append(f"Warnings: {len(self.warnings)} (non-blocking)")
        lines.append("=" * 60)

        return "\n".join(lines)


def main():
    """CLI entry point."""
    if len(sys.argv) != 2:
        print("Usage: validate_feature_plan.py <path-to-FEATURE_PLAN.md>")
        sys.exit(1)

    plan_path = sys.argv[1]
    validator = FeaturePlanValidator(plan_path)

    is_valid = validator.validate()
    print(validator.report())

    sys.exit(0 if is_valid else 1)


if __name__ == "__main__":
    main()
