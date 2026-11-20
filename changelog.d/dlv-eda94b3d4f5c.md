- ADR-093 (config-een-oppervlak): records the config contracts ADR-087 does not cover. One reader without a store
  (`enforcement_profile.enforced_storeless`); `data_dir_guard` records in phase 1 and decides only in phase 2 at the
  end of every `autowire` (amends ADR-087 decision 5); `vnx migrate` applies no stored profile row; recovery of
  `~/.vnx/config.json` on a `txn` with a refusal after a hand edit; `VNX_OVERRIDE_PAID_LANE_DAILY_BUDGET` stays a
  boolean bypass, a named exception to `VNX_OVERRIDE_<BARE>`. Docs only; PR-Q3c, PR-Q3d and G1b build against it.
