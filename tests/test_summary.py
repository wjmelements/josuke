"""Tests for josuke.summary — the human-readable upgrade rendering.

`facet_selectors` and the git commit stamp are stubbed; the states are
fabricated. `facet_selectors` is keyed by (tree, source_id) so a test can give a
facet different selectors in `current` vs `proposed` (i.e. a moved selector).
"""

import pytest

from josuke import summary
from josuke.selectors import Selector

PROXY = "0x2222222222222222222222222222222222222222"


@pytest.fixture(autouse=True)
def _isolate_maps():
    from josuke import delegate as _d
    from josuke import selectors as _s

    sm, dm = dict(_s.selector_map), dict(_d.source_map)
    _s.selector_map.clear()
    _d.source_map.clear()
    yield
    _s.selector_map.clear()
    _s.selector_map.update(sm)
    _d.source_map.clear()
    _d.source_map.update(dm)


def _sel(hex4, name=None):
    return Selector(hex4, name or f"fn{hex4}()")


class FakeStorage:
    def __init__(self, values=None):
        self.storage_values = values or {}


def _state(facets, commit="c0", **extra):
    return {"gitCommit": commit, "facets": facets, **extra}


def _facet(initcode_hash="0xaaaa", address="0x" + "11" * 20, **extra):
    return {"initcodeHash": initcode_hash, "codehash": "0xbbbb", "address": address, **extra}


def _same(selectors_by_source):
    """Same selectors in both trees — the common case."""
    return {
        (tree, sid): sels
        for tree in ("cur", "prop")
        for sid, sels in selectors_by_source.items()
    }


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setattr(summary, "_commit_stamp", lambda tree: f"{tree}0000 2026-01-01")

    def _install(by_tree_source):
        monkeypatch.setattr(
            summary,
            "facet_selectors",
            lambda facet, tree: by_tree_source.get((tree, facet.source_id), []),
        )

    return _install


def _run(capsys, stub, mapping, current, proposed, storage=None):
    stub(mapping)
    summary.summarize_upgrade(
        PROXY, "314", current, proposed, "cur", "prop", storage or FakeStorage()
    )
    return capsys.readouterr().out


def _segment(out, marker):
    """The output from the line containing `marker` to the end."""
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if marker in line)
    return "\n".join(lines[start:])


def test_new_selector_is_listed_in_new_section(capsys, stub):
    out = _run(
        capsys,
        stub,
        _same(
            {
                "src/Gov.sol:Gov": [_sel("0x11111111", "govAction()")],
                "src/Emergency.sol:Emergency": [_sel("0x8456cac0", "pause()")],
            }
        ),
        current=_state({"src/Gov.sol:Gov": _facet()}),
        proposed=_state(
            {
                "src/Gov.sol:Gov": _facet(),
                "src/Emergency.sol:Emergency": _facet(address="0x" + "ee" * 20),
            }
        ),
    )
    assert "NEW SELECTORS (1)" in out
    top = _segment(out, "NEW SELECTORS")
    assert "pause()" in top and "→ Emergency" in top
    assert any(line.strip().startswith("+ pause()") for line in out.splitlines())
    # the unchanged facet stays a one-liner
    assert "govAction()" not in out


def test_selectors_grouped_under_their_facet(capsys, stub):
    out = _run(
        capsys,
        stub,
        _same(
            {
                "a.sol:A": [_sel("0xaa000001", "alpha()"), _sel("0xaa000002", "beta()")],
                "b.sol:B": [_sel("0xbb000001", "gamma()")],
            }
        ),
        current=None,
        proposed=_state({"a.sol:A": _facet(), "b.sol:B": _facet()}),
    )
    lines = out.splitlines()
    ai = next(i for i, line in enumerate(lines) if "a.sol:A" in line)
    bi = next(i for i, line in enumerate(lines) if "b.sol:B" in line)
    seg_a, seg_b = "\n".join(lines[ai:bi]), "\n".join(lines[bi:])
    assert "alpha()" in seg_a and "beta()" in seg_a and "gamma()" not in seg_a
    assert "gamma()" in seg_b


def test_unchanged_facet_is_a_one_liner(capsys, stub):
    out = _run(
        capsys,
        stub,
        _same({"a.sol:A": [_sel("0xaa000001", "alpha()")]}),
        current=_state({"a.sol:A": _facet(initcode_hash="0xsame")}),
        proposed=_state({"a.sol:A": _facet(initcode_hash="0xsame")}),
    )
    assert "UNCHANGED" in out
    assert "alpha()" not in out


