"""OI-1846: the post-dispatch salvage pushed from the main checkout, counted a
regenerated FEATURE_PLAN.md as forgotten work, and turned delivered work into a
failure.

Three defects, one shared fixture (a real git repo, a real bare "origin", a real
``git worktree`` laid out like a dispatch worktree, a stub ``gh`` on PATH):

1. push and ``gh`` ran with cwd = the main checkout, so the operator's pre-push
   hook (and venv) ran with them and timed the push out. With ``wt_path`` known
   they must run from the worktree.
2. a tree dirty ONLY through the generated FEATURE_PLAN.md was salvage-committed
   as "forgotten work". The background regen must not write into a dispatch
   worktree (source), and the classifier must not count a generated file as
   substantive (defence).
3. work the worker had already pushed, with a PR, still ended on failure when the
   salvage push stumbled. The outcome must follow the work; the stumble stays loud.

Nothing here starts a worker, touches the real repo, or reaches GitHub: ``gh`` is
a shell stub, receipts are captured, every git repo lives under tmp_path.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pr_enforcement as pe

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_AUTOGEN = "<!-- AUTO-GENERATED — DO NOT EDIT — see scripts/build_feature_plan.py -->"
_GENERATED_PLAN = f"{_AUTOGEN}\n\n# VNX Feature Plan\n\nLast updated: 2026-09-24T10:00:00Z\n"
_REGENERATED_PLAN = f"{_AUTOGEN}\n\n# VNX Feature Plan\n\nLast updated: 2026-09-24T18:00:00Z\n"


# ---------------------------------------------------------------------------
# Fixture: bare origin + main checkout + dispatch-style worktree + gh stub
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=check,
    )


class _Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.bare = root / "origin.git"
        self.main = root / "main-checkout"

    def add_worktree(self, dispatch_id: str) -> Path:
        wt = self.main / ".vnx-data" / "worktrees" / f"dispatch-{dispatch_id}"
        wt.parent.mkdir(parents=True, exist_ok=True)
        _git(self.main, "worktree", "add", "-b", f"dispatch/{dispatch_id}", str(wt), "origin/main")
        return wt

    def commit(self, wt: Path, name: str, content: str) -> str:
        (wt / name).write_text(content)
        _git(wt, "add", name)
        _git(wt, "commit", "-m", f"worker: {name}")
        return _git(wt, "rev-parse", "HEAD").stdout.strip()

    def remote_head(self, branch: str) -> "str | None":
        out = _git(self.main, "ls-remote", "origin", f"refs/heads/{branch}").stdout.split()
        return out[0] if out else None

    def branch_subjects(self, branch: str) -> str:
        return _git(self.main, "log", branch, "--format=%s").stdout


@pytest.fixture()
def repo(tmp_path: Path) -> _Repo:
    r = _Repo(tmp_path)
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(r.bare)],
        check=True, capture_output=True,
    )
    subprocess.run(["git", "clone", str(r.bare), str(r.main)], check=True, capture_output=True)
    _git(r.main, "checkout", "-b", "main", check=False)
    _git(r.main, "config", "user.email", "test@test.local")
    _git(r.main, "config", "user.name", "Test")
    (r.main / "alpha.py").write_text("print('alpha')\n")
    (r.main / "FEATURE_PLAN.md").write_text(_GENERATED_PLAN)
    _git(r.main, "add", "alpha.py", "FEATURE_PLAN.md")
    _git(r.main, "commit", "-m", "initial")
    _git(r.main, "push", "-u", "origin", "main")
    return r


def _install_operator_prepush_hook(repo: _Repo) -> None:
    """The operator's hook: present in the MAIN checkout only.

    ``core.hooksPath`` is relative, so git resolves it against the checkout the
    push runs in. The hook directory is untracked, so a worktree (a checkout of
    tracked files) does not carry it — exactly the sales-copilot shape where the
    operator's working directory brings hooks and a venv the worktree lacks.
    """
    _git(repo.main, "config", "core.hooksPath", ".githooks")
    hook = repo.main / ".githooks" / "pre-push"
    hook.parent.mkdir()
    hook.write_text("#!/bin/sh\necho 'operator pre-push hook: venv missing' >&2\nexit 1\n")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)


def _install_flag_prepush_hook(repo: _Repo, flag: Path) -> None:
    """A hook (shared by every checkout) that refuses pushes while *flag* exists.

    Lets a test let the worker's own push through and then make the SALVAGE push
    fail, with real git on both sides.
    """
    hooks = repo.root / "shared-hooks"
    hooks.mkdir()
    hook = hooks / "pre-push"
    hook.write_text(
        f"#!/bin/sh\nif [ -e '{flag}' ]; then echo 'push refused by test hook' >&2; exit 1; fi\nexit 0\n"
    )
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    _git(repo.main, "config", "core.hooksPath", str(hooks))


class _GhStub:
    """A ``gh`` on PATH. Answers ``pr list`` / ``pr create``, logs every call's cwd."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.bin = tmp_path / "ghbin"
        self.bin.mkdir()
        self.log = tmp_path / "gh-calls.log"
        self.log.write_text("")
        script = self.bin / "gh"
        script.write_text(
            "#!/bin/sh\n"
            'echo "$(pwd -P)|$*" >> "$GH_STUB_LOG"\n'
            'case "$1 $2" in\n'
            '  "pr list") printf "%s" "$GH_STUB_PR_LIST" ;;\n'
            '  "pr create")\n'
            '    if [ -n "$GH_STUB_CREATE_FAIL" ]; then echo "gh: boom" >&2; exit 1; fi\n'
            '    echo "https://github.com/o/r/pull/${GH_STUB_CREATE_NUMBER:-777}" ;;\n'
            "  *) exit 1 ;;\n"
            "esac\n"
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("PATH", f"{self.bin}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("GH_STUB_LOG", str(self.log))
        monkeypatch.setenv("GH_STUB_PR_LIST", "[]")
        self._mp = monkeypatch

    def existing_pr(self, number: int) -> None:
        self._mp.setenv("GH_STUB_PR_LIST", f'[{{"number": {number}, "state": "OPEN"}}]')

    def create_fails(self) -> None:
        self._mp.setenv("GH_STUB_CREATE_FAIL", "1")

    def cwds(self) -> "set[str]":
        return {line.split("|", 1)[0] for line in self.log.read_text().splitlines() if line}


@pytest.fixture()
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _GhStub:
    return _GhStub(tmp_path, monkeypatch)


def _enforce(repo: _Repo, wt: Path, dispatch_id: str, state: str, **overrides):
    """Call the real enforce_pr_exists, capture the receipts it appends."""
    receipts: "list[dict]" = []
    kwargs = dict(
        dispatch_id=dispatch_id,
        branch=f"dispatch/{dispatch_id}",
        worktree_state=state,
        repo_root=repo.main,
        receipts_file="/tmp/does-not-matter.ndjson",
        pr_title="t",
        pr_body="b",
        wt_path=wt,
    )
    kwargs.update(overrides)
    with patch("append_receipt.append_receipt_payload",
               side_effect=lambda payload, **kw: receipts.append(dict(payload))):
        result = pe.enforce_pr_exists(**kwargs)
    return result, receipts


# ---------------------------------------------------------------------------
# Defect 1: push and gh run from the worktree when wt_path is known
# ---------------------------------------------------------------------------


def test_push_succeeds_from_worktree_when_main_checkout_hook_would_fail(repo, gh):
    """The operator's pre-push hook lives in the main checkout only. The push must
    run from the worktree, where it is absent, and go through."""
    _install_operator_prepush_hook(repo)
    wt = repo.add_worktree("d1")
    repo.commit(wt, "beta.py", "print('beta')\n")

    result, receipts = _enforce(repo, wt, "d1", "committed")

    assert result.ok is True, result.reason
    assert result.pushed is True
    assert repo.remote_head("dispatch/d1") == _git(wt, "rev-parse", "HEAD").stdout.strip()
    assert receipts == [], "a delivered dispatch must not get a failure receipt"


def test_gh_runs_from_the_worktree_in_the_normal_path(repo, gh):
    wt = repo.add_worktree("d2")
    repo.commit(wt, "beta.py", "print('beta')\n")

    result, _ = _enforce(repo, wt, "d2", "committed")

    assert result.ok is True, result.reason
    assert result.pr_number == 777 and result.created is True
    assert gh.cwds() == {str(wt.resolve())}


def test_without_wt_path_push_and_gh_still_run_from_repo_root(repo, gh):
    """No wt_path: behaviour is unchanged, git and gh run from repo_root."""
    wt = repo.add_worktree("d3")
    repo.commit(wt, "beta.py", "print('beta')\n")
    seen: "list[Path]" = []
    real_push = pe._push_branch

    def _spy(**kw):
        seen.append(Path(kw.get("cwd") or kw["repo_root"]))
        return real_push(**kw)

    with patch.object(pe, "_push_branch", _spy):
        result, _ = _enforce(repo, wt, "d3", "committed", wt_path=None)

    assert result.ok is True, result.reason
    assert seen == [repo.main]
    assert gh.cwds() == {str(repo.main.resolve())}


def test_without_wt_path_a_dirty_tree_stays_not_applicable(repo, gh):
    wt = repo.add_worktree("d4")
    (wt / "alpha.py").write_text("print('edited')\n")

    result, receipts = _enforce(repo, wt, "d4", "dirty", wt_path=None)

    assert result.applicable is False and result.ok is True
    assert receipts == []
    assert repo.remote_head("dispatch/d4") is None


# ---------------------------------------------------------------------------
# Defect 2: a regenerated FEATURE_PLAN.md is not forgotten work
# ---------------------------------------------------------------------------


def test_only_generated_feature_plan_dirty_gets_no_salvage_commit(repo, gh):
    wt = repo.add_worktree("d5")
    (wt / "FEATURE_PLAN.md").write_text(_REGENERATED_PLAN)
    head_before = _git(wt, "rev-parse", "HEAD").stdout.strip()

    result, receipts = _enforce(repo, wt, "d5", "dirty")

    assert result.ok is True, result.reason
    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == head_before
    assert "SALVAGED" not in repo.branch_subjects("dispatch/d5")
    assert repo.remote_head("dispatch/d5") is None, "nothing may be pushed for a generated-only tree"
    assert receipts == []


def test_worker_already_pushed_with_pr_then_regen_dirt_is_a_clean_success(repo, gh):
    """The D-64c6d85b shape: the worker pushed and opened PR 234, then the
    background regen dirtied FEATURE_PLAN.md. Delivered work must stay a success."""
    wt = repo.add_worktree("d6")
    repo.commit(wt, "beta.py", "print('beta')\n")
    _git(wt, "push", "-u", "origin", "dispatch/d6")
    gh.existing_pr(234)
    (wt / "FEATURE_PLAN.md").write_text(_REGENERATED_PLAN)

    result, receipts = _enforce(repo, wt, "d6", "dirty")

    assert result.ok is True, result.reason
    assert result.pr_number == 234 and result.created is False
    assert "SALVAGED" not in repo.branch_subjects("dispatch/d6")
    assert receipts == []


def test_generated_dirt_does_not_hide_an_unpushed_worker_commit(repo, gh):
    """The worker committed but never pushed, and the regen dirtied the tree. The
    generated file must not decide the verdict: the worker's commit still gets
    pushed and a PR, and no salvage commit is made."""
    _install_operator_prepush_hook(repo)
    wt = repo.add_worktree("d7")
    worker_head = repo.commit(wt, "beta.py", "print('beta')\n")
    (wt / "FEATURE_PLAN.md").write_text(_REGENERATED_PLAN)

    result, receipts = _enforce(repo, wt, "d7", "dirty")

    assert result.ok is True, result.reason
    assert result.pushed is True and result.pr_number == 777
    assert repo.remote_head("dispatch/d7") == worker_head
    assert "SALVAGED" not in repo.branch_subjects("dispatch/d7")
    assert receipts == []


def test_handwritten_feature_plan_without_the_generator_marker_is_still_real_work(repo, gh):
    wt = repo.add_worktree("d8")
    (wt / "FEATURE_PLAN.md").write_text("# My own plan\n\nPR-1: do the thing\n")

    result, receipts = _enforce(repo, wt, "d8", "dirty")

    assert result.applicable is True and result.ok is False
    assert "SALVAGED" in repo.branch_subjects("dispatch/d8")
    assert receipts and receipts[0]["autopr_kind"] == "dirty_substantive_salvaged"


def test_real_edit_next_to_generated_file_salvages_only_the_real_edit(repo, gh):
    wt = repo.add_worktree("d9")
    (wt / "alpha.py").write_text("print('edited by the worker')\n")
    (wt / "FEATURE_PLAN.md").write_text(_REGENERATED_PLAN)

    result, receipts = _enforce(repo, wt, "d9", "dirty")

    assert result.applicable is True and result.ok is False
    salvaged = _git(wt, "show", "HEAD", "--name-only", "--format=").stdout.split()
    assert salvaged == ["alpha.py"]
    assert receipts[0]["dirty_files"] == ["alpha.py"]


def test_classifier_names_the_generated_paths_it_set_aside(repo):
    wt = repo.add_worktree("d10")
    (wt / "FEATURE_PLAN.md").write_text(_REGENERATED_PLAN)

    classification = pe._classify_dirty_worktree(wt_path=wt)

    assert classification.substantive is False
    assert classification.generated_paths == ("FEATURE_PLAN.md",)
    assert classification.tracked_paths == ()
    assert "FEATURE_PLAN.md" in classification.evidence


def test_untracked_generated_feature_plan_is_kept_out_of_the_salvage(repo, gh):
    """In a repo that neither tracks nor ignores FEATURE_PLAN.md it shows up as
    ``??``. Next to a real edit it must not ride along in the salvage commit."""
    _git(repo.main, "rm", "--cached", "-q", "FEATURE_PLAN.md")
    _git(repo.main, "commit", "-m", "untrack FEATURE_PLAN.md")
    _git(repo.main, "push", "origin", "main")
    wt = repo.add_worktree("d11")
    (wt / "FEATURE_PLAN.md").write_text(_REGENERATED_PLAN)
    (wt / "alpha.py").write_text("print('edited by the worker')\n")

    result, _ = _enforce(repo, wt, "d11", "dirty")

    assert result.applicable is True
    assert _git(wt, "show", "HEAD", "--name-only", "--format=").stdout.split() == ["alpha.py"]


# ---------------------------------------------------------------------------
# Defect 2, at the source: the regen does not write into a dispatch worktree
# ---------------------------------------------------------------------------


@pytest.fixture()
def feature_plan_module(monkeypatch):
    import build_feature_plan as bfp

    monkeypatch.setattr(bfp, "read_register_events", lambda state_dir=None: [])
    monkeypatch.setattr(bfp, "fetch_merged_prs", lambda limit=100: [])
    monkeypatch.setattr(bfp, "fetch_recent_git_merged_prs", lambda days=14: [])
    monkeypatch.setattr(bfp, "load_roadmap", lambda roadmap_path=None: [])
    return bfp


def test_regen_does_not_write_into_a_dispatch_worktree(repo, feature_plan_module):
    wt = repo.add_worktree("d12")
    target = wt / "FEATURE_PLAN.md"
    before = target.read_text()

    feature_plan_module.write_feature_plan(target)

    assert target.read_text() == before
    assert _git(wt, "status", "--porcelain").stdout == ""


def test_regen_still_writes_in_the_main_checkout(repo, feature_plan_module):
    target = repo.main / "FEATURE_PLAN.md"

    content = feature_plan_module.write_feature_plan(target)

    assert content.startswith(_AUTOGEN)
    assert target.read_text() == content
    assert "Last updated" in target.read_text()


def test_regen_dry_run_inside_a_dispatch_worktree_still_returns_the_content(repo, feature_plan_module):
    wt = repo.add_worktree("d13")

    content = feature_plan_module.write_feature_plan(wt / "FEATURE_PLAN.md", dry_run=True)

    assert content.startswith(_AUTOGEN)


# ---------------------------------------------------------------------------
# Defect 3: delivered work with a PR does not end on failure
# ---------------------------------------------------------------------------


def _worker_pushed_with_pr_then_left_a_real_edit(repo, wt, gh, dispatch_id, flag):
    repo.commit(wt, "beta.py", "print('beta')\n")
    _git(wt, "push", "-u", "origin", f"dispatch/{dispatch_id}")
    gh.existing_pr(234)
    (wt / "alpha.py").write_text("print('an edit the worker never committed')\n")
    flag.write_text("refuse")


def test_worker_pushed_with_pr_then_salvage_push_fails_is_not_a_failure(repo, gh):
    flag = repo.root / "refuse-push"
    _install_flag_prepush_hook(repo, flag)
    wt = repo.add_worktree("d14")
    _worker_pushed_with_pr_then_left_a_real_edit(repo, wt, gh, "d14", flag)
    worker_head = repo.remote_head("dispatch/d14")

    result, receipts = _enforce(repo, wt, "d14", "dirty")

    assert result.ok is True, result.reason
    assert result.pr_number == 234
    assert repo.remote_head("dispatch/d14") == worker_head, "the salvage commit never reached origin"
    assert "SALVAGED" in _git(wt, "log", "--format=%s").stdout, "the salvage commit is kept locally"

    # Loud, not silent: a warning result and a receipt that names the reason ...
    assert result.warning and "salvage" in result.warning and "push" in result.warning
    assert len(receipts) == 1
    warning = receipts[0]
    assert warning["event_type"] == "pr_enforcement_warning"
    assert warning["autopr_kind"] == "dirty_substantive_salvage_push_failed_delivery_intact"
    assert "push refused by test hook" in warning["autopr_reason"]
    assert warning["pr_number"] == 234
    # ... that can never be read as a failed completion.
    for forbidden in ("status", "autopr_rejected", "failure_reason"):
        assert forbidden not in warning, forbidden


def test_salvage_push_failure_without_worker_push_is_still_a_failure(repo, gh):
    """The worker committed but never pushed: the salvage push failing leaves
    real work only on this machine. That stays a hard failure."""
    flag = repo.root / "refuse-push"
    _install_flag_prepush_hook(repo, flag)
    flag.write_text("refuse")
    wt = repo.add_worktree("d15")
    repo.commit(wt, "beta.py", "print('beta')\n")
    (wt / "alpha.py").write_text("print('an edit the worker never committed')\n")

    result, receipts = _enforce(repo, wt, "d15", "dirty")

    assert result.ok is False
    assert result.pushed is False
    assert receipts[0]["status"] == "failed"
    assert receipts[0]["autopr_kind"] == "dirty_substantive_unsalvaged"


def test_salvage_push_failure_when_worker_pushed_but_no_pr_can_be_made_is_a_failure(repo, gh):
    flag = repo.root / "refuse-push"
    _install_flag_prepush_hook(repo, flag)
    wt = repo.add_worktree("d16")
    _worker_pushed_with_pr_then_left_a_real_edit(repo, wt, gh, "d16", flag)
    gh._mp.setenv("GH_STUB_PR_LIST", "[]")
    gh.create_fails()

    result, receipts = _enforce(repo, wt, "d16", "dirty")

    assert result.ok is False
    assert receipts[0]["status"] == "failed"
    assert receipts[0]["autopr_kind"] == "dirty_substantive_unsalvaged"


def test_worker_pushed_but_remote_branch_moved_on_is_not_treated_as_delivered(repo, gh):
    """The remote branch does not contain the worker's local HEAD (someone
    force-pushed over it): 'already delivered' cannot be established, so failure."""
    flag = repo.root / "refuse-push"
    _install_flag_prepush_hook(repo, flag)
    wt = repo.add_worktree("d17")
    repo.commit(wt, "beta.py", "print('beta')\n")
    other = repo.root / "other-clone"
    subprocess.run(["git", "clone", str(repo.bare), str(other)], check=True, capture_output=True)
    _git(other, "config", "user.email", "o@test.local")
    _git(other, "config", "user.name", "Other")
    _git(other, "checkout", "-b", "dispatch/d17", "origin/main")
    (other / "gamma.py").write_text("print('gamma')\n")
    _git(other, "add", "gamma.py")
    _git(other, "commit", "-m", "someone else")
    _git(other, "push", "origin", "dispatch/d17")
    gh.existing_pr(234)
    (wt / "alpha.py").write_text("print('edit')\n")
    flag.write_text("refuse")

    result, receipts = _enforce(repo, wt, "d17", "dirty")

    assert result.ok is False
    assert receipts[0]["status"] == "failed"


def test_skip_pr_dispatch_with_delivered_work_and_failed_salvage_push_is_not_a_failure(repo, gh):
    flag = repo.root / "refuse-push"
    _install_flag_prepush_hook(repo, flag)
    wt = repo.add_worktree("d18")
    _worker_pushed_with_pr_then_left_a_real_edit(repo, wt, gh, "d18", flag)

    result, receipts = _enforce(repo, wt, "d18", "dirty", skip_pr=True)

    assert result.ok is True, result.reason
    assert receipts[0]["event_type"] == "pr_enforcement_warning"


# ---------------------------------------------------------------------------
# classify_path: the commit state without the working-tree verdict
# ---------------------------------------------------------------------------


def test_classify_path_can_ignore_the_working_tree(repo):
    import tmux_worktree

    wt = repo.add_worktree("d19")
    repo.commit(wt, "beta.py", "print('beta')\n")
    (wt / "FEATURE_PLAN.md").write_text(_REGENERATED_PLAN)

    assert tmux_worktree.classify_path(wt=wt, branch="dispatch/d19", dispatch_id="d19") == "dirty"
    assert tmux_worktree.classify_path(
        wt=wt, branch="dispatch/d19", dispatch_id="d19", ignore_working_tree=True,
    ) == "committed"
    _git(wt, "push", "-u", "origin", "dispatch/d19")
    assert tmux_worktree.classify_path(
        wt=wt, branch="dispatch/d19", dispatch_id="d19", ignore_working_tree=True,
    ) == "pushed"


# ---------------------------------------------------------------------------
# The warning receipt is accepted by the real ledger and never decides the outcome
# ---------------------------------------------------------------------------


def _through_the_real_ledger(tmp_path: Path, payload: dict) -> dict:
    """Pipe *payload* through the real append_receipt.py CLI (its validator and
    writer) in an isolated environment, and return what landed on the ledger."""
    import json

    data_dir = tmp_path / "ledger-data"
    (data_dir / "state").mkdir(parents=True)
    env = os.environ.copy()
    env.update(
        PROJECT_ROOT=str(tmp_path), VNX_DATA_DIR=str(data_dir),
        VNX_STATE_DIR=str(data_dir / "state"), VNX_HOME=str(_SCRIPTS_DIR.parent),
    )
    ledger = data_dir / "state" / "t0_receipts.ndjson"
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / "append_receipt.py"), "--receipts-file", str(ledger)],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(ledger.read_text().strip().splitlines()[-1])


