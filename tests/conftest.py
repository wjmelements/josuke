import pathlib

import pytest


class StubTrees:
    """Stands in for `SourceTrees`: each commit "checks out" to `trees/<commit>`,
    with no git and no build."""

    def __init__(self, root=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, commit):
        return pathlib.Path("trees", commit)


@pytest.fixture
def stub_source_trees(monkeypatch):
    """Call with a module to replace its `SourceTrees` with `StubTrees`."""

    def stub(module):
        monkeypatch.setattr(module, "SourceTrees", StubTrees)

    return stub
