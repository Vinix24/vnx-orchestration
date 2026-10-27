- Fleet pin audit (track `vnx-fleet-pin-audit`, PR-7): the audit now reports the
  store as well as the pins. A release dir whose VERSION disagrees with its name,
  or whose write bit is still set, is a finding (exit 1); a release dir with no
  readable VERSION is a WARN. A project pin more than
  `VNX_FLEET_PIN_MAX_LAG_RELEASES` releases behind the store's current is now a
  finding instead of a WARN (default 3, read through the config registry); an
  unknown `behind` count always stays a WARN. `--write-state` writes the
  `fleet_pin_audit` health beacon (daily) and refuses to run without
  `--scan-root`, so the beacon can never call a partial fleet healthy. A
  machine-wide launchd template runs it over `$HOME/Development` every six hours;
  loading it is the operator's act.
