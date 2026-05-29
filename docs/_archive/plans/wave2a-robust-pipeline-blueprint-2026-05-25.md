# Wave 2a Robust Pipeline — Architecture Blueprint

> 2026-05-25 — Architect output voor `feat/wave2a-robust-pipeline-blueprint`.
> Dispatch-ID: 20260525-064351-wave2a-robust-pipeline-blueprint
> Gebaseerd op 4 gefaalde apply-runs (2026-05-24 t/m 2026-05-25) en forensic analyse van alle logs, schema files, en migrator code.

---

## Executive Summary

Na 4 gefaalde Wave 2a Dag 2 MC cutover pogingen is het patroon duidelijk: elke run stuit op een **andere blocker** omdat er geen gelaagde pre-flight verdediging bestaat. De code is na PR #617 correct — maar environmental issues (macOS TCC, env contamination, backup sprawl) en architecturale gaps (4-in-1 design, geen per-project mode, geen test-mode-apply) maken een betrouwbare cutover structureel onmogelijk zonder systeemwijzigingen.

Dit blueprint adresseert alle 15 bekende issues (A1-A3 al opgelost, B1-B6, C1-C4, D1-D4) via een georderd PR-plan van 8 PRs. Geschatte totale doorlooptijd: ~3-5 werkdagen bij sequentiële worker dispatches.

**Directe blocker voor Run 5**: B1 (macOS TCC). Oplossen via PR-WAVE2A-1 is de critisch path.

---

## 1. Issue Prioritization Matrix

| ID | Omschrijving | Severity | Category | Effort | Geblokkeerd door | Blokkeert |
|----|-------------|----------|----------|--------|-----------------|-----------|
| A1 | `runtime_coordination_v10.sql` project_id index ordering | ~~FIXED~~ | code | - | - | - |
| A2 | `dispatch_experiments` table missing in canonical bootstrap | ~~FIXED~~ | code | - | - | - |
| A3 | `_restore_snapshot` masks primary exception with disk I/O error | ~~FIXED~~ | code | - | - | - |
| **B1** | **macOS TCC PermissionError op mission-control/.vnx-data** | **BLOCKER** | environmental | S | - | Run 5 |
| **B4** | **Geen TCC pre-flight check** | **BLOCKER** | architectural | S | - | B1 cleanup |
| B3 | 4-in-1 migrator: één project failure stopt alle 4 | HIGH | architectural | M | B1 | cutover |
| B5 | Backup sprawl: 5 × 6.7 GB = ~33.5 GB, geen retention policy | HIGH | operational | S | B1 | disk ruimte |
| B6 | Geen test-mode apply (dry-run schrijft niet, apply schrijft alles) | HIGH | architectural | M | B1, B3 | staging test |
| B2 | Parent process env inheritance — VNX_DATA_DIR cross-repo leak | WARN | operational | S | - | - |
| C1 | Installer template-leak — install.sh ships stale machine defaults | HIGH | upstream | M | - | C2 |
| C2 | Path hygiene FAIL — hardcoded /Users/ + .nvm paths in scripts | HIGH | upstream | M | C1 | - |
| C3 | Hook wrapper conflict — generic sessionstart.sh overschrijft conditional routing | WARN | upstream | S | C1 | - |
| C4 | vnx bootstrap-hooks gedrag onbekend — geen --check/dry-run | WARN | upstream | S | C1 | - |
| D1 | Stale tmux sessions — `vnx-mission-control` van 19 mei | WARN | hygiene | XS | - | - |
| D2 | Shell env contamination — VNX_HOME/VNX_DATA_DIR in tmux parent | WARN | hygiene | S | - | B2 |
| D3 | `.vnx-data exists but no snapshot metadata` WARN bij worktree-start | INFO | hygiene | S | - | - |
| D4 | 30 GB backup sprawl op ~/Documents (deels op MediaHDD) | HIGH | hygiene | XS | - | B5 |

**Effort schaal**: XS = <30 min, S = 1-2u, M = 3-4u, L = 5-8u

### Dependency graph

```
B4 + B1 → PR-WAVE2A-1 (TCC preflight + per-project backup)
    ↓
PR-WAVE2A-2 (per-project migrator mode)   PR-WAVE2A-3 (backup retention)
    ↓                                           ↓
PR-WAVE2A-4 (test-mode apply)
    ↓
[alle B's opgelost → cutover klaar voor staging test]

D1 + D2 → PR-WAVE2A-5 (env isolation runbook, operationeel)

C1 → PR-WAVE2A-6 (installer template-leak, upstream vnx-orchestration)
    ↓
PR-WAVE2A-7 (SEOcrawler path hygiene + hook conflict)

D3 + D4 → PR-WAVE2A-8 (housekeeping, parallel uitvoerbaar)
```

---

## 2. Architecture Recommendations

### 2.1 TCC Pre-flight Check (B1 + B4)

**Probleem**: `backup_projects()` opent een `tarfile.open()` en roept `tar.add(src_dir, ...)` aan zonder eerst te controleren of Python leestoegang heeft op de bronmap. macOS TCC blokkeert dit op `/Users/.../Desktop/BUSINESS/...` als de Python binary niet Full Disk Access heeft.

**Aanbevolen implementatie** (in `backup_projects()`, vóór de tarfile-loop):

```python
def _check_backup_access(projects: list[ProjectEntry]) -> list[str]:
    """Probe read access to each project's .vnx-data directory.
    Returns list of (project_id, path, error_str) tuples for inaccessible dirs."""
    failures = []
    for project in projects:
        src_dir = project.path / ".vnx-data"
        if not src_dir.is_dir():
            continue  # missing dir is caught later by BackupFailure
        try:
            os.listdir(src_dir)
        except PermissionError as exc:
            failures.append((project.project_id, str(src_dir), str(exc)))
    return failures
```

