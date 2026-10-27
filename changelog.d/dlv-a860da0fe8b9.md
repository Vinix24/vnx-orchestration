- Merge gate (`verify_pr`, track `slop-refactor-panel-review`): the base-branch
  `.vnx-attest/allowed_signers` copy now stays alive through the evidence-bound
  phase instead of being deleted at the end of the provenance phase. A single
  context manager materialises the file once and unlinks it on exit — normal
  return, early return and exception alike — so `required` mode stops refusing
  a valid evidence record as an "invalid signature" and `advisory` mode stops
  reporting false signature diagnostics for the base-branch path.
