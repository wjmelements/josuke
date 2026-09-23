import pathlib
import tempfile

from .proc import run


class SourceTrees:
    """Detached git worktrees, one per commit, each with a completed `forge build`."""

    def __init__(self, root: pathlib.Path):
        self.root = root
        self._tmp = tempfile.TemporaryDirectory(prefix="josuke-")
        self._trees: dict[str, pathlib.Path] = {}

    def get(self, commit: str) -> pathlib.Path:
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
