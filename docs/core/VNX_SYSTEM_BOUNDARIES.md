# VNX System vs Project Boundaries
**Status**: Active
**Last Updated**: 2026-02-05
**Owner**: T-MANAGER

**Date**: January 19, 2026
**Purpose**: Define what belongs to VNX System (reusable) vs <project> Project (specific)

## Core Principle

VNX System = Generic orchestration framework
Project = Specific implementation using VNX

## VNX System Components (Goes to GitHub)

### 1. Core Orchestration Engine
```
vnx-system/
├── scripts/                      # All orchestration scripts
│   ├── dispatcher_v8_minimal.sh
│   ├── receipt_processor_v4.sh
│   ├── gather_intelligence.py
│   ├── gate_runner.py
│   └── [all other orchestration scripts]
├── schemas/                      # Data structure definitions
├── config/                       # System configuration templates
├── docs/                         # VNX documentation
│   ├── architecture/
│   ├── operations/
│   └── intelligence/
├── tests/                        # VNX system tests
└── state/                        # State management (structure only)
    └── .gitkeep                  # Empty folders with .gitkeep
```

### 2. Terminal Library (Generic Templates)
```
terminals/library/
├── templates/
│   ├── agents/                   # 11 agent role templates
│   ├── dispatches/               # Dispatch templates
│   ├── footers/                  # Report footers
│   ├── snippets/                # Reusable snippets
│   └── t0/                      # Manager block templates
├── agent_template_directory.yaml
├── TEMPLATE_COMPOSITION_GUIDE.md
└── TERMINAL_RULES.md
```

### 3. T-MANAGER Terminal (Part of VNX)
```
terminals/T-MANAGER/
├── CLAUDE.md                     # T-MANAGER is VNX's orchestration expert
└── modules/                      # T-MANAGER specific modules
```

### 4. Hooks (VNX-Specific)
```
hooks/
├── sessionstart_tmanager.sh      # T-MANAGER session hook
├── t0_pre_dispatch_intelligence.sh
└── preprompt_venv_activator.sh   # Generic venv activation
```

## Project Components (Stays in <project>)

### 1. Project Terminals (T0, T1, T2, T3)
```
terminals/T0/                     # <project> orchestrator
├── CLAUDE.md                     # Project-specific instructions
└── modules/                      # Project-specific modules

terminals/T1/                     # Worker (any task)
terminals/T2/                     # Worker (any task)
terminals/T3/                     # Worker (any task)
```

### 2. Project-Specific Folders
```
unified_reports/                  # Project reports (NOT vnx-system/unified_reports)
orchestration-docs/               # In T-MANAGER but project-specific
hooks/sessionstart_t0.sh         # T0 is project-specific
hooks/sessionstart_worker.sh      # Worker terminals are project-specific
```

### 3. Runtime Data (Never in VNX repo)
```
vnx-system/unified_reports/       # Runtime reports
vnx-system/receipts/              # Runtime receipts
vnx-system/state/*.ndjson        # Runtime state files
vnx-system/logs/*.log            # Runtime logs
vnx-system/dispatches/active/    # Active dispatches
vnx-system/dispatches/processed/ # Processed dispatches
vnx-system/dispatches/rejected/  # Rejected dispatches
```

## Directory Structure After Separation

### VNX Repository (Generic Framework)
```
vnx-orchestration-system/
├── core/
│   ├── scripts/                  # All orchestration scripts
│   ├── schemas/
│   ├── config/
│   ├── docs/
│   └── tests/
├── library/                      # Terminal library
│   ├── templates/
│   └── *.yaml, *.md
├── t-manager/                    # T-MANAGER terminal
│   ├── CLAUDE.md
│   └── modules/
├── hooks/                        # VNX-specific hooks
├── examples/
│   └── project-setup/            # Example project structure
└── README.md
```

### <project> Project (Using VNX)
```
<project>/
├── .claude/
│   ├── vnx-system -> [symlink to vnx repo/core]
│   ├── terminals/
│   │   ├── library -> [symlink to vnx repo/library]
│   │   ├── T-MANAGER -> [symlink to vnx repo/t-manager]
│   │   ├── T0/                  # Project terminal
│   │   ├── T1/                  # Project terminal
│   │   ├── T2/                  # Project terminal
│   │   └── T3/                  # Project terminal
│   └── hooks/
│       ├── sessionstart_t0.sh   # Project hook
│       └── sessionstart_worker.sh # Project hook
└── unified_reports/              # Project reports (NOT in .claude)
```

## Key Insights

### VNX System Provides:
1. **Orchestration Engine**: Dispatcher, smart tap, receipt processing
2. **Templates**: Agent roles, dispatch formats, report footers
3. **Intelligence**: Pattern matching, agent validation
4. **T-MANAGER**: The VNX expert terminal
5. **Documentation**: How to use VNX

### Projects Provide:
1. **Terminal Configuration**: T0, T1, T2, T3 with CLAUDE.md
2. **Project Documentation**: What the project does
3. **Runtime Data**: Reports, receipts, state
4. **Custom Hooks**: Project-specific session starts

## Migration Rules

### Include in VNX Repo:
- ✅ Generic orchestration scripts
- ✅ Template library
- ✅ T-MANAGER (VNX's own terminal)
- ✅ System documentation
- ✅ Schemas and configs

### Exclude from VNX Repo:
- ❌ T0, T1, T2, T3 terminals (project-specific)
- ❌ Runtime data (reports, receipts, logs)
- ❌ Project documentation
- ❌ unified_reports folder
- ❌ orchestration-docs content (project-specific)

### Updated Config Ownership (V8)

| Resource | Owner | Notes |
|----------|-------|-------|
| `settings.json` | **Shared** (patch-managed) | VNX patches only VNX-owned keys via `regen-settings --merge` |
| `.vnx-data/state/gate_results/` | VNX | Per-PR deterministic gate results |
| `.vnx-data/state/verification_results/` | VNX | Contract verification output |
| `.vnx-intelligence/` | **Shared** | Git-tracked, merge-safe; VNX exports, project may extend |
| `.vnx-data/dispatch_payloads/` | VNX | Temp transport files, auto-cleaned |
| `~/.vnx/projects.json` | VNX | Global project registry |

## Configuration Interface

When deploying VNX to a new project:

```yaml
# vnx.config.yaml (project-specific)
project:
  name: "MyNewProject"
  terminals:
    - name: T0
      model: opus
      role: orchestrator
    - name: T1
      model: sonnet
      role: worker

paths:
  reports: "./project_reports"    # Project's report location
  docs: "./project_docs"          # Project's documentation
```

## Benefits of This Separation

1. **VNX is truly generic** - No <project> references
2. **Easy deployment** - Copy VNX, configure terminals, start working
3. **Clear boundaries** - VNX provides framework, project provides implementation
4. **T-MANAGER included** - VNX expert travels with the system
5. **No confusion** - unified_reports stays in project, not VNX