- Receipt hash chains (OI-3094): explicit ledger directories select their own project's
  `VNX_HASH_CHAIN_REQUIRED` setting. Missing project identity or unreadable project
  configuration emits a warning and excludes project rows while preserving override,
  global config, env and default precedence; receipt appends continue. Operator guidance
  names the current switch, while frozen release history remains unchanged.
