- Governance (ADR-088, track pr-grootte-afgedwongen): the merge door gets a `pr_size` preflight after `ci` and before `review`,
  with `VNX_PR_SIZE_ENFORCE` (`off`, `warn`, `refuse`), a security limit of 300 for paths taken from code, HOLD down from 600 to 450,
  overrides that may only tighten, and an exception per PR recorded as a `grant` on the answer line of a `pr:<n>` inbox point.
  Amends ADR-076 decisions 4 and 6 by cross-ref. Decision record only; the code follows in PG-0 to PG-4.
