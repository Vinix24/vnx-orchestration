# JSON Dispatch Format for VNX Orchestration V7
**Status**: Active
**Last Updated**: 2026-02-05
**Owner**: T-MANAGER
**Purpose**: Documentation for JSON Dispatch Format for VNX Orchestration V7.

## Overview
Smart Tap V7 introduces comprehensive JSON dispatch support with automatic Markdown translation. T0 can create dispatches in structured JSON format for machine-readable precision while maintaining optimal human readability through automatic translation.

**Key Features**:
- **Dual Format Support**: Both JSON and Markdown dispatches fully supported
- **Automatic Translation**: JSON converted to Markdown transparently for popups/terminals
- **25ms Average Translation Time**: High-performance JSON-to-Markdown conversion
- **JSON Archive**: Original JSON preserved in `queue/.json/` directory
- **Backward Compatible**: All existing Markdown dispatches continue working
- **Popup Display**: Automatically shows human-readable Markdown format

## JSON Dispatch Structure

### Complete Example
```json
{
  "dispatch_format": "json",
  "version": "7.0",
  "metadata": {
    "track": "A",
    "dispatch_id": "20250915-094500-abc123",
    "timestamp": "2025-09-15T09:45:00Z",
    "priority": "high",
    "timeout": 300,
    "gate": "implementation",
    "phase": "sprint.3.2",
    "cognition": "focused"
  },
  "content": {
    "title": "Implement WebVitals Plugin Architecture",
    "objective": "Create modular plugin system for web vitals metrics collection",
    "context": "Part of A3-2 sprint to modularize crawler functionality",
    "instructions": "1. Create plugin interface in src/crawler/plugins/\n2. Implement WebVitalsPlugin class\n3. Add plugin registration system\n4. Update tests",
    "success_criteria": [
      "Plugin interface defined and documented",
      "WebVitalsPlugin implements all required methods",
      "Tests pass with >90% coverage",
      "Memory usage stays under 20MB"
    ]
  }
}
```

### Minimal Example
```json
{
  "dispatch_format": "json",
  "metadata": {
    "track": "B",
    "gate": "testing"
  },
  "content": {
    "title": "Test Storage Pipeline",
    "instructions": "Run comprehensive storage tests and validate performance"
  }
}
```

## Field Descriptions

### Root Level Fields
- **dispatch_format** (required): Must be "json" to trigger translation
- **version** (optional): JSON format version (default: "7.0")

### metadata (required)
- **track** (required): Target track ("A", "B", or "C")
- **dispatch_id** (optional): Unique identifier (auto-generated if omitted)
- **timestamp** (optional): ISO 8601 timestamp (auto-generated if omitted)
- **priority** (optional): "low", "normal", "high", "critical" (default: "normal")
- **timeout** (optional): Seconds before timeout (default: 300)
- **gate** (optional): Current gate ("planning", "implementation", "review", "testing", "validation")
- **phase** (optional): Sprint phase identifier (e.g., "sprint.3.2")
- **cognition** (optional): Processing type ("normal", "focused", "deep")
- **target_open_items** (optional): JSON array of OI-IDs this dispatch targets for resolution (e.g., `["OI-042", "OI-043"]`). Auto-extracted from instruction text via `OI-\d{3,}` regex by the dispatcher. Used by CQS OI Delta scoring to detect unresolved targets.

### content (required)
- **title** (required): Brief task description
- **objective** (optional): High-level strategic goal
- **context** (optional): Background information and dependencies
- **instructions** (required): Step-by-step task details
- **success_criteria** (optional): Array of measurable outcomes
- **resources** (optional): Array of file paths or documentation references
- **constraints** (optional): Limitations or requirements to consider

## Translation to Markdown

Smart Tap V7 automatically converts JSON to Markdown:

```markdown
[[TARGET:A]]
[[DISPATCH_ID:20250915-094500-abc123]]
[[TIMESTAMP:2025-09-15T09:45:00Z]]
[[PRIORITY:normal]]
[[TIMEOUT:300]]
[[GATE:implementation]]
[[PHASE:sprint.3.2]]

---

## Task: Implement WebVitals Plugin Architecture

### Objective
Create modular plugin system for web vitals metrics collection

### Context
Part of A3-2 sprint to modularize crawler functionality

### Instructions
1. Create plugin interface in src/crawler/plugins/
2. Implement WebVitalsPlugin class
3. Add plugin registration system
4. Update tests

### Success Criteria
- Plugin interface defined and documented
- WebVitalsPlugin implements all required methods
- Tests pass with >90% coverage
- Memory usage stays under 20MB

[[DONE]]
```

## Benefits of JSON Format

1. **Machine Precision**: Structured data prevents parsing errors
2. **Metadata Rich**: Easy to add/extend metadata fields
3. **Validation Ready**: Can validate against JSON schema
4. **Analytics Friendly**: Easy to aggregate and analyze
5. **Version Control**: Clean diffs in git

## Smart Tap V7 Translation Workflow

### 1. Automatic Detection & Processing
```
T0 Terminal Output → Smart Tap V7 Monitor (2s interval)
     ↓
JSON Detection: {"dispatch_format":"json"} pattern
     ↓
Translation Engine: JSON → Markdown conversion (25ms avg)
     ↓
Dual Storage: 
  - Markdown: dispatches/queue/{timestamp}-{track}.md
  - JSON Archive: dispatches/queue/.json/{timestamp}-{track}.json
     ↓
Popup Display: Shows human-readable Markdown format
     ↓
Terminal Delivery: Optimized Markdown for Claude processing
```

