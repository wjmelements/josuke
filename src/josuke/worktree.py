import pathlib
import re
import tempfile

import click

from .proc import run

# A `git submodule status` line for a submodule checked out at its recorded
# commit; "-", "+" or "U" replaces the leading space otherwise.
_SUBMODULE_IN_SYNC = re.compile(r" [0-9a-f]{40}(?:[0-9a-f]{24})? ")


def _submodule_paths(checkout: pathlib.Path) -> dict[str, str]:
    """name -> path for each submodule `checkout`'s .gitmodules declares."""
    if not (checkout / ".gitmodules").exists():
        return {}
    try:
        out = run(["git", "config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"], checkout)
    except click.ClickException:
        return {}  # git exits 1 when nothing matches
    paths = {}
    for line in out.splitlines():
        key, path = line.split(" ", 1)
        paths[key.removeprefix("submodule.").removesuffix(".path")] = path
    return paths


_FILE_OK = ["-c", "protocol.file.allow=always"]  # git refuses local submodule clones by default


def _clone_submodules(tree: pathlib.Path, root: pathlib.Path) -> None:
    """Check out worktree `tree`'s submodules, recursively, by cloning each from its
    counterpart in `root` instead of from its remote: local clones take no network
    round trips.

    A worktree shares its repository's config, so the local URLs are per-command
    `-c` overrides here; writing them would repoint the user's own checkout."""
    paths = _submodule_paths(tree)
    urls = [arg for name, path in paths.items() for arg in ("-c", f"submodule.{name}.url={root / path}")]
    run(["git", *_FILE_OK, *urls, "submodule", "update", "--init"], tree)
    for path in paths.values():
        _clone_nested(tree / path, root / path)


def _clone_nested(checkout: pathlib.Path, source: pathlib.Path) -> None:
    """`_clone_submodules` below the top level. Each nested submodule is a fresh
    clone whose config belongs to the worktree alone, so its local URLs are written
    there (git ignores `-c` URLs for a submodule not yet active in the repo's config)."""
    paths = _submodule_paths(checkout)
    if not paths:
        return
    run(["git", "submodule", "init"], checkout)
    for name, path in paths.items():
        run(["git", "config", f"submodule.{name}.url", str(source / path)], checkout)
    run(["git", *_FILE_OK, "submodule", "update"], checkout)
    for path in paths.values():
        _clone_nested(checkout / path, source / path)


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
                try:
                    _clone_submodules(tree, self.root)
                except click.ClickException:
                    # A recorded submodule commit the root's copies lack: start over from the remotes.
                    run(["git", "worktree", "remove", "--force", str(tree)], self.root)
                    run(["git", "worktree", "add", "--detach", str(tree), commit], self.root)
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
