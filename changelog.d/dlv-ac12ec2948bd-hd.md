- Provenance enforcement: CI, the git-hook installer and commit-msg now follow the shared config chain, retain enforcement on config errors, and resolve embedded installations correctly. Validator errors no longer count as valid commits.
  GitHub PR validation selects the event's exact head SHA and refuses missing or invalid head evidence.