Aanroep in `main()` vóór `backup_projects()`:

```python
access_failures = _check_backup_access(projects)
if access_failures:
    for pid, path, err in access_failures:
        LOG.error("TCC/permission preflight FAIL: project=%s path=%s: %s", pid, path, err)
    LOG.error(
        "Fix: grant Full Disk Access to Python (%s) in "
        "System Settings → Privacy & Security → Full Disk Access",
        sys.executable,
    )
    return 3
```

**Voordelen**:
- Operator ziet meteen welk project TCC blokkeert + exact hoe te fixen
- Geen backup overhead (geen tar aanmaken voordat we weten dat het kan)
- Exit code 3 (backup failure) is consistent met bestaand contract

**Trade-off**: `os.listdir()` is een shallow probe — TCC blokkeert ook diepere subdirectories als FDA ontbreekt. Dit is voldoende omdat tarfile.add recursief dezelfde FDA-check nodig heeft. Een diepere probe zou te langzaam zijn voor elke run.

### 2.2 Per-project Migrator Mode (B3)

**Probleem**: Huidige design migreert alle 4 projecten in één aanroep. Eén failure (b.v. TCC op mission-control) stopt de hele batch.

**Aanbevolen implementatie**:

Voeg `--project=<id>` flag toe aan de CLI:

```python
parser.add_argument(
    "--project",
    type=str,
    default=None,
    metavar="PROJECT_ID",
    help=(
        "Migrate only this project_id (from registry). "
        "Enables targeted re-run after partial failure without re-applying "
        "already-succeeded projects. Multiple: --project a --project b."
    ),
    action="append",
    dest="projects_filter",
)
```

Filter `projects` na `load_registry()`:

```python
if args.projects_filter:
    valid_ids = {p.project_id for p in projects}
    unknown = set(args.projects_filter) - valid_ids
    if unknown:
        LOG.error("unknown project_id(s) in --project: %s; valid: %s", unknown, valid_ids)
        return 2
    projects = [p for p in projects if p.project_id in set(args.projects_filter)]
    LOG.info("--project filter: migrating subset: %s", [p.project_id for p in projects])
```

**Cutover sequentie met per-project mode**:
```bash
# Stap 1: TCC-vrij project eerst
python3 scripts/migrate_to_central_vnx.py --apply --confirm MIGRATE-NOW-2026 \
  --no-prompt --fresh-central --project vnx-orchestration

# Stap 2: na 24u burn-in
python3 scripts/migrate_to_central_vnx.py --apply --confirm MIGRATE-NOW-2026 \
  --no-prompt --project seocrawler-v2

# Stap 3 (na TCC fix voor MC):
python3 scripts/migrate_to_central_vnx.py --apply --confirm MIGRATE-NOW-2026 \
  --no-prompt --project mission-control

# Stap 4:
python3 scripts/migrate_to_central_vnx.py --apply --confirm MIGRATE-NOW-2026 \
  --no-prompt --project sales-copilot
```

**Kanttekening**: `--fresh-central` bootstrapt de hele centrale DB. Bij per-project runs geldt: alleen de éérste run gebruikt `--fresh-central`; latere runs zien de DB als populated en slaan bootstrap over. Dit is al correct door `_central_is_empty()`.

### 2.3 Backup Retention Policy (B5 + D4)

**Huidig probleem**: 5 backup-directories van elk ~6.7 GB = ~33.5 GB op `~/Documents/`. Groeit bij elke run.

**Aanbevolen design**:

```python
def cleanup_old_backups(
    backup_base: Path,
    keep_n: int = 3,
    max_age_days: int = 30,
) -> list[Path]:
    """Remove excess backups beyond keep_n most-recent, and any older than max_age_days.
    
    Only touches directories matching the pattern vnx-pre-p4-auto-backup-*.
    Returns list of removed paths.
    """
```

CLI flag:
```
--keep-backups N    Keep the N most recent backup directories (default: 3)
--cleanup-backups   After successful apply, remove backups beyond --keep-backups
```

**Retention regels**:
- Minimum: bewaar altijd de laatste succesvolle run (safety net)
- Default: `keep_n=3` — last 3 runs of 6.7 GB = 20 GB max
- Hard max: als `backup_base` disk usage > 50 GB, waarschuw operator vóór backup
- `--cleanup-backups` is OPT-IN (niet automatisch bij elke run)

**Operator workflow**:
```bash
# Na geslaagde apply, cleanup:
python3 scripts/migrate_to_central_vnx.py --cleanup-backups --keep-backups 2 --backup-base ~/Documents
```

### 2.4 Test-mode Apply (B6)

**Probleem**: Geen "staging-equivalent" mode. `--dry-run` schrijft niets; `--apply` schrijft alles. Tussenliggend: bootstrap + migraties simuleren zonder echte centrale DB te raken.

**Aanbevolen implementatie** — `--test-apply` flag:

```python
parser.add_argument(
    "--test-apply",
    action="store_true",
    help=(
        "Run the full bootstrap + migration chain against a TEMP central "
        "(in /tmp), using real source DBs read-only. Verifies the apply "
        "sequence succeeds without touching the live central DB. "
        "Combine with --project to test one project at a time."
    ),
)
```

Implementatie in `main()`:

