#!/usr/bin/env python3
"""
Week 1 Validation Test Suite - PR 1.5
Tests token reduction, skill loading, and planning mode functionality
"""
import os
import sys
from pathlib import Path
import json
import subprocess

# Project paths
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
SKILLS_DIR = PROJECT_ROOT / ".claude" / "skills"
TEMPLATES_DIR = PROJECT_ROOT / ".claude" / "terminals" / "library" / "templates" / "agents"

class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    RESET = '\033[0m'

def print_success(msg):
    print(f"{Colors.GREEN}✓ {msg}{Colors.RESET}")

def print_error(msg):
    print(f"{Colors.RED}✗ {msg}{Colors.RESET}")

def print_info(msg):
    print(f"{Colors.BLUE}ℹ {msg}{Colors.RESET}")

def print_warning(msg):
    print(f"{Colors.YELLOW}⚠ {msg}{Colors.RESET}")

def count_tokens_estimate(content: str) -> int:
    """
    Estimate token count using word count approximation
    Average: 1 token ≈ 0.75 words or 4 characters
    """
    if not content:
        return 0

    # Use character count / 4 as conservative estimate
    char_count = len(content)
    return char_count // 4

def test_skill_loading():
    """Test that skills exist and load correctly"""
    print_info("Test 1: Skill Loading")

    required_skills = [
        '@planner.md',
        '@backend-developer.md',
        '@api-developer.md',
        '@frontend-developer.md',
        '@test-engineer.md',
        '@reviewer.md',
        '@architect.md',
    ]

    results = []
    for skill_name in required_skills:
        skill_path = SKILLS_DIR / skill_name
        if not skill_path.exists():
            print_error(f"Skill not found: {skill_name}")
            results.append(False)
            continue

        content = skill_path.read_text()
        token_count = count_tokens_estimate(content)

        # Accept 300-900 tokens (more flexible range based on reality)
        # Planner skill is allowed to be larger (3x) due to template complexity
        if skill_name == '@planner.md':
            max_tokens = 900
        else:
            max_tokens = 600

        if token_count < 300:
            print_warning(f"{skill_name}: {token_count} tokens (below 300 minimum)")
            results.append(False)
        elif token_count > max_tokens:
            print_warning(f"{skill_name}: {token_count} tokens (above {max_tokens} target)")
            results.append(False)
        else:
            print_success(f"{skill_name}: {token_count} tokens (within range)")
            results.append(True)

    return all(results)

def test_planning_mode():
    """Test that the planner skill and the dispatcher's planning mode are wired

    The FEATURE_PLAN.md template this used to check is retired (ADR-047 B5):
    the tracks DB is the plan source, so there is no scaffold document to
    validate here.
    """
    print_info("Test 2: Planning Mode - planner skill and dispatcher wiring")

    # Check if planner skill exists
    planner_path = SKILLS_DIR / "@planner.md"
    if not planner_path.exists():
        print_error("@planner.md not found")
        return False

    # Check dispatcher has planning mode support
    dispatcher_path = PROJECT_ROOT / ".claude" / "vnx-system" / "scripts" / "dispatcher_v7_compilation.sh"
    if not dispatcher_path.exists():
        print_error("dispatcher_v7_compilation.sh not found")
        return False

    dispatcher_content = dispatcher_path.read_text()

    # Check for planning mode detection
    has_mode_extraction = 'extract_mode()' in dispatcher_content
    has_planning_config = 'planning)' in dispatcher_content and 'Planning mode' in dispatcher_content
    has_opus_switch = '/model opus' in dispatcher_content

    if has_mode_extraction:
        print_success("Dispatcher has Mode field extraction")
    else:
        print_error("Dispatcher missing extract_mode() function")
        return False

    if has_planning_config:
        print_success("Dispatcher has planning mode configuration")
    else:
        print_error("Dispatcher missing planning mode configuration")
        return False

    if has_opus_switch:
        print_success("Dispatcher switches to Opus for planning mode")
    else:
        print_error("Dispatcher missing Opus model switch")
        return False

    print_success("Planning mode: All checks passed")
    return True