def test_changed_facet_lists_selectors(capsys, stub):
    out = _run(
        capsys,
        stub,
        _same({"a.sol:A": [_sel("0xaa000001", "alpha()")]}),
        current=_state({"a.sol:A": _facet(initcode_hash="0xold")}),
        proposed=_state({"a.sol:A": _facet(initcode_hash="0xnew")}),
    )
    assert "CHANGED" in out
    assert "alpha()" in out


def test_removed_facet_groups_dropped_selectors(capsys, stub):
    out = _run(
        capsys,
        stub,
        _same(
            {
                "keep.sol:Keep": [_sel("0x11111111", "keep()")],
                "legacy.sol:Legacy": [
                    _sel("0x99999991", "old1()"),
                    _sel("0x99999992", "old2()"),
                ],
            }
        ),
        current=_state({"keep.sol:Keep": _facet(), "legacy.sol:Legacy": _facet()}),
        proposed=_state({"keep.sol:Keep": _facet()}),
    )
    seg = _segment(out, "REMOVED")
    assert "Legacy" in seg
    assert "selectors no longer served (2)" in seg
    assert "- old1()" in seg and "dropped" in seg
    assert "- old2()" in seg


def test_moved_selector_annotated_both_sides(capsys, stub):
    veto = _sel("0xf5b541a6", "veto(uint256)")
    mapping = {
        ("cur", "gov.sol:Gov"): [_sel("0x11111111", "govAction()"), veto],
        ("prop", "gov.sol:Gov"): [_sel("0x11111111", "govAction()")],
        ("prop", "emg.sol:Emergency"): [veto, _sel("0x8456cac0", "pause()")],
    }
    out = _run(
        capsys,
        stub,
        mapping,
        current=_state({"gov.sol:Gov": _facet(initcode_hash="0xg")}),
        proposed=_state(
            {
                "gov.sol:Gov": _facet(initcode_hash="0xg2"),
                "emg.sol:Emergency": _facet(address="0x" + "ee" * 20),
            }
        ),
    )
    # not a new selector — it existed before, just elsewhere
    assert "NEW SELECTORS (1)" in out  # only pause()
    assert "veto(uint256)" not in _segment(out, "NEW SELECTORS").split("FACETS")[0]

    gov_seg = _segment(out, "gov.sol:Gov")
    emg_seg = _segment(out, "emg.sol:Emergency")
    assert "» veto(uint256)" in emg_seg and "from Gov" in emg_seg
    gov_only = gov_seg.split("emg.sol:Emergency")[0]
    assert "» veto(uint256)" in gov_only and "moved to Emergency" in gov_only


def test_constructor_args_rendered_and_diffed(capsys, stub):
    out = _run(
        capsys,
        stub,
        _same({"t.sol:Token": [_sel("0xa9059cbb", "transfer(address,uint256)")]}),
        current=_state(
            {"t.sol:Token": _facet(initcode_hash="0xold", constructorArgs={"name": "Old", "supply": 100})}
        ),
        proposed=_state(
            {"t.sol:Token": _facet(initcode_hash="0xnew", constructorArgs={"name": "New", "supply": 100})}
        ),
    )
    assert "constructor" in out
    assert "~ name" in out and '"New"' in out and '(was "Old")' in out
    supply_line = next(line for line in out.splitlines() if "supply" in line)
    assert "(was" not in supply_line


def test_first_deployment_has_no_current(capsys, stub):
    out = _run(
        capsys,
        stub,
        _same({"a.sol:A": [_sel("0xaa000001", "alpha()")]}),
        current=None,
        proposed=_state({"a.sol:A": _facet()}),
    )
    assert "(none — first deployment)" in out
    assert "NEW " in out and "alpha()" in out
    assert "NEW SELECTORS (1)" in out


def test_migration_and_generated_lines(capsys, stub):
    keep = _sel("0x11111111", "keep()")
    gone = _sel("0x99999999", "gone()")
    mapping = {
        ("cur", "a.sol:A"): [keep, gone],
        ("prop", "a.sol:A"): [keep],
    }
    storage = FakeStorage({"0x99999999": "0x" + "00" * 12 + "aa" * 20})
    out = _run(
        capsys,
        stub,
        mapping,
        current=_state({"a.sol:A": _facet(initcode_hash="0xold")}),
        proposed=_state(
            {"a.sol:A": _facet(initcode_hash="0xnew")},
            selectors={"address": "0x" + "5e" * 20},
            migration={"address": "0x" + "d1" * 20},
        ),
        storage=storage,
    )
    assert "GENERATED" in out and "returns 2 selectors" in out
    assert "MIGRATION" in out
    assert "installs 2 selector routes, clears 1" in out
    # the generated selectors() route is not counted as a new method
    assert "NEW SELECTORS (0)" in out
    assert out.rstrip().endswith("0 new · 0 moved · 1 removed selector")