```python
if args.test_apply:
    import tempfile
    with tempfile.TemporaryDirectory(prefix="vnx-test-apply-") as tmp_dir:
        test_state = Path(tmp_dir)
        LOG.info("test-apply: using temp central at %s", test_state)
        # Run full bootstrap + migrations against temp central
        # Source DBs remain read-only (unchanged from normal flow)
        args.central_state = test_state
        args.fresh_central = True
        args.no_prompt = True
        # ... run normal apply flow against temp central
        # At end: verify and report, then temp dir is auto-cleaned
```

**Voordeel**: operator kan volledige apply keten testen zonder live centrale DB te raken. Fundamenteel anders dan `--dry-run` (die source queries doet maar niets schrijft).

### 2.5 Installer Template-Leak Fix (C1)

**Probleem**: `install.sh` in het `vnx-orchestration` repo kopieert configuratiebestanden inclusief install-machine-specifieke defaults (vnx_demo/leadflow paden, /Users/vincentvandeth paden) naar target projecten.

**Root cause** (hypothese, te bevestigen bij C1 worker):
- `install.sh` gebruikt `cp` of `rsync` van de source repo's config templates
- Templates zijn nooit "blank" gemaakt na ontwikkeling op de install machine
- Geen template-substitutie voor `{{VNX_PROJECT_ROOT}}`, `{{VNX_DATA_DIR}}` etc.

**Aanbevolen design**:
1. Template variabelen in alle config-files: `{{VNX_PROJECT_ROOT}}`, `{{VNX_HOME}}`, `{{PROJECT_ID}}`
2. `install.sh` doet substitutie via `sed` of Python op install-time
3. CI test die controleert: geen hardcoded `/Users/` paden in geïnstalleerde output
4. Separeer "install-machine development defaults" van "blank project templates"

**Scope**: vnx-orchestration upstream repo (`~/Development/vnx-orchestration-system/` of equivalent). Dit is een ander repo dan dit worktree.

### 2.6 Environment Isolation (B2 + D2)

**Probleem**: VNX_HOME, VNX_DATA_DIR etc. uit parent tmux sessie lekken naar child Claude Code sessies. Een sessie gestart vanuit een VNX-repo pane erft de env vars van die repo.

**Aanbevolen design** (geen code change vereist, operationeel):

1. **Expliciete unset** in elke terminal launch script:
   ```bash
   unset VNX_HOME VNX_DATA_DIR VNX_PROJECT_ID VNX_STATE_DIR
   ```

2. **Verify-script** `scripts/check_env_isolation.sh`:
   ```bash
   #!/bin/bash
   # Check for cross-repo env contamination before migrator runs
   expected_project="$(basename $(git rev-parse --show-toplevel))"
   if [[ -n "$VNX_DATA_DIR" ]] && [[ "$VNX_DATA_DIR" != *"$expected_project"* ]]; then
       echo "WARN: VNX_DATA_DIR=$VNX_DATA_DIR may be from a different project"
       echo "Run: unset VNX_DATA_DIR VNX_HOME VNX_STATE_DIR"
       exit 1
   fi
   ```

3. **Pre-flight in migrator**: migrator checkt of `VNX_DATA_DIR` (indien gezet) consistent is met het opgegeven `--central-state` pad. Mismatch = WARN maar geen abort.

---

## 3. Ordered PR Plan

Volgorde is dependency-aware. PRs met hetzelfde niveau kunnen parallel dispatched worden.

### PR-WAVE2A-1: TCC Pre-flight Check + Per-project Backup Access Probe

| Kenmerk | Waarde |
|---------|--------|
| **Skill routing** | database-engineer |
| **Branch** | `fix/wave2a-tcc-preflight` |
| **Effort** | S (1-2u) |
| **Dependencies** | geen (eerste PR) |
| **Risk** | Laag — nieuwe check-only code, geen wijziging in backup logica |

**Files to touch**:
- `scripts/migrate_to_central_vnx.py` — voeg `_check_backup_access()` toe + aanroep in `main()` vóór `backup_projects()`

**Scope guard**: alleen `migrate_to_central_vnx.py`. Geen schema changes, geen tests aanpassen (alleen toevoegen).

**Acceptance criteria**:
1. `_check_backup_access(projects)` retourneert lijst van `(project_id, path, error)` tuples voor inaccessible dirs
2. `main()` roept check aan vóór backup; bij failure exit code 3 + actionable error message met `sys.executable` pad
3. Regression test: mock `os.listdir` zodat het raises `PermissionError` voor één project; assert exit code 3 en error message bevat "Full Disk Access"
4. Bestaande 72 tests groen
5. Codex gate clean

---

### PR-WAVE2A-2: Per-project Migrator Mode (`--project` flag)

| Kenmerk | Waarde |
|---------|--------|
| **Skill routing** | database-engineer |
| **Branch** | `fix/wave2a-per-project-mode` |
| **Effort** | M (3-4u) |
| **Dependencies** | PR-WAVE2A-1 |
| **Risk** | Medium — registry filtering raakt apply flow |

**Files to touch**:
- `scripts/migrate_to_central_vnx.py` — `--project` arg, registry filter, `--fresh-central` interaction
- `tests/test_migrate_to_central_vnx.py` of nieuw `tests/test_migrate_per_project.py`

**Scope guard**: alleen migrator + bijbehorende tests.

**Acceptance criteria**:
1. `--project vnx-orchestration` migreert uitsluitend het opgegeven project; andere projecten worden overgeslagen met INFO log
2. `--project unknown-id` geeft exit code 2 + error met geldige IDs
3. `--project` + `--fresh-central` werkt correct: bootstrap runt alleen bij eerste project (central is fresh); latere projecten slaan bootstrap over
4. Test: single-project apply in tmp-dir fixture slaagt, centrale DB bevat alleen rijen van dat project
5. Test: fresh-central + single project bootstrapt correct
6. Bestaande tests groen

