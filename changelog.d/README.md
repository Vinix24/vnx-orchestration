# changelog.d: one CHANGELOG fragment per PR

A PR never edits `CHANGELOG.md`. It adds its entry as a file in this directory, and the release PR folds every fragment into `CHANGELOG.md` before the tag. A new file per PR never conflicts with another PR's new file. Two PRs that both inserted a bullet under `## [Unreleased]` did (OI-2118).

## The fragment

- **Name.** `changelog.d/<dispatch-id>.md` for a dispatch, `changelog.d/<branch-slug>.md` for a PR without one. One level deep. The part before `.md` matches `[A-Za-z0-9][A-Za-z0-9._-]*`. `README.md` is this file and is never a fragment.
- **Content.** A bullet block: UTF-8 with LF line endings, ending with a newline. The first non-blank line starts with `- `. Every other non-blank line starts with `- ` (another bullet) or with at least two spaces (a continuation or a sub-bullet). No line starts with `#`.

```
- Review gates (OI-1234): what changed, and what it means for the reader.
  A continuation line is indented by two spaces.
  - A sub-bullet too.
```

The `Changelog fragments` step of the required `Lint Patterns` job refuses a pull request that changes the `## [Unreleased]` section of `CHANGELOG.md` (it adds, removes, changes or reorders a line there; blank lines do not count), a `CHANGELOG.md` with a `## [...]` heading more than once, an invalid or symlinked fragment, a file here whose name is not a fragment name, or the removal of this README. A PR that changes only fragments runs the light CI profile.

The bullets that were under `## [Unreleased]` before this rule stay there and are folded in first at the next release. `scripts/ci/changelog_legacy_prs.txt` lists the PRs that were open with such a bullet, with a hash of their lines: such a PR passes only while its added lines are unchanged, and it removes, changes or reorders none of the others. The first release PR after the rule deletes that file.

## The release

On the release branch, cut from an up-to-date `origin/main`, before the release PR opens:

1. Set `VERSION` to `X.Y.Z`.
2. `python3 scripts/release/assemble_changelog.py --version X.Y.Z --check` prints the section it would write and the fragments it would delete. It changes nothing.
3. `python3 scripts/release/assemble_changelog.py --version X.Y.Z` writes it. A new `## [X.Y.Z] - <date>` heading lands directly under `## [Unreleased]`, which stays and is left empty. Under it come the bullets that were already under `## [Unreleased]`, then every fragment in the order it landed on the first-parent history of `main`. The fragments are deleted. `--date YYYY-MM-DD` overrides today. `--rev REV` overrides `origin/main` and must be an ancestor of `HEAD`. Every fragment in the tree must be in `--rev` with the same bytes, and every line under `## [Unreleased]` must be a bullet, a continuation (two or more spaces) or blank; anything else is refused before a byte is written. One run at a time: a second run on the same working tree refuses at once and names its lock, `vnx-assemble-changelog.lock` in the git dir.
4. Write the intro paragraph under the new heading by hand: prose only, before the first bullet. Do not edit the bullets. A wrong bullet is fixed in its fragment, in an earlier PR.
5. Commit `CHANGELOG.md`, `changelog.d/` and `VERSION`, and open the release PR. The operator tags after it merges.

The CI check recomputes the release PR from its merge base with the same fold. It fails when `CHANGELOG.md` differs beyond the intro, when a consumed fragment is left in place, when a new fragment is added, or when the new heading's version is not `VERSION`. If `main` moves before the release PR merges, cut the branch again from the new `main` and rerun.

**An interrupted run.** A write or a delete that fails puts the original files back itself. A run that was killed (power loss, SIGKILL) leaves `CHANGELOG.md` and `changelog.d/` dirty, and the next run refuses. Restore with

```
git checkout -- CHANGELOG.md changelog.d
```

and run again. The output is the same, because the assembler commits nothing and reads only the committed tree. There is no partial-rerun mode.
