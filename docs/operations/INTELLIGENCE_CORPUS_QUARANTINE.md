# Intelligence-corpus-quarantaine: operatorrunbook

**Doel:** het intelligence-corpus (`code_snippets` en `snippet_metadata`) uit een store halen zonder het te wissen, en het met één bewezen commando terugzetten. De policy staat in `docs/governance/decisions/ADR-070-intelligence-corpus-quarantine.md`; dit runbook is de uitvoering.

De tool is operatorgereedschap, geen `bin/vnx`-subcommando: `scripts/vnx_intelligence_corpus_quarantine.py`. Hij werkt per store en raakt alleen de store die `--db` noemt.

## Paden

```
DB    = ~/.vnx-data/vnx-dev/state/quality_intelligence.db
Q     = ~/.vnx-data/_quarantine/intelligence-corpus-<YYYYMMDD>
ART   = $Q/artifact
AUDIT = <state_dir>/intelligence_corpus_quarantine_audit.ndjson
```

`<state_dir>` is `$VNX_STATE_DIR` tenzij je `--state-dir` meegeeft. `manifest.json`, `LEESMIJ.md` en `artifact/` staan in `$Q`; het artefact `$ART/quality_intelligence-corpus-<YYYYMMDD>.db` is na de purge het enige terugzetpad.

## Vooraf

1. **Stop de daemon die de store schrijft** (`scripts/intelligence_daemon.py`) en houd hem gestopt tot de purge klaar is.
2. **Niet in hetzelfde venster als de v35-migratie** van `self-learning-loop-reactivate` D5 (zelfde bestand van 442 MB) **en niet rond 02:00**, wanneer `com.vnx.conversation-analyzer` `session_analytics` schrijft en `quality_db_init` draait.
3. **Zorg voor ruimte en bestaande mappen.** De snapshot is een volledige `VACUUM INTO`-kopie (circa 442 MB) en het artefact circa 330 MB. `--out` en de map van `--snapshot` moeten al bestaan; anders weigert de CLI met exit 2 (`scripts/vnx_intelligence_corpus_quarantine.py:88`).
4. **De volgorde is bindend:** `baseline` -> `quarantine` -> `verify` -> `rehearse` -> `purge`. `verify` is de groene knop; `purge` herhaalt dezelfde permit binnen zijn schrijftransactie, want tussen `verify` en `purge` kan een schrijver iets veranderen.

## De live volgorde (één venster)

```bash
DB=~/.vnx-data/vnx-dev/state/quality_intelligence.db
Q=~/.vnx-data/_quarantine/intelligence-corpus-$(date -u +%Y%m%d)
ART="$Q/artifact"
SNAP=~/.vnx-data/_backups/$(date -u +%Y%m%d-%H%MZ)-intelligence-corpus-quarantine/vnx-dev/quality_intelligence.db
mkdir -p "$Q" "$(dirname "$SNAP")"

# Eén keten: elke stap draait alleen als de vorige exit 0 gaf.
# 1. Nulmeting plus een bewezen snapshot
python3 scripts/vnx_intelligence_corpus_quarantine.py baseline --db "$DB" --out "$Q" --snapshot "$SNAP" &&
# 2. Het corpus naar de verzegelde quarantaine (het artefact)
python3 scripts/vnx_intelligence_corpus_quarantine.py quarantine --db "$DB" --out "$Q" &&
# 3. De permit: alle negen eisen, niets veranderd
python3 scripts/vnx_intelligence_corpus_quarantine.py verify --db "$DB" --manifest "$Q/manifest.json" --quarantine "$ART" --human &&
# 4. Dezelfde vijf stadia op een kopie van de live store
python3 scripts/vnx_intelligence_corpus_quarantine.py rehearse --db "$DB" --manifest "$Q/manifest.json" --quarantine "$ART" &&
# 5. Leegmaken in één transactie, daarna wal_checkpoint(TRUNCATE) en VACUUM
python3 scripts/vnx_intelligence_corpus_quarantine.py purge --db "$DB" --manifest "$Q/manifest.json" --quarantine "$ART"
```

