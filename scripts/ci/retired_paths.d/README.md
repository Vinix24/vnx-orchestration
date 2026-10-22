# Retired paths, symbols and env vars

Checked by `scripts/ci/check_retired_paths.py`.

Three kinds of line (`#` starts a comment):

```
<path>             the file is gone and nothing calls it in an executable way
<path>::<symbol>   the module stays, the function or class is gone from it
                   and nothing imports it
env:<NAME>         nothing reads the env var any more
```

A line stays when its cluster has merged: the list is the memory of what was
removed, so it cannot quietly come back.

## One fragment per cluster

Every cluster adds its own `*.txt` fragment here, named after its dispatch
(`dlv-<id>.txt`), instead of appending to a shared file. Clusters can then
merge in any order without conflicting on this directory. The check reads
every `*.txt` fragment in sorted order as one list; a line that appears in two
fragments is an error that names both files.

Group the lines in the fragment per cluster, with the dispatch and the source
of the decision.