def _actionable_event_types() -> frozenset:
    from headless_trigger import ReceiptWatcher

    return ReceiptWatcher.ACTIONABLE_EVENTS


def _capture(fn, **kwargs) -> dict:
    captured: "list[dict]" = []
    with patch("append_receipt.append_receipt_payload",
               side_effect=lambda payload, **kw: captured.append(dict(payload))):
        fn(**kwargs)
    assert len(captured) == 1
    return captured[0]


def test_warning_receipt_lands_on_the_ledger_and_never_overrides_the_workers_completion(tmp_path):
    from dispatch_govern import dedup_completion_receipts

    warning = _capture(
        pe._record_delivery_warning_receipt,
        dispatch_id="dW", branch="dispatch/dW", reason="salvage push failed: hook",
        receipts_file="/tmp/unused.ndjson", kind="dirty_substantive_salvage_push_failed_delivery_intact",
        extra_fields={"pr_number": 234},
    )
    stored = _through_the_real_ledger(tmp_path, warning)
    assert stored["event_type"] == "pr_enforcement_warning"
    assert stored["autopr_kind"] == "dirty_substantive_salvage_push_failed_delivery_intact"

    # The watchers that pick a dispatch's outcome first keep only ACTIONABLE
    # event types, then dedup. The warning is not one of them, so it never
    # reaches the dedup and the worker's own completion stands.
    workers_own = {
        "event_type": "subprocess_completion", "dispatch_id": "dW", "status": "success",
        "timestamp": "2026-09-24T17:00:00Z",
    }
    actionable = [r for r in (workers_own, stored) if r["event_type"] in _actionable_event_types()]
    assert actionable == [workers_own]
    assert dedup_completion_receipts(actionable) is workers_own


