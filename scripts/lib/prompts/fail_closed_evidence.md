## Fail-closed on evidence

Code that reads evidence (verdicts, receipts, gate results, CI job and check data, exit codes, test counts, timestamps, log lines) decides only on positive evidence.

- Missing, `None`, malformed, an unknown enum value or an empty read is never a pass, never a success and never a cause to exclude, skip or set aside. The result stays blocking/dirty and names why.
- Match the positive value exactly (`== "failure"`, an integer that is not 0). Never "anything that is not the good value" and never `x != 0` on something that can be `None`.
- A null guard such as `(value or {}).get(...)` is only right where the default IS the fail-closed answer, or in display and formatting code. When that is not obvious, write the reason at that line.
- When you fix a fail-open path a reviewer named, grep the same function and its siblings for the same pattern and fix them in the same round. Name each one in the report.
- Tests: every branch that clears, passes or excludes gets one case each with the field missing, `None`, malformed and an unknown value, all expected to stay blocking.
