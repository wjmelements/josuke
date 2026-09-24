"""Tests for josuke.signer.

The prompt and `cast` are monkeypatched out except in the end-to-end test,
which unlocks a throwaway keystore with the real `cast`.
"""

import os
import shutil

import click
import pytest

from josuke import signer

ADDRESS = "0x471E88Ac0d6501018439571765289f64B06bDd30"


@pytest.fixture
def keystore_env(monkeypatch):
    monkeypatch.setenv("ETH_KEYSTORE", "/keystores/k")
    monkeypatch.delenv("ETH_KEYSTORE_ACCOUNT", raising=False)
    monkeypatch.delenv("ETH_PASSWORD", raising=False)
    monkeypatch.setattr(signer.sys.stdin, "isatty", lambda: True)


def _prompts(monkeypatch, *passwords):
    """Answer successive prompts with `passwords`; returns the list of prompts shown."""
    shown, answers = [], iter(passwords)
    monkeypatch.setattr(signer.click, "prompt", lambda text, **k: shown.append(text) or next(answers))
    return shown


def _cast_accepting(monkeypatch, good):
    """Fake `cast wallet address` that accepts `good`; records each password it read."""
    seen = []

    def fake_run(cmd, root=None, stdin=None, stdin_fd=None):
        password = os.read(stdin_fd, 1024).decode()
        seen.append(password)
        if password != good:
            raise click.ClickException(f"cast wallet address failed:\nError: Failed to decrypt keystore \"k\"")
        return ADDRESS + "\n"

    monkeypatch.setattr(signer, "run", fake_run)
    return seen


# -- keystore_prompts -----------------------------------------------------------


@pytest.mark.parametrize(
    "env, prompts",
    [
        ({}, False),
        ({"ETH_KEYSTORE": "/k"}, True),
        ({"ETH_KEYSTORE_ACCOUNT": "deployer"}, True),
        ({"ETH_KEYSTORE": "/k", "ETH_PASSWORD": "/pw"}, False),
    ],
)
def test_keystore_prompts(monkeypatch, env, prompts):
    for name in ("ETH_KEYSTORE", "ETH_KEYSTORE_ACCOUNT", "ETH_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert signer.keystore_prompts() is prompts


# -- cast_password ---------------------------------------------------------------


def test_cast_password_outside_session_adds_nothing(keystore_env):
    assert signer.cast_password() == ([], None)


def test_cast_password_without_keystore_never_prompts(monkeypatch, keystore_env):
    monkeypatch.delenv("ETH_KEYSTORE")
    shown = _prompts(monkeypatch)
    with signer.keystore_session():
        assert signer.cast_password() == ([], None)
    assert shown == []


def test_cast_password_prompts_once_and_rewinds(monkeypatch, keystore_env):
    shown = _prompts(monkeypatch, "hunter2")
    _cast_accepting(monkeypatch, "hunter2")

    with signer.keystore_session():
        for _ in range(2):
            args, fd = signer.cast_password()
            assert args == ["--password-file", "/dev/stdin"]
            assert os.read(fd, 1024) == b"hunter2"  # a consumer reads from the start each time
    assert shown == ["Keystore password"]


def test_cast_password_retries_wrong_password(monkeypatch, keystore_env, capsys):
    shown = _prompts(monkeypatch, "wrong", "hunter2")
    seen = _cast_accepting(monkeypatch, "hunter2")

    with signer.keystore_session():
        _, fd = signer.cast_password()
        assert os.read(fd, 1024) == b"hunter2"  # truncated, not "hunter2" over "wrong"
    assert seen == ["wrong", "hunter2"]
    assert len(shown) == 2
    assert "wrong keystore password" in capsys.readouterr().err


def test_cast_password_gives_up_after_attempts(monkeypatch, keystore_env):
    _prompts(monkeypatch, *["wrong"] * signer.ATTEMPTS)
    _cast_accepting(monkeypatch, "hunter2")

    with signer.keystore_session():
        with pytest.raises(click.ClickException, match="wrong keystore password"):
            signer.cast_password()


def test_cast_password_propagates_other_cast_errors(monkeypatch, keystore_env):
    shown = _prompts(monkeypatch, "hunter2", "hunter2")

    def missing(cmd, root=None, stdin=None, stdin_fd=None):
        raise click.ClickException("cast wallet address failed:\nError: keystore not found")

    monkeypatch.setattr(signer, "run", missing)
    with signer.keystore_session():
        with pytest.raises(click.ClickException, match="keystore not found"):
            signer.cast_password()
    assert len(shown) == 1  # not mistaken for a wrong password


def test_cast_password_requires_terminal(monkeypatch, keystore_env):
    monkeypatch.setattr(signer.sys.stdin, "isatty", lambda: False)
    with signer.keystore_session():
        with pytest.raises(click.ClickException, match="set ETH_PASSWORD"):
            signer.cast_password()


# -- keystore_session ------------------------------------------------------------


def test_keystore_session_closes_password_file(monkeypatch, keystore_env):
    _prompts(monkeypatch, "hunter2")
    _cast_accepting(monkeypatch, "hunter2")

    with signer.keystore_session():
        _, fd = signer.cast_password()
    with pytest.raises(OSError):
        os.fstat(fd)
    assert signer.cast_password() == ([], None)


def test_keystore_session_is_not_reentrant():
    with signer.keystore_session():
        with pytest.raises(RuntimeError, match="already active"):
            with signer.keystore_session():
                pass
        assert signer._session is not None  # the outer session is intact
    assert signer._session is None


# -- end to end ------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("cast") is None, reason="requires the `cast` binary")
@pytest.mark.parametrize("password", ["hunter2", ""])
def test_cast_unlocks_keystore_through_anonymous_file(monkeypatch, tmp_path, password):
    signer.run(["cast", "wallet", "new", str(tmp_path), "--unsafe-password", password])
    (keystore,) = tmp_path.iterdir()
    monkeypatch.setenv("ETH_KEYSTORE", str(keystore))
    monkeypatch.delenv("ETH_KEYSTORE_ACCOUNT", raising=False)
    monkeypatch.delenv("ETH_PASSWORD", raising=False)
    monkeypatch.setattr(signer.sys.stdin, "isatty", lambda: True)
    _prompts(monkeypatch, "wrong", password)

    with signer.keystore_session():
        address = None
        for _ in range(2):  # a second read must see the password again
            args, fd = signer.cast_password()
            got = signer.run(["cast", "wallet", "address", *args], stdin_fd=fd).strip()
            assert address in (None, got)
            address = got
    assert address.startswith("0x")