---

### PR-WAVE2A-3: Backup Retention Policy

| Kenmerk | Waarde |
|---------|--------|
| **Skill routing** | backend-developer |
| **Branch** | `fix/wave2a-backup-retention` |
| **Effort** | S (1-2u) |
| **Dependencies** | geen (parallel met PR-WAVE2A-2) |
| **Risk** | Laag — bestaande backups worden alleen verwijderd bij expliciet gebruik van `--cleanup-backups` |

**Files to touch**:
- `scripts/migrate_to_central_vnx.py` — `cleanup_old_backups()` functie + `--cleanup-backups` + `--keep-backups` flags
- `tests/test_migrate_backup_retention.py` (nieuw)

**Acceptance criteria**:
1. `cleanup_old_backups(backup_base, keep_n=3)` verwijdert alle `vnx-pre-p4-auto-backup-*` directories buiten de `keep_n` meest recente
2. `--cleanup-backups` is OPT-IN; zonder flag: geen automatische cleanup
3. Geen cleanup van directories die NIET matchen op `vnx-pre-p4-auto-backup-*` pattern
4. Test: maak 5 fake backup dirs, roep cleanup aan met keep_n=2, assert 3 verwijderd + 2 intact
5. Test: geen flag = geen cleanup

---

### PR-WAVE2A-4: Test-mode Apply (`--test-apply` flag)

| Kenmerk | Waarde |
|---------|--------|
| **Skill routing** | database-engineer |
| **Branch** | `fix/wave2a-test-apply-mode` |
| **Effort** | M (3-4u) |
| **Dependencies** | PR-WAVE2A-1, PR-WAVE2A-2 |
| **Risk** | Medium — raakt main() flow; risico van divergentie test-path vs real-path |

**Files to touch**:
- `scripts/migrate_to_central_vnx.py` — `--test-apply` flag + temp-central redirect in `main()`
- `tests/test_migrate_test_apply_mode.py` (nieuw)

**Implementatie detail**: `--test-apply` is implementeerbaar door `args.central_state` te overschrijven naar een `tempfile.mkdtemp()` pad vóór de apply-flow begint, en `--fresh-central` + `--no-prompt` impliciet te zetten. Na completion: print summary + auto-clean temp dir.

**Acceptance criteria**:
1. `--test-apply` voert volledige bootstrap + migratie chain uit
2. Echte centrale DB (`~/.vnx-data/state/`) blijft ongewijzigd na `--test-apply`
3. Source DBs worden uitsluitend read-only geopend (ongewijzigd van normale flow)
4. Output vermeldt duidelijk "TEST MODE — geen writes naar live centrale DB"
5. Bij failure in test-apply: zelfde exit codes als normale apply (2/3/4)
6. Test: `--test-apply` slaagt op real source DBs (fixture equivalent); centrale DB leeg na test

---

### PR-WAVE2A-5: Env Isolation Check Script + Pre-flight Doc Update

| Kenmerk | Waarde |
|---------|--------|
| **Skill routing** | backend-developer |
| **Branch** | `fix/wave2a-env-isolation` |
| **Effort** | S (1-2u) |
| **Dependencies** | geen (parallel uitvoerbaar) |
| **Risk** | Laag — nieuwe check script + doc update |

**Files to touch**:
- `scripts/check_env_isolation.sh` (nieuw) — env contamination check script
- `scripts/migrate_to_central_vnx.py` — voeg env pre-flight check toe (WARN, niet abort)
- `claudedocs/runbook-fresh-central-migration-v3-2026-05-25.md` (nieuw) — updated runbook

**Acceptance criteria**:
1. `check_env_isolation.sh` detecteert VNX_DATA_DIR/VNX_HOME van een ander project en print actionable unset instructie
2. Migrator logt WARN (niet ERROR) als VNX_DATA_DIR gezet is en niet matcht met `--central-state`
3. Runbook v3 bevat expliciete "unset env vars" stap vóór elke apply run
4. Script is executable en heeft geen externe dependencies buiten bash

---

### PR-WAVE2A-6: Installer Template-Leak Fix (upstream vnx-orchestration)

| Kenmerk | Waarde |
|---------|--------|
| **Skill routing** | backend-developer |
| **Branch** | `fix/installer-template-leak` (in vnx-orchestration repo) |
| **Effort** | M (3-4u) |
| **Dependencies** | geen (ander repo, parallel uitvoerbaar) |
| **Risk** | Medium — installer change raakt alle toekomstige installs |
| **Repo** | `~/Development/vnx-orchestration-system/` (of het geïnstalleerde .vnx/ systeem) |

**Scope**: installer templates in het vnx-orchestration distributierepo. NIET in dit worktree.

**Files to touch** (in vnx-orchestration repo):
- `install.sh` of equivalent — voeg template-substitutie toe voor platform-specifieke paden
- Alle config template files die `/Users/`, `.nvm/versions/node`, of project-specifieke paden bevatten
- CI test die verifieert dat geen hardcoded machine-paden in geïnstalleerde output staan

**Acceptance criteria**:
1. `install.sh` installeert config templates zonder `/Users/vincentvandeth/` of vergelijkbare machine-paden
2. Alle `{{VNX_PROJECT_ROOT}}`, `{{VNX_HOME}}` placeholders worden correct gesubstitueerd op install-time
3. CI test faalt bij hardcoded `/Users/` in geïnstalleerde bestanden
4. SEOcrawler re-install na deze fix produceert schone config.yml zonder leakage
5. Existing install tests groen

