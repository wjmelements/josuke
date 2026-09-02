from pathlib import Path
from subprocess import run
from json import loads

def get_forge_config(root: Path) -> dict:
    result = run(
        ["forge", "config", "--json"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return loads(result.stdout)