def test_failure_receipt_still_overrides_the_workers_completion(tmp_path):
    """The contrast that gives the test above its meaning: the corrective
    'failed' receipt is authoritative, the warning is not."""
    from dispatch_govern import dedup_completion_receipts

    failed = _capture(
        pe._record_corrective_receipt,
        dispatch_id="dF", branch="dispatch/dF", reason="push failed",
        receipts_file="/tmp/unused.ndjson", kind="push_failed",
    )
    stored = _through_the_real_ledger(tmp_path, failed)

    workers_own = {
        "event_type": "subprocess_completion", "dispatch_id": "dF", "status": "success",
        "timestamp": "2026-09-24T17:00:00Z",
    }
    actionable = [r for r in (workers_own, stored) if r["event_type"] in _actionable_event_types()]
    assert dedup_completion_receipts(actionable) is stored


def test_the_workers_own_commit_is_measured_against_the_recorded_base_not_origin_main(repo, gh):
    """A consumer whose default branch is not ``origin/main`` still gets the soft
    outcome when the envelope hands over the worktree's base commit."""
    flag = repo.root / "refuse-push"
    _install_flag_prepush_hook(repo, flag)
    wt = repo.add_worktree("d20")
    base = _git(wt, "rev-parse", "HEAD").stdout.strip()
    _worker_pushed_with_pr_then_left_a_real_edit(repo, wt, gh, "d20", flag)
    _git(repo.main, "update-ref", "-d", "refs/remotes/origin/main")

    result, receipts = _enforce(repo, wt, "d20", "dirty", base_sha=base)

    assert result.ok is True, result.reason
    assert receipts[0]["event_type"] == "pr_enforcement_warning"


def test_without_a_base_and_without_origin_main_the_soft_outcome_is_not_established(repo, gh):
    flag = repo.root / "refuse-push"
    _install_flag_prepush_hook(repo, flag)
    wt = repo.add_worktree("d21")
    _worker_pushed_with_pr_then_left_a_real_edit(repo, wt, gh, "d21", flag)
    _git(repo.main, "update-ref", "-d", "refs/remotes/origin/main")

    result, receipts = _enforce(repo, wt, "d21", "dirty")

    assert result.ok is False
    assert receipts[0]["status"] == "failed"
    assert "could not be compared" in receipts[0]["autopr_reason"]
