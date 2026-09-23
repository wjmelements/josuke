"""SourceTrees against a real git repository, with `forge build` stubbed."""

import shutil

import pytest

from josuke import worktree
from josuke.proc import run

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="requires git")


def _git(root, *args) -> str:
    return run(["git", *args], root).strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A two-commit repo; returns (first commit, HEAD, the roots `forge build` ran in)."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "A.sol").write_text("contract A { function a() external {} }\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "one")
    first = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "src" / "A.sol").write_text("contract A { function a() external {} function b() external {} }\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "two")
    head = _git(tmp_path, "rev-parse", "HEAD")

    builds = []
    real_run = worktree.run
    monkeypatch.setattr(
        worktree,
        "run",
        lambda cmd, root=None, stdin=None: builds.append(str(root)) or ""
        if cmd[:2] == ["forge", "build"]
        else real_run(cmd, root, stdin),
    )
    return first, head, builds


def test_source_trees_checks_out_each_commit_and_cleans_up(tmp_path, repo):
    first, _, builds = repo

    with worktree.SourceTrees(tmp_path) as trees:
        tree = trees.get(first)
        assert trees.get(first) is tree  # cached
        assert (tree / "src" / "A.sol").read_text().count("function") == 1
        assert builds == [str(tree)]
        parent = tree.parent

    assert not parent.exists()
    assert str(tree) not in _git(tmp_path, "worktree", "list")


def test_source_trees_serves_clean_head_from_root(tmp_path, repo):
    _, head, builds = repo

    with worktree.SourceTrees(tmp_path) as trees:
        assert trees.get(head) == tmp_path
        assert trees.get(head) == tmp_path
        assert builds == [str(tmp_path)]  # built once

    assert tmp_path.exists()
    assert _git(tmp_path, "worktree", "list").count("\n") == 0  # only the main worktree


def test_source_trees_checks_out_head_when_root_is_dirty(tmp_path, repo):
    _, head, _ = repo
    (tmp_path / "src" / "B.sol").write_text("contract B {}\n")  # untracked

    with worktree.SourceTrees(tmp_path) as trees:
        tree = trees.get(head)
        assert tree != tmp_path
        assert not (tree / "src" / "B.sol").exists()


def test_source_trees_checks_out_head_when_a_submodule_is_uninitialized(tmp_path, repo, monkeypatch):
    # every git call here, including SourceTrees' own, may clone a local submodule
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
    sub = tmp_path.parent / (tmp_path.name + "-sub")
    sub.mkdir()
    _git(sub, "init", "-q")
    _git(sub, "-c", "user.email=t@t.t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "s")
    _git(tmp_path, "submodule", "add", "-q", str(sub), "lib/sub")
    _git(tmp_path, "commit", "-qm", "sub")
    head = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "submodule", "deinit", "-q", "-f", "lib/sub")
    assert _git(tmp_path, "status", "--porcelain") == ""  # invisible to plain `git status`

    with worktree.SourceTrees(tmp_path) as trees:
        tree = trees.get(head)
        assert tree != tmp_path
        assert (tree / "lib" / "sub" / ".git").exists()  # the worktree initializes it
