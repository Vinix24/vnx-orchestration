# codex_gate — Headless Gate Report

**PR**: 2089
**Branch**: dispatch/20261017-cr-e-export
**Gate**: codex_gate
**Generated**: 2026-10-17T18:17:54Z

---

## Findings

Blocking count: 1
Advisory count: 3

### Blocking

- [BLOCKING] The exclusion `skip_parts = {"__pycache__", ".pytest_cache"}` skips tracked files that the exporter still publishes. Reproduced: committing hooks/__pycache__/identity.txt containing /Users/someone/project produces a successful export with that forbidden content intact. Scan every exported blob, or reject excluded artifacts before publication. (original severity: error)

### Advisory

- `with open(path, "wb") as handle:` overwrites distinct source blobs when their filenames collide on a case-insensitive filesystem. Reproduced on macOS: skills/Example.md and skills/example.md contain different source bytes, but the successful export commits the lowercase file's content at both paths. Preserve source blob identities directly or refuse filesystem collisions. (severity: warning)
- `return tuple(int(n) for n in found.groups()) if found else (0, 0, 0)` discards prerelease identifiers despite accepting them in tags. Reproduced: exporting 0.2.0 after releasing 0.2.0-rc.1 fails because both compare as (0, 2, 0). Compare full semantic-version precedence. (severity: warning)
- `old = open(market_path, encoding="utf-8").read()` normalizes CRLF before merging, so updating one plugin rewrites unrelated marketplace entries' bytes. The README helper has the same issue. Reproduced: both exported files lost every original CRLF. Preserve existing line endings when reading and writing. (severity: warning)