`purge` gaat alleen na een `rehearse` met exit 0 en `"ok": true`; bij elke andere exit stopt de keten en wordt niets leeggemaakt. Er staat bewust geen `set -e` of `exit` in: een geplakte `exit` sluit je terminal.

De volgorde in `purge` is vast: `DELETE` in één transactie, `COMMIT`, `wal_checkpoint(TRUNCATE)`, `VACUUM`, de na-meting en de auditregel `vacuumed` (`scripts/lib/intelligence_corpus_quarantine/purge.py:124-136`). Er draait geen FTS5 `optimize`; de winst in bestandsgrootte komt van `VACUUM` en wordt in clausule 4 gemeten.

**Een extra `verify` hoort vóór `purge`**, bijvoorbeeld als zegelcontrole (B4) na een pauze tussen stap 3 en stap 5. Na `purge` weigert `verify` bewust met exit 3 (`code_snippets live rows differ from the artefact`, want de live tabellen zijn leeg) en voegt het een `verified`-regel met `"status":"refused"` toe aan `$AUDIT`. Dat is geen fout. Elk commando dat de laatste `verified`-regel leest kiest daarom de `ok`-regel, zie clausule 3.

`quarantine` weigert (exit 3) als `$ART` al bestaat; een tweede quarantaine is een nieuwe `$Q`. `rehearse` laat de kopie staan en noemt zijn pad; alleen `--discard-copy` ruimt die kopie op, niets anders. `baseline` heeft geen `--state-dir` en schrijft geen auditregel; de auditreeks begint bij `quarantined`.

## Exitcodes

De CLI zet de uitzonderingen om op een code (`scripts/vnx_intelligence_corpus_quarantine.py:110-115`); `docs/EXIT_CODES.md` heeft dezelfde tabel.

| Code | Wat het voor jou betekent | Wat je doet |
|---|---|---|
| `0` | klaar | ga naar de volgende stap |
| `1` | onverwachte fout of een mislukte auditregel in `verify` (daar is niets gecommit); zie stderr | lees stderr; herstel de oorzaak en draai de stap opnieuw |
| `2` | verkeerd gebruik: een argument ontbreekt of `--out` bestaat niet | corrigeer het commando; er is niets gemeten |
| `3` | geweigerd: permit, decoy, invariant of manifest klopt niet; **er is niets veranderd** | lees de reden (stderr en de `refused`-regel in `$AUDIT`); herstel met `baseline --reuse-snapshot` of een nieuwe `quarantine` |
| `4` | na de commit of het zegel: de na-controle, de `VACUUM`, een auditregel of een repetitie-verdict dat niet volledig waar is; het corpus is weg en `$ART` is het terugzetpad | niet opruimen; lees de fout en zet zo nodig terug |

Exit 3 is een weigering vóór de eerste schrijfactie. Exit 4 is nooit "probeer nog eens": de store is dan al gewijzigd of het artefact is al verzegeld.

## Terugzetten

`restore` leest `--quarantine` als het pad van het bestand `quarantine.json`, niet als de map. Het artefact wordt naast dat bestand gelezen (`document_path.parent / document["artifact"]`) en het manifest in de permit-map twee niveaus erboven (`audit.prepare(state_dir, document_path.parent.parent)`), zie `scripts/lib/intelligence_corpus_quarantine/store.py:241-246`. `verify`, `rehearse` en `purge` nemen de map `$ART`; `restore` neemt het bestand. Een map geeft `[Errno 21] Is a directory` en er wordt niets teruggezet.

`restore` zet in één transactie terug, herberekent de digest en doet een `MATCH`-probe vóór de commit; bij elke afwijking `ROLLBACK` en exit 3.

