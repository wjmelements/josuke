import pathlib
import subprocess

import click

from .trace import span


def run(cmd: list, root: pathlib.Path | None = None, stdin: str = None, stdin_fd: int | None = None) -> str:
    """Run `cmd` (optionally in `root`), returning stdout; raise a ClickException
    on a missing binary or a non-zero exit. `stdin` is text to feed it;
    `stdin_fd` is an open file descriptor to use as its stdin instead."""
    try:
        with span(f"run {' '.join(cmd[:4])}"):
            return subprocess.run(
                cmd, cwd=root, input=stdin, stdin=stdin_fd, capture_output=True, text=True, check=True
            ).stdout
    except FileNotFoundError:
        raise click.ClickException(f"{cmd[0]}: command not found")
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"{' '.join(cmd[:3])} failed:\n{e.stderr.strip()}")
