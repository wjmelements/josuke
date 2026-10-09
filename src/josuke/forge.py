import functools
from pathlib import Path
from json import loads

from .proc import run


# Memoised per checkout: a run never edits foundry.toml, and each recorded commit
# is read from a worktree of its own.
@functools.cache
def get_forge_config(root: Path) -> dict:
    return loads(run(["forge", "config", "--json"], root))