Een restore draait op een latere dag dan de quarantaine. Herbereken `Q` dus niet uit de datum van vandaag, maar zet het op de bestaande map. De volledige regel met absolute paden staat in `$Q/LEESMIJ.md`.

```bash
Q=~/.vnx-data/_quarantine/intelligence-corpus-<datum van de quarantaine>   # de map die bestaat, bijvoorbeeld intelligence-corpus-2026-11-11
python3 scripts/vnx_intelligence_corpus_quarantine.py restore --db "$DB" --quarantine "$Q/artifact/quarantine.json" --state-dir ~/.vnx-data/vnx-dev/state
```

Dit is dezelfde vorm als in `$Q/LEESMIJ.md` (`restore --db <DB> --quarantine <Q>/artifact/quarantine.json --state-dir <state>`).

Een niet-lege doeltabel weigert zonder `--allow-existing`; met die vlag weigert een rowid-botsing, zonder partiële insert. `restore` zet `session_analytics` niet terug: de purge heeft die tabel niet aangeraakt en terugzetten zou rijen overschrijven die na de baseline zijn geschreven.

**Referentie, gemeten op 11-11 (22:02 tot 22:04Z) op een kopie van de live store, code van `main` op `903e3779`:** `restore --db <kopie> --quarantine <Q>/artifact/quarantine.json --state-dir <eigen map>` gaf exit 0 en `{"match_probe": 59, "rows": {"code_snippets": 192495, "snippet_metadata": 192495}}`; de kopie ging van `0|0` naar `192495|192495`. Met de map `<Q>/artifact` als `--quarantine` faalde dezelfde opdracht met `[Errno 21] Is a directory`.

## Wat er daarna waar blijft

- **Het artefact wordt nooit door code verwijderd.** Geen codepad en geen glob ruimt `$ART` op (besluit 22). De nachtelijke rotatie ruimt de vier grote `copy2`-back-ups binnen drie nachten op; het artefact blijft. Bewaar het tot je zeker weet dat je het niet meer nodig hebt.
- **`scripts/check_intelligence_health.py` blijft op exit 11.** Dat is verwacht en wordt niet gerepareerd (besluit 10): bij 0 patronen is `unhealthy` de waarheid (`scripts/check_intelligence_health.py:289`) en de code is `EXIT_HEALTH = 11` (`scripts/check_intelligence_health.py:26`). De check aan een quarantainemanifest koppelen zou een tweede bron van waarheid geven, en er is geen scheduler die hem draait.
- **Het corpus kan opnieuw groeien.** `VNX_DAILY_INTEL_REFRESH` staat default op `1` (`scripts/intelligence_daemon.py:314`); `_refresh_quality_intelligence` (`scripts/lib/intelligence_hygiene.py:70`) kan de quality-scanner en de snippet-extractor opnieuw laten lopen. Er is vandaag geen geplande run (besluit 11). Een handmatige daemon-run vult het corpus weer; dat is geen fout, en een tweede quarantaine is een nieuwe map.

## Done measurement

`$DB`, `$Q`, `$ART` en `$AUDIT` zoals hierboven.

**Clausule 1 - back-up plus nulmeting.**

```bash
python3 -c "import json,sys; m=json.load(open(sys.argv[1])); print(sorted(m), sorted(m['invariants']['session_analytics']), m['corpus']['languages'])" "$Q/manifest.json"
shasum -a 256 "$SNAP"
```

Klaar als: exit 0; het manifest heeft `db`, `snapshot`, `tables`, `corpus` en `invariants.session_analytics` (met `columns` van 40 namen en `digest_sha256`); `snapshot.integrity_check == "ok"`; `snapshot.bytes > 0`; en `shasum -a 256` geeft dezelfde hash als `snapshot.sha256`.

**Clausule 2 - verplaatst, verwijderd in één transactie, checkpoint en `VACUUM` gedraaid.**

