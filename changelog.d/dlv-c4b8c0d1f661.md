- Audit chain (receipt-hashketen-aan H1): `audit_chain.py verify` now reads `verified-segmented` (an ADR-029 sealed ledger) as healthy, exit 0.
  A defect prints its real status, the first 20 violations and the total count (exit 1); an unknown status exits 3 and is never healthy; `unchained` stays exit 2.
