"""Tests for josuke.delegate."""

import pytest

from josuke.delegate import ContractSource, Delegate, source_map

# An arbitrary but valid-looking deployment address.
ADDRESS = "0x1A4E1a4e1A4E1a4e1a4E1a4e1A4e1A4E1a4E1A4e"


@pytest.fixture(autouse=True)
def _isolate_source_map():
    """delegate.source_map is module-global; keep tests from leaking into each other."""
    saved = dict(source_map)
    source_map.clear()
    yield
    source_map.clear()
    source_map.update(saved)


def test_delegate_registers_in_source_map():
    source = ContractSource("src/NoArgs.sol", "NoArgs")
    delegate = Delegate(ADDRESS, source)
    assert source_map[ADDRESS] is delegate
    assert delegate.address20 == ADDRESS.removeprefix("0x").lower()
