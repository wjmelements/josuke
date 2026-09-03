"""Tests for josuke.erc8167 helpers that don't need the `evm` binary."""

from josuke.erc8167 import SELECTORS_SELECTOR, generated_selectors
from josuke.selectors import Selector


def _sel(hex4):
    return Selector(hex4, f"fn{hex4}()")


def test_generated_selectors_sorts_dedupes_and_includes_itself():
    result = generated_selectors(
        [[_sel("0x22222222"), _sel("0x11111111")], [_sel("0x11111111")]]
    )
    assert [s.selector for s in result] == ["0x11111111", "0x22222222", SELECTORS_SELECTOR]


def test_generated_selectors_with_no_facets_is_just_itself():
    assert [s.selector for s in generated_selectors([])] == [SELECTORS_SELECTOR]
