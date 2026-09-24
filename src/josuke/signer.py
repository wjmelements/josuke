"""Unlock a keystore signer once per run instead of once per `cast send`.

josuke passes `cast` no signer flags, so the signer comes from Foundry's
environment. With a keystore (`ETH_KEYSTORE` / `ETH_KEYSTORE_ACCOUNT`) and no
password file (`ETH_PASSWORD`), every `cast send` would prompt on the terminal.
`keystore_session` prompts once instead, on first use, and checks the password
before anything is sent.

The password never appears in argv (visible to every user via `ps`), in the
environment (`ETH_PASSWORD` is a path; `cast` takes no password from the
environment), or in a named file. It is held in an anonymous file — memory-only
`memfd` on Linux, an unlinked 0600 temp file elsewhere — that each `cast` reads
as `--password-file /dev/stdin`. `cast` requires a regular file there, so a pipe
will not do. Nothing is left behind if josuke is killed: the file has no name,
and the kernel frees it when the last descriptor closes.
"""

import os
import sys
import tempfile
from contextlib import contextmanager
from os import environ

import click

from .proc import run

ATTEMPTS = 3

_session: dict | None = None  # {"fd": int | None}; None outside `keystore_session`


def keystore_prompts() -> bool:
    """`cast` would prompt for a keystore password: a keystore is configured and no password file is."""
    keystore = environ.get("ETH_KEYSTORE") or environ.get("ETH_KEYSTORE_ACCOUNT")
    return bool(keystore) and not environ.get("ETH_PASSWORD")


def _anonymous_file() -> int:
    """A read/write file descriptor for a regular file with no name on any filesystem."""
    if hasattr(os, "memfd_create"):
        return os.memfd_create("josuke-password")  # Linux: never written to disk
    fd, path = tempfile.mkstemp(prefix="josuke-")  # 0600
    os.unlink(path)
    return fd


def _rewound(fd: int) -> int:
    # On macOS `cast`'s /dev/stdin shares this descriptor's offset, so each read
    # leaves it at the end; rewind or the next `cast` reads an empty password.
    os.lseek(fd, 0, os.SEEK_SET)
    return fd


def _unlock() -> int:
    """Prompt for the keystore password until `cast` accepts it; the anonymous file holding it."""
    if not sys.stdin.isatty():
        raise click.ClickException(
            "a keystore signer is configured (ETH_KEYSTORE / ETH_KEYSTORE_ACCOUNT) but there is "
            "no terminal to prompt for its password; set ETH_PASSWORD to a password file"
        )
    fd = _anonymous_file()
    try:
        for _ in range(ATTEMPTS):
            # A blank password is valid: `cast` accepts an empty password file for it.
            password = click.prompt("Keystore password", hide_input=True, default="", show_default=False)
            os.ftruncate(fd, 0)
            os.pwrite(fd, password.encode(), 0)
            del password
            try:
                address = run(
                    ["cast", "wallet", "address", "--password-file", "/dev/stdin"], stdin_fd=_rewound(fd)
                ).strip()
            except click.ClickException as e:
                if "Failed to decrypt keystore" not in e.message:
                    raise
                click.echo("wrong keystore password", err=True)
                continue
            click.echo(f"signing as {address}")
            return fd
        raise click.ClickException(f"wrong keystore password ({ATTEMPTS} attempts)")
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def keystore_session():
    """Scope within which `cast_password` may prompt, at most once; closes the password file on exit.

    Not reentrant: one session holds the password, so a second one is a bug."""
    global _session
    if _session is not None:
        raise RuntimeError("keystore_session is already active")
    _session = {"fd": None}
    try:
        yield
    finally:
        if _session["fd"] is not None:
            os.close(_session["fd"])
        _session = None


def cast_password() -> tuple[list[str], int | None]:
    """(extra `cast` args, stdin fd) that supply the keystore password to a signing `cast` call.

    ([], None) when `cast` needs nothing from us — a non-keystore signer, or a
    password file already in `ETH_PASSWORD`, or outside a `keystore_session`."""
    if _session is None or not keystore_prompts():
        return [], None
    if _session["fd"] is None:
        _session["fd"] = _unlock()
    return ["--password-file", "/dev/stdin"], _rewound(_session["fd"])
