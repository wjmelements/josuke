"""Timestamped timing lines on stderr when `JOSUKE_TRACE` is set, to see where a run spends its time."""

import sys
import time
from contextlib import contextmanager
from datetime import datetime
from os import environ

_BRIEF = 100  # longest argument summary shown; initcode and calldata run to kilobytes


def enabled() -> bool:
    return bool(environ.get("JOSUKE_TRACE"))


def brief(value) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= _BRIEF else f"{text[:_BRIEF]}… ({len(text)} chars)"


def log(message: str) -> None:
    if enabled():
        print(f"{datetime.now():%H:%M:%S.%f}"[:-3] + f" trace {message}", file=sys.stderr, flush=True)


@contextmanager
def span(label: str):
    """Log `label` on entry and again on exit with the elapsed time."""
    if not enabled():
        yield
        return
    log(f"→ {label}")
    start = time.monotonic()
    try:
        yield
    finally:
        log(f"← {label} ({(time.monotonic() - start) * 1000:.0f} ms)")
