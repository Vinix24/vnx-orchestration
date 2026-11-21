- Merge gate (config-een-oppervlak N5): `VNX_MERGE_OVERRIDE_REASON` and `VNX_CI_WORKFLOW_NAME` are deleted.
  The override reason comes only from `--override-reason` on `pr_merge.py` and `merge_preflight_ci_check`; the CI
  workflow is the argument, else `ci_workflow` in main's `branch_protection.yaml`, else `VNX CI`.
