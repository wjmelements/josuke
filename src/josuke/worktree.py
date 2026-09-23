import pathlib
import re
import tempfile

from .proc import run

# A `git submodule status` line for a submodule checked out at its recorded
# commit; "-", "+" or "U" replaces the leading space otherwise.
_SUBMODULE_IN_SYNC = re.compile(r" [0-9a-f]{40}(?:[0-9a-f]{24})? ")


class SourceTrees:
    """Detached git worktrees, one per commit, each with a completed `forge build`."""

    def __init__(self, root: pathlib.Path):
        self.root = root
        self._tmp = tempfile.TemporaryDirectory(prefix="josuke-")
        self._trees: dict[str, pathlib.Path] = {}
        dirty = run(["git", "status", "--porcelain", "--ignore-submodules=none"], root).strip()
        submodules = run(["git", "submodule", "status", "--recursive"], root).splitlines()
        clean = not dirty and all(_SUBMODULE_IN_SYNC.match(line) for line in submodules)
        self._root_commit = run(["git", "rev-parse", "HEAD"], root).strip() if clean else ""
        self._root_built = False

    def get(self, commit: str) -> pathlib.Path:
        if commit == self._root_commit:
            if not self._root_built:
                run(["forge", "build"], self.root)  # a no-op when the caller already built
                self._root_built = True
            return self.root
        if commit not in self._trees:
            tree = pathlib.Path(self._tmp.name) / commit
            run(["git", "worktree", "add", "--detach", str(tree), commit], self.root)
            self._trees[commit] = tree  # recorded before build so a build failure still cleans up
            if (tree / ".gitmodules").exists():
                run(["git", "submodule", "update", "--init", "--recursive"], tree)
            run(["forge", "build"], tree)
        return self._trees[commit]

    def close(self) -> None:
        for tree in self._trees.values():
            run(["git", "worktree", "remove", "--force", str(tree)], self.root)
        self._tmp.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