---

### PR-WAVE2A-7: SEOcrawler Path Hygiene + Hook Conflict Fix

| Kenmerk | Waarde |
|---------|--------|
| **Skill routing** | backend-developer |
| **Branch** | `fix/seocrawler-path-hygiene` (in SEOcrawler_v2 repo of vnx-orchestration) |
| **Effort** | M (2-4u) |
| **Dependencies** | PR-WAVE2A-6 (template-leak fix moet eerst in) |
| **Risk** | Medium — hook wrapper aanpassen raakt terminal routing |

**Context**: Na PR-WAVE2A-6 kan SEOcrawler opnieuw worden geïnstalleerd met schone templates. Deze PR fixt de resulterende configuratieproblemen.

**Files to touch**:
- `.vnx/scripts/` in SEOcrawler_v2 — verwijder hardcoded paden, vervang met env vars
- Hook wrapper conflict (C3): voeg merge-logica toe zodat `sessionstart.sh` bestaande conditional routing respecteert
- `vnx_doctor` path check — fix of demote de false positives op `.vnx/scripts/` zelf

**Acceptance criteria**:
1. `vnx_doctor` geeft geen false-positive path matches op `.vnx/scripts/` eigen inhoud
2. `sessionstart.sh` overschrijft NIET de SEOcrawler conditional T0/T1/T2/T3 routing
3. Geen hardcoded `/Users/` of `.nvm/versions/node` paden in `.vnx/scripts/` na reinstall
4. SEOcrawler terminal routing werkt correct na upgrade naar rc3

---

### PR-WAVE2A-8: Operational Housekeeping (D1 + D3 + D4)

| Kenmerk | Waarde |
|---------|--------|
| **Skill routing** | backend-developer |
| **Branch** | `fix/wave2a-housekeeping` |
| **Effort** | XS-S (30 min - 1u) |
| **Dependencies** | geen (parallel uitvoerbaar) |
| **Risk** | Laag — hygiene actions, geen production code |

**Actions** (mix van scripts + docs):
- D1: Stale tmux session cleanup script `scripts/cleanup_stale_vnx_sessions.sh`
- D3: Voeg `vnx worktree-start` aanroep toe aan Wave 2a runbook
- D4: Voeg backup cleanup stap toe aan runbook (handmatig tot PR-WAVE2A-3 merged)

**Acceptance criteria**:
1. `cleanup_stale_vnx_sessions.sh` toont stale VNX sessions (>7 dagen) en vraagt bevestiging voor kill
2. Runbook bevat `vnx worktree-start` als stap 0
3. Runbook bevat handmatige backup cleanup instructie met `du -sh ~/Documents/vnx-pre-p4-auto-backup-*`

---

### PR execution order (aanbevolen)

```
Wave A (direct starten — critisch path):
  PR-WAVE2A-1  [database-engineer, S]

Wave B (na PR-WAVE2A-1):
  PR-WAVE2A-2  [database-engineer, M]  ← critical path voor cutover
  PR-WAVE2A-3  [backend-developer, S]  ← parallel
  PR-WAVE2A-5  [backend-developer, S]  ← parallel

Wave C (na PR-WAVE2A-2):
  PR-WAVE2A-4  [database-engineer, M]

Wave D (onafhankelijk, start direct):
  PR-WAVE2A-6  [backend-developer, M]  ← upstream repo, parallel
  PR-WAVE2A-8  [backend-developer, XS] ← parallel

Wave E (na PR-WAVE2A-6):
  PR-WAVE2A-7  [backend-developer, M]
```

**Totale critical path**: PR-WAVE2A-1 → PR-WAVE2A-2 → PR-WAVE2A-4 → staging test → cutover ≈ 3 werkdagen

---

## 4. End-to-End Test Plan

### Test Environment Setup

**Doel**: volledige apply-keten valideren in isolatie zonder live centrale DB te raken.

```bash
# Stap 1: tmp centrale dir aanmaken
CENTRAL_TEST_DIR=$(mktemp -d /tmp/vnx-central-test-XXXXXX)

# Stap 2: dry-run valideren (altijd eerst)
python3 scripts/migrate_to_central_vnx.py \
  --central-state "$CENTRAL_TEST_DIR" \
  --dry-run-manifest /tmp/test-manifest.json \
  2>&1 | tee /tmp/e2e-dry-run.log

# Stap 3: test-apply (na PR-WAVE2A-4 gemerged)
python3 scripts/migrate_to_central_vnx.py \
  --test-apply \
  --project vnx-orchestration \
  2>&1 | tee /tmp/e2e-test-apply-vnx.log
```

### Tests to Run

| Test | Commando | Pass criteria |
|------|----------|---------------|
| Unit tests | `pytest tests/test_migrate_to_central_vnx*.py -v` | 100% groen (72+ cases) |
| Fresh-central | `pytest tests/test_migrate_to_central_vnx_fresh_central.py -v` | alle 11 cases groen |
| Rollback exception | `pytest tests/test_migrate_rollback_exception_handling.py -v` | groen |
| TCC preflight | `pytest tests/test_migrate_tcc_preflight.py -v` (na PR-WAVE2A-1) | groen |
| Per-project mode | `pytest tests/test_migrate_per_project.py -v` (na PR-WAVE2A-2) | groen |
| Backup retention | `pytest tests/test_migrate_backup_retention.py -v` (na PR-WAVE2A-3) | groen |
| Test-apply mode | `pytest tests/test_migrate_test_apply_mode.py -v` (na PR-WAVE2A-4) | groen |

