- Launchd reload (D3-6, track ci-profile-a-xdist): `reload_plist.sh` no longer tests for its placeholders through `printf | grep -q`.
  Under `pipefail` that pipeline ends on 141 when grep exits at the first match while printf is still writing, and on a loaded Linux runner the project-id substitution was skipped (measured 14 of 6000 runs under CPU load, 0 unloaded).
  The three checks now match in the shell, so the two reload tests that failed only under the xdist shape stop flaking.
