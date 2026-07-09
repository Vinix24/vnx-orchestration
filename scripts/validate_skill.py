#!/usr/bin/env python3
"""
Skill Validation Script
Validates skill names before T0 creates dispatches

Usage:
    python3 validate_skill.py frontend-developer
    python3 validate_skill.py --list                # List all valid skills
    python3 validate_skill.py --check-disk          # Verify SKILL.md files exist

Exit codes:
    0 - Valid skill
    1 - Invalid skill
    2 - No skill provided
"""

import sys
import yaml
from pathlib import Path
from typing import List, Dict, Optional, Set

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from vnx_skills import iter_skill_dirs  # noqa: E402


class SkillValidator:
    """Validates skill names against skills.yaml registry and SKILL.md on disk"""

    def __init__(self):
        script_dir = Path(__file__).resolve().parent
        sys.path.insert(0, str(script_dir / "lib"))
        try:
            from vnx_paths import ensure_env
        except Exception as exc:
            raise SystemExit(f"Failed to load vnx_paths: {exc}")

        paths = ensure_env()
        self.skills_dir = Path(paths["VNX_SKILLS_DIR"])
        self.skills_file = self.skills_dir / "skills.yaml"
        self.skills = self._load_skills()
        self.directory_skills = self._load_directory_skills()

    def _load_skills(self) -> Dict:
        """Load skills from YAML registry"""
        if not self.skills_file.exists():
            print(f"Skills file not found: {self.skills_file}")
            sys.exit(1)

        with open(self.skills_file, 'r') as f:
            data = yaml.safe_load(f)
            return data.get('skills', {})

    def _load_directory_skills(self) -> Set[str]:
        """Discover skill directories not explicitly listed in the manifest."""
        manifest_names = {self.normalize_skill_name(data['name']) for data in self.skills.values()}
        return {
            self.normalize_skill_name(p.name)
            for p in iter_skill_dirs(self.skills_dir)
            if self.normalize_skill_name(p.name) not in manifest_names
        }

    def get_valid_skills(self) -> List[str]:
        """Get list of all valid skill names (without @ prefix)"""
        manifest = [skill_data['name'].lstrip('@') for skill_data in self.skills.values()]
        return sorted(set(manifest) | self.directory_skills)

    def normalize_skill_name(self, skill: str) -> str:
        """Normalize skill name (strip @ and / prefixes)"""
        return skill.strip().lstrip('@').lstrip('/')

    def skill_md_exists(self, skill_name: str) -> bool:
        """Check if SKILL.md file exists on disk for this skill"""
        normalized = self.normalize_skill_name(skill_name)
        return (self.skills_dir / normalized / "SKILL.md").exists()

    def validate(self, skill_name: str) -> tuple[bool, Optional[str]]:
        """
        Validate skill name against registry and disk.

        Args:
            skill_name: Skill to validate (e.g., 'frontend-developer')

        Returns:
            Tuple of (is_valid: bool, error_or_warning: Optional[str])
        """
        normalized = self.normalize_skill_name(skill_name)
        valid_skills = self.get_valid_skills()

        if normalized in valid_skills:
            if not self.skill_md_exists(normalized):
                return True, f"Warning: {normalized} is valid but SKILL.md not found on disk"
            return True, None
        else:
            # Find similar skills (fuzzy match)
            suggestions = self._find_similar(normalized, valid_skills)
            error = f"Invalid skill: {normalized}\n"
            if suggestions:
                error += f"   Did you mean: {', '.join(suggestions[:3])}\n"
            error += f"   Use --list to see all valid skills"
            return False, error

    def _find_similar(self, skill: str, valid_skills: List[str], max_suggestions: int = 3) -> List[str]:
        """Find similar skill names using simple string matching"""
        skill_clean = skill.lower().replace('@', '').replace('-', '')
        suggestions = []

        for valid in valid_skills:
            valid_clean = valid.lower().replace('@', '').replace('-', '')
            # Simple substring matching
            if skill_clean in valid_clean or valid_clean in skill_clean:
                suggestions.append(valid)

        return suggestions[:max_suggestions]

    def get_skill_info(self, skill_name: str) -> Optional[Dict]:
        """Get detailed information about a skill"""
        normalized = self.normalize_skill_name(skill_name)

        for skill_id, skill_data in self.skills.items():
            registry_name = skill_data['name'].lstrip('@')
            if registry_name == normalized:
                return {
                    'id': skill_id,
                    'name': registry_name,
                    'type': skill_data.get('type', 'unknown'),
                    'domain': skill_data.get('domain', 'general'),
                    'file': f"{registry_name}/SKILL.md",
                    'skill_md_exists': self.skill_md_exists(registry_name),
                    'token_count': skill_data.get('token_count', 0),
                    'triggers': skill_data.get('triggers', []),
                    'responsibilities': skill_data.get('responsibilities', [])
                }
        return None

    def list_skills_by_category(self):
        """List all skills grouped by type"""
        skills_by_type = {}

        for skill_id, skill_data in self.skills.items():
            skill_type = skill_data.get('type', 'other')
            if skill_type not in skills_by_type:
                skills_by_type[skill_type] = []
            skills_by_type[skill_type].append({
                'name': skill_data['name'],
                'domain': skill_data.get('domain', ''),
                'description': skill_data.get('responsibilities', [''])[0] if skill_data.get('responsibilities') else ''
            })

        return skills_by_type


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 validate_skill.py <skill-name>")
        print("   Example: python3 validate_skill.py frontend-developer")
        print("   Or: python3 validate_skill.py --list")
        print("   Or: python3 validate_skill.py --check-disk")
        sys.exit(2)

    validator = SkillValidator()

    if sys.argv[1] == '--list':
        # List all skills
        print("Valid VNX Skills\n")
        print("=" * 70)

        skills_by_type = validator.list_skills_by_category()

        for skill_type in sorted(skills_by_type.keys()):
            print(f"\n{skill_type.upper()}")
            print("-" * 70)
            for skill in sorted(skills_by_type[skill_type], key=lambda x: x['name']):
                domain = f" ({skill['domain']})" if skill['domain'] else ""
                disk = "OK" if validator.skill_md_exists(skill['name']) else "MISSING"
                print(f"  {skill['name']}{domain} [{disk}]")
                if skill['description']:
                    print(f"    -> {skill['description']}")

        print("\n" + "=" * 70)
        print(f"Total: {len(validator.get_valid_skills())} skills")
        sys.exit(0)

    elif sys.argv[1] == '--check-disk':
        # Check disk integrity
        issues = []
        for name in validator.get_valid_skills():
            if not validator.skill_md_exists(name):
                issues.append(name)
        if issues:
            print(f"Found {len(issues)} skills without SKILL.md on disk:")
            for name in issues:
                print(f"  {name}: SKILL.md not found")
                print(f"    Expected: {validator.skills_dir / name / 'SKILL.md'}")
            sys.exit(1)
        else:
            print(f"All {len(validator.get_valid_skills())} skills have SKILL.md on disk")
            sys.exit(0)

    elif sys.argv[1] == '--info':
        # Show detailed info about a skill
        if len(sys.argv) < 3:
            print("Usage: python3 validate_skill.py --info <skill-name>")
            sys.exit(2)

        skill_name = sys.argv[2]
        info = validator.get_skill_info(skill_name)

        if info:
            print(f"\nSkill Information: {info['name']}\n")
            print("=" * 70)
            print(f"Type: {info['type']}")
            print(f"Domain: {info['domain']}")
            print(f"File: {info['file']}")
            print(f"SKILL.md on disk: {'Yes' if info['skill_md_exists'] else 'MISSING'}")
            print(f"Token Count: {info['token_count']}")

            if info['triggers']:
                print(f"\nTriggers:")
                for trigger in info['triggers']:
                    print(f"  - {trigger}")

            if info['responsibilities']:
                print(f"\nResponsibilities:")
                for resp in info['responsibilities']:
                    print(f"  - {resp}")

            print("=" * 70)
            sys.exit(0)
        else:
            print(f"Skill not found: {skill_name}")
            sys.exit(1)

    else:
        # Validate skill
        skill_name = sys.argv[1]
        is_valid, message = validator.validate(skill_name)

        if is_valid:
            normalized = validator.normalize_skill_name(skill_name)
            print(f"Valid skill: {normalized}")

            info = validator.get_skill_info(skill_name)
            if info:
                disk = "SKILL.md found" if info['skill_md_exists'] else "SKILL.md MISSING"
                print(f"   Type: {info['type']}, Domain: {info['domain']}, Disk: {disk}")

            if message:
                print(f"   {message}")

            sys.exit(0)
        else:
            print(f"{message}")
            sys.exit(1)


if __name__ == "__main__":
    main()
