"""SourceTrees against a real git repository, with `forge build` stubbed."""

import shutil
import subprocess

import pytest

from josuke import worktree


@pytest.mark.skipif(shutil.which("git") is None, reason="requires git")
def test_source_trees_checks_out_each_commit_and_cleans_up(tmp_path, monkeypatch):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@t.t")
    git("config", "user.name", "t")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "A.sol").write_text("contract A { function a() external {} }\n")
    git("add", "-A")
    git("commit", "-qm", "one")
    first = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    (tmp_path / "src" / "A.sol").write_text("contract A { function a() external {} function b() external {} }\n")
    git("add", "-A")
    git("commit", "-qm", "two")

    builds = []
    real_run = worktree.run
    monkeypatch.setattr(
        worktree,
        "run",
        lambda cmd, root=None, stdin=None: builds.append(str(root)) or ""
        if cmd[:2] == ["forge", "build"]
        else real_run(cmd, root, stdin),
    )

    with worktree.SourceTrees(tmp_path) as trees:
        tree = trees.get(first)
        assert trees.get(first) is tree  # cached
        assert (tree / "src" / "A.sol").read_text().count("function") == 1
        assert builds == [str(tree)]
        parent = tree.parent

    assert not parent.exists()
    listed = subprocess.run(
        ["git", "worktree", "list"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout
    assert str(tree) not in listed