### 2. Translation Features
- **Intelligent Field Mapping**: JSON fields mapped to appropriate Markdown sections
- **Metadata Preservation**: All JSON metadata converted to [[FIELD:value]] format
- **Array Handling**: success_criteria arrays converted to bullet lists
- **Content Formatting**: Proper Markdown structure with headers and sections
- **Performance**: 25ms average translation time with jq + bash fallback

## Advanced JSON Features

### Dynamic Field Generation
```json
{
  "dispatch_format": "json",
  "metadata": {
    "track": "A",
    "dispatch_id": "auto-generated",
    "timestamp": "auto-generated",
    "priority": "high",
    "cognition": "focused"
  },
  "content": {
    "title": "Complex Multi-Stage Task",
    "objective": "Implement advanced crawler feature with tests",
    "context": "Part of performance optimization sprint",
    "instructions": "1. Analyze current bottlenecks\n2. Design optimization strategy\n3. Implement with benchmarks\n4. Validate performance gains",
    "success_criteria": [
      "Performance improved by >50%",
      "Memory usage reduced by >20%",
      "All tests pass with >95% coverage",
      "Documentation updated"
    ],
    "resources": [
      "docs/performance-guidelines.md",
      "benchmarks/baseline-metrics.json"
    ],
    "constraints": [
      "Must maintain API compatibility",
      "No external dependencies",
      "Complete within sprint timeline"
    ]
  }
}
```

### Priority-Based Examples

#### High Priority Task
```json
{
  "dispatch_format": "json",
  "metadata": {
    "track": "C",
    "priority": "critical",
    "cognition": "deep",
    "timeout": 600
  },
  "content": {
    "title": "Critical Security Audit",
    "instructions": "Perform comprehensive security analysis of crawler system"
  }
}
```

#### Standard Development Task
```json
{
  "dispatch_format": "json",
  "metadata": {
    "track": "B",
    "gate": "implementation",
    "cognition": "focused"
  },
  "content": {
    "title": "Implement Storage Caching",
    "instructions": "Add Redis caching layer to storage pipeline"
  }
}
```

## T0 Usage Patterns

### Direct JSON Output
```bash
# T0 creates structured dispatch
echo '{
  "dispatch_format": "json",
  "metadata": {
    "track": "A",
    "priority": "high",
    "gate": "testing"
  },
  "content": {
    "title": "Run E2E Tests",
    "instructions": "Execute full test suite and validate results"
  }
}'
```

### Template-Based Generation
```bash
# T0 can use variables for dynamic dispatch creation
TRACK="B"
TASK="Database Migration"
echo "{
  \"dispatch_format\": \"json\",
  \"metadata\": {
    \"track\": \"$TRACK\",
    \"dispatch_id\": \"$(date +%Y%m%d-%H%M%S)-migration\",
    \"priority\": \"high\"
  },
  \"content\": {
    \"title\": \"$TASK\",
    \"instructions\": \"Execute database schema migration with rollback plan\"
  }
}"
```

## Backward Compatibility & Migration

### Dual Format Support
- **JSON Format**: Machine-readable with structured metadata
- **Markdown Format**: Human-readable legacy format
- **No Breaking Changes**: All existing Markdown dispatches work unchanged
- **Gradual Migration**: Teams can adopt JSON format incrementally

### File Organization
```
dispatches/queue/
├── 20250915-143000-A.md          # Translated Markdown (for popup/terminal)
├── 20250915-143100-B.md          # Legacy Markdown (unchanged)
└── .json/
    └── 20250915-143000-A.json    # Original JSON (archived)
```

### Smart Detection Logic
Smart Tap V7 automatically detects format:
- **JSON**: Starts with `{"dispatch_format":"json"`
- **Markdown**: Contains `[[TARGET:X]]` and `[[DONE]]` markers
- **Processing**: JSON → Translation → Markdown output
- **Legacy**: Markdown → Direct processing (unchanged)

## Implementation Status V7

✅ **Smart Tap V7**: Complete JSON translation engine  
✅ **Automatic Detection**: JSON/Markdown format recognition  
✅ **Translation Layer**: JSON-to-Markdown converter (25ms avg)  
✅ **Dual Storage**: Markdown output + JSON archive  
✅ **Popup Integration**: Displays translated Markdown  
✅ **Backward Compatibility**: Legacy Markdown unchanged  
✅ **Performance**: High-speed translation with fallback parsing  
✅ **Validation**: Both JSON and Markdown format validation  

## Performance Metrics

- **Translation Speed**: 25ms average (jq + bash fallback)
- **Detection Accuracy**: 100% format recognition
- **Memory Usage**: <5MB for translation process
- **CPU Impact**: <2% during translation
- **Storage Efficiency**: JSON archives 30% smaller than Markdown

## Future Enhancements

1. **JSON Schema Validation**: Formal schema for dispatch validation
2. **Priority Queue**: JSON metadata enables smart queue sorting
3. **Analytics Dashboard**: Rich metadata for dispatch analytics
4. **Template Library**: Reusable JSON dispatch templates
5. **API Integration**: REST API for programmatic dispatch creation