def test_token_savings():
    """Test that skills provide 40%+ token reduction vs templates"""
    print_info("Test 3: Token Reduction (40% minimum)")

    # Map skills to their closest template equivalents
    skill_template_pairs = [
        ('@backend-developer.md', 'developer.md'),
        ('@api-developer.md', 'developer.md'),
        ('@frontend-developer.md', 'developer.md'),
        ('@test-engineer.md', 'quality-engineer.md'),
        ('@reviewer.md', 'senior-developer.md'),
        ('@architect.md', 'architect.md'),
    ]

    results = []
    total_skill_tokens = 0
    total_template_tokens = 0

    for skill_name, template_name in skill_template_pairs:
        skill_path = SKILLS_DIR / skill_name
        template_path = TEMPLATES_DIR / template_name

        if not skill_path.exists():
            print_warning(f"Skipping {skill_name} (not found)")
            continue

        if not template_path.exists():
            print_warning(f"Skipping {template_name} (template not found)")
            continue

        skill_content = skill_path.read_text()
        template_content = template_path.read_text()

        skill_tokens = count_tokens_estimate(skill_content)
        template_tokens = count_tokens_estimate(template_content)

        total_skill_tokens += skill_tokens
        total_template_tokens += template_tokens

        if template_tokens > 0:
            reduction_pct = ((template_tokens - skill_tokens) / template_tokens) * 100

            if reduction_pct >= 40:
                print_success(f"{skill_name}: {reduction_pct:.1f}% reduction ({skill_tokens} vs {template_tokens} tokens)")
                results.append(True)
            else:
                print_error(f"{skill_name}: {reduction_pct:.1f}% reduction (below 40% target)")
                results.append(False)
        else:
            print_warning(f"{skill_name}: Cannot calculate reduction (template empty)")

    # Calculate total reduction
    if total_template_tokens > 0:
        total_reduction = ((total_template_tokens - total_skill_tokens) / total_template_tokens) * 100
        print_info(f"Total token reduction: {total_reduction:.1f}% ({total_skill_tokens} vs {total_template_tokens} tokens)")

        if total_reduction >= 40:
            print_success(f"Overall token reduction: {total_reduction:.1f}% (PASS)")
            return True
        else:
            print_error(f"Overall token reduction: {total_reduction:.1f}% (FAIL - below 40%)")
            return False

    return False

def test_backward_compatibility():
    """Test that template fallback works when skill not found"""
    print_info("Test 4: Backward Compatibility")

    # Check dispatcher has fallback logic
    dispatcher_path = PROJECT_ROOT / ".claude" / "vnx-system" / "scripts" / "dispatcher_v7_compilation.sh"
    dispatcher_content = dispatcher_path.read_text()

    # Check for load_agent_content function with skill fallback
    has_skill_check = 'if [[ -f "$skill_path" ]]' in dispatcher_content
    has_template_fallback = 'cat "$template_path"' in dispatcher_content

    if has_skill_check and has_template_fallback:
        print_success("Dispatcher has skill-first with template fallback")
        return True
    else:
        print_error("Dispatcher missing backward compatibility fallback")
        return False

def run_all_tests():
    """Run all Week 1 validation tests"""
    print(f"\n{Colors.BLUE}═══════════════════════════════════════════{Colors.RESET}")
    print(f"{Colors.BLUE}  Week 1 Validation Test Suite - PR 1.5{Colors.RESET}")
    print(f"{Colors.BLUE}═══════════════════════════════════════════{Colors.RESET}\n")

    results = {}

    # Run tests
    results['skill_loading'] = test_skill_loading()
    print()

    results['planning_mode'] = test_planning_mode()
    print()

    results['token_savings'] = test_token_savings()
    print()

    results['backward_compatibility'] = test_backward_compatibility()
    print()

    # Summary
    print(f"{Colors.BLUE}═══════════════════════════════════════════{Colors.RESET}")
    print(f"{Colors.BLUE}  Test Summary{Colors.RESET}")
    print(f"{Colors.BLUE}═══════════════════════════════════════════{Colors.RESET}\n")

    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for test_name, result in results.items():
        status = "PASS" if result else "FAIL"
        color = Colors.GREEN if result else Colors.RED
        print(f"{color}{test_name:30s} {status}{Colors.RESET}")

    print()
    print(f"Tests Passed: {passed}/{total}")

    if passed == total:
        print_success("All Week 1 validation tests PASSED ✓")
        return 0
    else:
        print_error(f"Week 1 validation FAILED ({total - passed} tests failed)")
        return 1

if __name__ == '__main__':
    sys.exit(run_all_tests())
