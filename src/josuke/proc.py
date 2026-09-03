import pathlib
import subprocess

import click


def run(cmd: list, root: pathlib.Path | None = None, stdin: str = None) -> str:
    """Run `cmd` (optionally in `root`), returning stdout; raise a ClickException
    on a missing binary or a non-zero exit."""
    try:
        return subprocess.run(
            cmd, cwd=root, input=stdin, capture_output=True, text=True, check=True
        ).stdout
    except FileNotFoundError:
        raise click.ClickException(f"{cmd[0]}: command not found")
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"{' '.join(cmd[:3])} failed:\n{e.stderr.strip()}")