```bash
sqlite3 -readonly "$DB" "SELECT (SELECT COUNT(*) FROM code_snippets), (SELECT COUNT(*) FROM snippet_metadata), (SELECT freelist_count FROM pragma_freelist_count());"
sqlite3 -readonly "$ART"/quality_intelligence-corpus-*.db "SELECT (SELECT COUNT(*) FROM corpus_code_snippets), (SELECT COUNT(*) FROM corpus_snippet_metadata);"
```

Klaar als: `quarantine` exit 0 en `quarantine.json` met `track`, `content_digest` per tabel, `source_ddl_sha256` en `restore_proof.ok: true`; `verify` exit 0 met `rows_equal`, `digests_equal`, `ddl_equal`, `session_equal`, `snapshot_ok` en `seal_ok` alle waar; `rehearse` exit 0 met `rows_equal`, `rowids_equal`, `digests_equal`, `fts_match_ok`, `session_equal`, `purge_empty`, `live_unchanged` waar en `orphans: 0`; `purge` exit 0 en de telling geeft `0|0|0`; de auditregels `quarantined`, `verified`, `deleted` en `vacuumed` staan in `$AUDIT` met dezelfde `permit_sha256`; het artefact bevat nog 192.495 rijen in `corpus_code_snippets` én 192.495 in `corpus_snippet_metadata`.

**Clausule 3 - `session_analytics` gelijk, `gather` rc=0 met `pattern_count=0`, monitor en dashboard falen niet hard.**

```bash
python3 scripts/gather_intelligence.py gather "corpus quarantaine" T0 ; echo "rc=$?"
python3 -c "import sys,sqlite3,pathlib; sys.path[:0]=['scripts','scripts/lib']; from intelligence_daemon_monitor import validate_monitor_schema_compatibility, _collect_pattern_metrics; c=sqlite3.connect('file:'+str(pathlib.Path.home()/'.vnx-data/vnx-dev/state/quality_intelligence.db')+'?mode=ro', uri=True); validate_monitor_schema_compatibility(c); print(_collect_pattern_metrics(c))"
python3 -c "import sys,pathlib; sys.path[:0]=['scripts','scripts/lib']; from quality_dashboard_integration import QualityDashboardIntegrator; q=QualityDashboardIntegrator(pathlib.Path.home()/'.vnx-data/vnx-dev/state/quality_intelligence.db'); q.connect(); print(q.get_snippet_summary()); q.close()"
grep '"stage":"verified"' "$AUDIT" | grep '"status":"ok"' | tail -1
```

Klaar als: het eerste commando `rc=0` geeft en in de JSON `"pattern_count": 0` staat; het tweede geen exceptie geeft en `patterns_available` op 0 staat; het derde nullen geeft en geen exceptie; en de laatste `verified`-regel met `"status":"ok"` (de bindende, van vóór de purge) `session_equal` toont met de gemeten sessieregels. De verwachte exit 11 van `scripts/check_intelligence_health.py` is geen fout.

**Clausule 4 - de winst in MB met `dbstat` voor en na.**

```bash
sqlite3 -readonly "$DB" "SELECT ROUND(SUM(pgsize)/1000000.0,2) FROM dbstat WHERE name LIKE 'code_snippets%' OR name LIKE 'idx_snippet%' OR name = 'snippet_metadata';"
ls -l "$DB"
python3 -c "import json,os,sys; m=json.load(open(sys.argv[1])); print(m['corpus']['dbstat_bytes'], os.path.getsize(sys.argv[2]))" "$Q/manifest.json" "$DB"
```

Klaar als: de corpus-familie in `dbstat` na de purge op of onder 2 MB staat, de bestandsgrootte meetbaar is gedaald en de vergelijking met `manifest.corpus.dbstat_bytes` de winst in bytes toont. Referentie, gemeten op 11-11 op een kopie van de live store: bestand 36,2 MB, corpus-familie 0,04 MB.
