## Verification
Red run, on the PR head 94f7191f before regeneration:
`python3 -m pytest tests/test_dispatch_envelope_characterization.py tests/test_report_parser_verification_heading.py -q`
-> 1 failed, 68 passed. FAILED TestLayer2Census::test_census_matches_fixture: "census mismatch ... +1 tests/test_report_parser_verification_heading.py:25 [direct-call] envelope_govern_support._verification_from_report". Behavioural failure (census diff), no missing symbol.

Green run, after `python3 tests/dispatch_envelope_census_scanner.py` (242 couplings):
same command -> 69 passed in 3.96s.

Sibling grep: the scanner regenerates the whole fixture, so no other test file is missing from it.
