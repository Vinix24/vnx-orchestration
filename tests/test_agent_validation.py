#!/usr/bin/env python3
"""
Test Agent Validation for VNX Intelligence System
"""

import sys
import json
from pathlib import Path

# Add parent directory to path
sys.path.append(str(Path(__file__).parent.parent / "scripts"))

from gather_intelligence import T0IntelligenceGatherer


def test_agent_validation():
    """Test agent validation functionality"""
    gatherer = T0IntelligenceGatherer()

    print("Testing Agent Validation...")
    print("-" * 40)

    # Test 1: Valid agent
    result = gatherer.validate_agent("backend-developer")
    assert result["valid"] == True, "backend-developer should be valid"
    print("✅ Test 1: Valid agent backend-developer - PASSED")

    # Test 2: Invalid agent
    result = gatherer.validate_agent("invalid-agent")
    assert result["valid"] == False, "Invalid agent should fail"
    assert "suggestion" in result, "Should suggest alternative"
    print(f"✅ Test 2: Invalid agent rejected, suggested '{result['suggestion']}' - PASSED")

    # Test 3: Empty agent (should be valid - no agent is OK)
    result = gatherer.validate_agent("")
    assert result["valid"] == True, "Empty agent should be valid"
    print("✅ Test 3: Empty agent allowed - PASSED")

    # Test 4: Agent suggestions
    test_cases = {
        "debug": "debugger",
        "refactor": "python-optimizer",
        "perf": "performance-profiler",
        "test": "quality-engineer"
    }

    for invalid, expected in test_cases.items():
        suggestion = gatherer.suggest_closest_agent(invalid)
        assert suggestion == expected, f"Should suggest {expected} for {invalid}"
        print(f"✅ Test 4.{invalid}: Suggests '{expected}' - PASSED")

    # Test 5: Dispatch blocking
    result = gatherer.gather_for_dispatch("Test task", "T1", "bad-agent")
    assert result.get("dispatch_blocked") == True, "Should block invalid agent"
    assert "error" in result, "Should have error message"
    print("✅ Test 5: Dispatch blocked for invalid agent - PASSED")

    # Test 6: Dispatch allowed
    result = gatherer.gather_for_dispatch("Test task", "T1", "backend-developer")
    assert result.get("agent_validated") == True, "Should validate correct agent"
    assert result.get("dispatch_blocked") != True, "Should not block valid agent"
    print("✅ Test 6: Dispatch allowed for valid agent - PASSED")

    print("-" * 40)
    print("All tests PASSED! ✅")

    # List all valid agents for reference
    print("\nValid agents in system:")
    for agent in gatherer.agent_directory:
        print(f"  - {agent}")

    return True


if __name__ == "__main__":
    try:
        test_agent_validation()
        sys.exit(0)
    except AssertionError as e:
        print(f"❌ Test FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        sys.exit(1)