### Verify Queries (na staging apply)

```sql
-- Controleer dat alle 4 project_ids aanwezig zijn in centrale QI DB
SELECT project_id, COUNT(*) as rows
FROM dispatch_metadata
GROUP BY project_id
ORDER BY project_id;
-- Verwacht: vnx-orchestration, seocrawler-v2, mission-control, sales-copilot

-- Controleer dispatch_experiments aanwezig en gevuld
SELECT project_id, COUNT(*) as experiments
FROM dispatch_experiments
GROUP BY project_id;
-- Verwacht: minimaal vnx-orchestration met 1495 rijen (zie dry-run report)

-- Controleer geen cross-tenant collision in dispatch_ids
SELECT dispatch_id, COUNT(DISTINCT project_id) as projects
FROM dispatch_metadata
GROUP BY dispatch_id
HAVING projects > 1
LIMIT 10;
-- Verwacht: 0 rijen (alle dispatch_ids hebben project_id: prefix)

-- Runtime coordination: terminal leases geïsoleerd per project
SELECT project_id, COUNT(*) as leases
FROM terminal_leases
GROUP BY project_id;
```

### Rollback in Test Environment

```bash
# Test omgeving: simpel verwijderen
rm -rf "$CENTRAL_TEST_DIR"

# Productie: zie Cutover Runbook §6 rollback procedure
```

### Pass Criteria

1. Alle pytest suites 100% groen
2. Verify queries: alle 4 project_ids aanwezig in `dispatch_metadata`
3. `dispatch_experiments`: minimaal 1495 rijen voor `vnx-orchestration`
4. Geen cross-tenant collisions (query retourneert 0 rijen)
5. Exit code 0 van migrator

---

## 5. Productie Cutover Runbook (na alle PRs gemerged + staging test groen)

> Versie: v3 — specifiek voor Wave 2a na PR-WAVE2A-1 t/m WAVE2A-5

### Pre-flight Checks (voer uit in volgorde, stop bij failure)

**PF-1: Disk ruimte**
```bash
df -h ~/Documents ~/.vnx-data
# Vereiste: minimaal 10 GB vrij op ~/Documents (backup), 5 GB vrij op ~/.vnx-data
```

**PF-2: macOS TCC — Full Disk Access voor Python**
```bash
python3 -c "import os; os.listdir('/Users/$(whoami)/Desktop/BUSINESS/development/mission-control/.vnx-data'); print('TCC OK')"
# Bij PermissionError: System Settings → Privacy & Security → Full Disk Access → voeg $(which python3) toe
# Alternatief na PR-WAVE2A-1: python3 scripts/migrate_to_central_vnx.py --dry-run (check pre-flight failure message)
```

**PF-3: Env isolatie**
```bash
bash scripts/check_env_isolation.sh  # na PR-WAVE2A-5
# Of handmatig:
unset VNX_HOME VNX_DATA_DIR VNX_PROJECT_ID VNX_STATE_DIR
echo "VNX_DATA_DIR=${VNX_DATA_DIR:-<unset>}"  # moet <unset> zijn
```

**PF-4: Stale tmux sessions opruimen**
```bash
tmux ls 2>/dev/null | grep vnx
# Kill stale sessions:
# tmux kill-session -t vnx-mission-control  # als >7 dagen oud
```

**PF-5: Dry-run manifest vernieuwen (verplicht — manifest mag max 24u oud zijn)**
```bash
cd /Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt
python3 scripts/migrate_to_central_vnx.py --verbose \
  2>&1 | tee claudedocs/$(date +%Y-%m-%d)-p7-dry-run.log
# Controleer: exit code 0, manifest aangemaakt
```

**PF-6: Backup budget**
```bash
du -sh ~/Documents/vnx-pre-p4-auto-backup-*
# Bij >25 GB: handmatig cleanup (of na PR-WAVE2A-3: --cleanup-backups --keep-backups 2)
```

**PF-7: Existing central DB check**
```bash
ls -lh ~/.vnx-data/state/quality_intelligence.db 2>/dev/null || echo "FRESH (verwacht)"
# Fresh = geen bestaande DB (juist bij eerste cutover)
```

---

### Apply Sequence

> Gebruik `--project` mode (na PR-WAVE2A-2) voor granulaire controle.
> Elke stap: 24-48u burn-in voor monitoring voordat je verder gaat.

**STAP 1: vnx-orchestration (TCC-vrij, eigen project — laagste risico)**

```bash
cd /Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt

python3 scripts/migrate_to_central_vnx.py \
  --apply \
  --confirm MIGRATE-NOW-2026 \
  --no-prompt \
  --fresh-central \
  --project vnx-orchestration \
  --dry-run-manifest claudedocs/$(ls -t claudedocs/*dry-run-report.md.json | head -1 | xargs basename) \
  --verbose \
  2>&1 | tee claudedocs/$(date +%Y-%m-%d)-p7-apply-vnx.log

echo "Exit code: $?"
# Verwacht: 0
```

**Verify na stap 1**:
```bash
python3 scripts/migrate_to_central_vnx.py --verify-only --json | python3 -m json.tool | head -50
# Check: vnx-orchestration project_id aanwezig, verwachte row counts
```

**24u burn-in**: Monitor `~/.vnx-data/state/` op onverwachte writes. Controleer dat bestaande vnx-orchestration sessies normaal draaien.

---

**STAP 2: seocrawler-v2**

