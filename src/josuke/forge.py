import functools
from pathlib import Path
from subprocess import run
from json import loads


# Memoised per checkout: a run never edits foundry.toml, and each recorded commit
# is read from a worktree of its own.
@functools.cache
def get_forge_config(root: Path) -> dict:
    result = run(
        ["forge", "config", "--json"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return loads(result.stdout)
