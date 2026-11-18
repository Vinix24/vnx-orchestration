- Governance (ADR-090, track agnostisch-standaard): the account directories of the permission readers come from `~/.claude`,
  `CLAUDE_CONFIG_DIR` and a global registry key `VNX_ACCOUNT_DIRS` (`[{map, slot}]`, slot `vnx` or `sm`), mapped onto the closed
  slots of `permission_event/1`; the evidence path for `applied` follows the same mapping. Succeeds ADR-075 for the account source
  and ADR-086 decision 2 for the `user` paths by cross-ref. Decision record only; the code follows in AG-3.
