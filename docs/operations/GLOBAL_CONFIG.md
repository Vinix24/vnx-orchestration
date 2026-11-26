# Global configuration writes and crash recovery

The global layer is `~/.vnx/config.json`; `VNX_GLOBAL_CONFIG_FILE` relocates it with
`config_audit.ndjson` and `config.json.lock`. This implements
[ADR-093 decision 3](../governance/decisions/ADR-093-config-storeless-reads-recovery-and-named-exceptions.md).

Each set/unset records a UUID4 `txn` and the full document's `new_sha256`, replaces
the document containing `last_txn` atomically, then appends `commit`. Recovery and
all write steps hold the same lock. The next write commits an unfinished transaction
whose id and hash match the file, or aborts it when the preceding committed id
remains (absent for the first transaction). Completed recovery adds nothing on rerun.

A different id or a matching id with a changed hash refuses every global write,
naming the file, transaction and audit line. Reads return the file's value and warn
once per read without recovering. Malformed/unreadable audit evidence also blocks
writes and warns on reads. Legacy files and logs without transaction fields work.
PR #3074 provides set/unset, but no `vnx config` reconciliation command; operator
reconciliation remains necessary for a hand-edited file with an unfinished transaction.