```bash
# TCC check op SEOcrawler path (ander repo, maar .vnx-data is lokaal)
python3 -c "import os; os.listdir('/Users/$(whoami)/Development/SEOcrawler_v2/.vnx-data'); print('TCC OK')"

python3 scripts/migrate_to_central_vnx.py \
  --apply \
  --confirm MIGRATE-NOW-2026 \
  --no-prompt \
  --project seocrawler-v2 \
  --verbose \
  2>&1 | tee claudedocs/$(date +%Y-%m-%d)-p7-apply-seo.log

echo "Exit code: $?"
```

**Verify na stap 2**: zelfde verify-only commando, controleer seocrawler-v2 row counts vs dry-run manifest.

**24u burn-in**.

---

**STAP 3: sales-copilot**

```bash
# TCC check (Desktop path)
python3 -c "import os; os.listdir('/Users/$(whoami)/Desktop/BUSINESS/development/sales-copilot/.vnx-data'); print('TCC OK')"

python3 scripts/migrate_to_central_vnx.py \
  --apply \
  --confirm MIGRATE-NOW-2026 \
  --no-prompt \
  --project sales-copilot \
  --verbose \
  2>&1 | tee claudedocs/$(date +%Y-%m-%d)-p7-apply-sc.log
```

---

**STAP 4: mission-control (vereist TCC Full Disk Access)**

> Voer PF-2 nogmaals uit voor deze stap. MC zit op Desktop/BUSINESS/ — meest restrictief.

```bash
# Verplichte TCC pre-flight
python3 -c "import os; os.listdir('/Users/$(whoami)/Desktop/BUSINESS/development/mission-control/.vnx-data'); print('TCC OK')"

# Bij PermissionError: STOP. Fix TCC, herhaal PF-2, dan pas verder.

python3 scripts/migrate_to_central_vnx.py \
  --apply \
  --confirm MIGRATE-NOW-2026 \
  --no-prompt \
  --project mission-control \
  --verbose \
  2>&1 | tee claudedocs/$(date +%Y-%m-%d)-p7-apply-mc.log
```

---

### Post-apply Verification

```bash
# Volledige verify suite
python3 scripts/migrate_to_central_vnx.py --verify-only --json \
  | tee claudedocs/$(date +%Y-%m-%d)-p7-verify-report.json | python3 -m json.tool

# Verwacht: exit 0, alle projecten aanwezig, row counts binnen ±5% van dry-run manifest
```

---

### Rollback Procedure

**Situatie A: backup beschikbaar, centrale DB corrupt**

```bash
# Identificeer meest recente backup
ls -lt ~/Documents/vnx-pre-p4-auto-backup-*/manifest.sha256 | head -3

# Verwijder corrupte centrale DBs
rm ~/.vnx-data/state/quality_intelligence.db
rm ~/.vnx-data/state/runtime_coordination.db
rm -f ~/.vnx-data/state/*.presnap.*

# Herstel via backup (handmatig, geen migrator betrokken)
BACKUP_DIR="<path naar meest recente backup>"
# Backups zijn tar.gz per project — centrale DB zit NIET in backup
# (backup bevat source .vnx-data dirs, niet de centrale)
# Centrale DB was fresh vóór eerste apply → gewoon verwijderen is rollback naar fresh state
echo "Central DB verwijderd; bij volgende start wordt centrale DB opnieuw aangemaakt"
```

**Situatie B: migrator crasht na partial apply (1-2 van 4 projecten geïmporteerd)**

```bash
# Snapshot restore (automatisch gedaan door migrator als pre_snapshot beschikbaar)
# Bij manuele rollback:
rm ~/.vnx-data/state/quality_intelligence.db
rm ~/.vnx-data/state/runtime_coordination.db
# Herhaal apply met alleen reeds gefaalde projecten via --project flag
```

**Situatie C: data corruption na burn-in gedetecteerd**

```bash
# Herstel uit backup via retroactive_backfill.py (per source project)
# Of: verwijder centrale DB, herhaal volledige apply sequence
```

---

## 6. Risks + Mitigations

### Risk 1: macOS TCC wordt niet permanent granted (B1 regressie)

**Scenario**: Operator grant Full Disk Access aan Python 3.12. Apple Silicon reset of OS upgrade revokes grants. Volgende migrator run faalt opnieuw met TCC.

**Kans**: Medium (TCC grants zijn persistent tot expliciet revoke, MAAR macOS updates kunnen grants resetten bij major updates).

**Detectie**: PF-2 pre-flight check loopt direct vóór elke run. PR-WAVE2A-1 maakt de fout vroeg en actiesbaar.

**Mitigatie**:
1. PF-2 staat in cutover runbook als verplichte stap
2. PR-WAVE2A-1 geeft exact sys.executable pad zodat re-grant eenvoudig is
3. Alternatief: verplaats mission-control `.vnx-data/` naar een locatie buiten Desktop/BUSINESS/ (bv. `~/Development/mission-control-data/`) — vereist geen TCC

**Residual risk na mitigatie**: Laag. Pre-flight detecteert het vóór backup poging.

---

### Risk 2: Per-project apply volgorde produceert inconsistente centrale DB

**Scenario**: PR-WAVE2A-2 is gemerged, operator past vnx-orchestration toe op dag 1 en mission-control op dag 3. In de tussentijd is er een schema migratie gemerged die de centrale DB format wijzigt. MC import faalt op schema mismatch.

**Kans**: Laag (schema migraties zijn zeldzaam, maar niet onmogelijk).

**Detectie**: `_assert_central_tables_exist()` loopt vóór elke per-project import. Schema versie mismatch leidt tot BootstrapFailure met duidelijk bericht.

**Mitigatie**:
1. Volledige apply sequence in één dag uitvoeren (gestaffeld per project, maar zelfde dag)
2. Dry-run manifest wordt herschreven als schemawijziging gedetecteerd (manifest is max 24u oud)
3. `--verify-only` na elke stap — inconsistentie zichtbaar vóór volgende import

---

### Risk 3: Backup sprawl keert terug na PR-WAVE2A-3 niet uitvoeren

**Scenario**: PR-WAVE2A-3 (backup retention) wordt gedeprioriseerd. Operator doet meerdere re-runs bij troubleshooting. ~/Documents loopt vol.

**Kans**: Medium (historisch al 5 × 6.7 GB = 33.5 GB na 4 runs).

**Detectie**: PF-6 in cutover runbook controleert disk usage voor elke run.

**Mitigatie**:
1. PF-6 is verplichte pre-flight stap in runbook
2. Handmatige cleanup instructie in runbook (geen code vereist)
3. PR-WAVE2A-3 is S-effort — aanbevolen snel te mergen

---

### Risk 4: SEOcrawler template-leak fix (PR-WAVE2A-6) breekt bestaande installs

**Scenario**: Install.sh template substitutie is incorrect geïmplementeerd. Bestaande SEOcrawler install krijgt lege config values (`VNX_HOME=""`) na reinstall.

**Kans**: Medium (template substitutie is error-prone bij diverse shell environments).

**Detectie**: CI test op geïnstalleerde output (acceptance criterion 3 van PR-WAVE2A-6).

**Mitigatie**:
1. PR-WAVE2A-6 worker testen op fresh temp directory vóór productierepo
2. PR-WAVE2A-7 is dependency op PR-WAVE2A-6 en valideert de end-state voor SEOcrawler

---

### Risk 5: `dispatch_experiments` heeft geen `project_id` kolom in source DBs (C1 interactie)

**Scenario**: Dry-run rapport (2026-05-25) toont `has_project_id_column: false` voor `dispatch_experiments` in vnx-orchestration source DB (1495 rijen). PR-WAVE2A-2 per-project import zal deze 1495 rijen importeren zonder valide project_id stamp — collision kans of lege project_id.

**Kans**: Hoog — dit is een bestaand data-kwaliteitsprobleem zichtbaar in het dry-run rapport.

**Detectie**: `_assert_central_tables_exist()` passeert, maar post-import verify query toont `project_id = NULL` of `project_id = 'vnx-dev'` voor dispatch_experiments rijen.

**Mitigatie**:
1. Pre-import: retroactive_backfill.py `--step experiments` uitvoeren op bron-DBs die `has_project_id_column: false` hebben
2. Check dry-run manifest op `has_project_id_column: false` vóór apply
3. Addendum aan cutover runbook: check alle `has_project_id_column` flags in manifest, backfill waar `false`

**Actie vereist**: Dit risico is niet gedekt door de huidige PR-plan. Aanbeveling: voeg pre-apply backfill check toe aan PR-WAVE2A-2 als acceptance criterion.

---

### Risk 6: Run 5 faalt op nieuwe onbekende blocker (patroon-herhaling)

**Scenario**: Na PR-WAVE2A-1 t/m WAVE2A-5 gemerged, Run 5 stuit op nog een onvoorziene environmental of code bug.

**Kans**: Laag (5 specifieke blockers zijn nu structureel geadresseerd). Lager dan bij eerdere runs omdat:
- Pre-flight checks nu 3 lagen diep (TCC, env, disk)
- Per-project mode isoleert failures
- Test-apply mode laat keten valideren vóór live central write

**Mitigatie**:
1. `--test-apply` eerst (PR-WAVE2A-4) — vangt 80% van onverwachte keten-failures
2. Verbose logging bewaren per stap (tee naar claudedocs/)
3. PR-WAVE2A-5 runbook bevat "stop-and-diagnose" instructie bij onverwachte exit codes

---

## Appendix: Forensic Data Summary

| Artifact | Locatie | Bevat |
|----------|---------|-------|
| Run 1 stderr | `claudedocs/2026-05-24-p4-apply.stderr.log` | `no such column: project_id` (A1) |
| Run 3 stderr | `claudedocs/2026-05-24-p5-apply.stderr.log` | `dispatch_experiments missing` + rollback masking (A2+A3) |
| Run 4 stderr | `claudedocs/2026-05-24-p6-apply.stderr.log` | TCC PermissionError op MC (B1) |
| Latest dry-run | `claudedocs/2026-05-25-p4-dry-run-report.md.json` | 1.9M rows, 4 projects, project_id flags |
| Failed DBs (4096B) | `~/.vnx-data/state/*.fresh-failed-*-20260524` | Empty schema state post-bug |
| Backup sprawl | `~/Documents/vnx-pre-p4-auto-backup-*` (5 dirs × 6.7 GB) | Forensic source state |
| PR #616 | commit `b8670dc` | A1 fix: v10.sql project_id index |
| PR #617 | commit `e976485` | A2+A3 fix: dispatch_experiments + rollback |

**Row counts uit dry-run manifest (2026-05-25 05:50 UTC)**:
- vnx-orchestration: 299,325 rows (QI + RC)
- seocrawler-v2: 1,522,517 rows
- mission-control: 28,615 rows
- sales-copilot: 54,666 rows
- **TOTAAL: 1,905,123 rows**

---

*Blueprint gegenereerd door architect dispatch 20260525-064351-wave2a-robust-pipeline-blueprint. Alle aanbevelingen zijn planning; implementatie via separate worker dispatches per PR.*
