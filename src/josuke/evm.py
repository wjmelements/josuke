import json
import pathlib
import subprocess
from os import environ

import click
from requests import post

from .proc import run

# artifact paths already built this process, so repeated facet_abi / facet_initcode
# calls don't re-invoke make (make no-ops when up to date, but this also keeps its
# output off the console).
_built: set[str] = set()


def execute(initcode_hex: str, sender: str | None = None) -> str:
    """Run `evm -x`: execute creation bytecode, return the deployed runtime hex.

    `sender` sets `msg.sender` for the constructor, so an immutable derived from
    the deployer is reproduced.
    """
    if sender is None:
        stdin = initcode_hex
    else:
        stdin = json.dumps({"from": sender, "data": initcode_hex})
    return subprocess.run(
        ["evm", "-x"],
        input=stdin,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


class EvmRelay:
    """A running ``evm -nx``. Feed it eth_call-shaped requests with :meth:`call`;
    the JSON-RPC state fetches it emits on stdout are forwarded to ``ETH_RPC_URL``
    and its result line is returned.

    Every RPC result is memoised by ``(method, params)`` in ``cache`` (a dict you
    may pass in to share across relays), so repeated reads — and repeated runs
    over the same initcode, in this process or the next — hit the node once. Use
    as a context manager so the process is always reaped."""

    def __init__(self, cache: dict | None = None):
        self.cache = {} if cache is None else cache
        self._proc = subprocess.Popen(
            ["evm", "-nx"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def call(self, request: dict, on_exchange=None) -> str:
        """Run one request (a ``{"data": ...}`` create or a ``{"to": ...}`` call)
        and return evm's output line: the runtime hex for a create, ``""`` on a
        revert. ``on_exchange(rpc_request, rpc_response)`` sees every request/
        response pair, cache hits included."""
        self._write(json.dumps(request))
        while True:
            line = self._proc.stdout.readline()
            if line == "":
                raise click.ClickException("evm -nx exited before returning a result")
            line = line.strip()
            if line[:1] not in ("{", "["):
                return line
            rpc_request = json.loads(line)
            rpc_response = self._answer(rpc_request)
            self._write(json.dumps(rpc_response))
            if on_exchange is not None:
                on_exchange(rpc_request, rpc_response)

    def _answer(self, rpc_request):
        """Resolve one JSON-RPC request (object or batch array) from the cache,
        fetching only the misses from ``ETH_RPC_URL``."""
        batch = rpc_request if isinstance(rpc_request, list) else [rpc_request]
        answers = [None] * len(batch)
        misses = []
        for i, req in enumerate(batch):
            key = (req["method"], json.dumps(req.get("params", [])))
            if key in self.cache:
                answers[i] = {"jsonrpc": "2.0", "id": req.get("id"), "result": self.cache[key]}
            else:
                misses.append((i, req, key))
        if misses:
            payload = [req for _, req, _ in misses]
            response = post(environ["ETH_RPC_URL"], json=payload if isinstance(rpc_request, list) else payload[0])
            if response.status_code != 200:
                raise click.ClickException(f"ETH_RPC_URL: HTTP {response.status_code}")
            fetched = json.loads(response.text)
            by_id = {r.get("id"): r for r in (fetched if isinstance(fetched, list) else [fetched])}
            for i, req, key in misses:
                answer = by_id[req.get("id")]
                if "result" in answer:
                    self.cache[key] = answer["result"]
                answers[i] = answer
        return answers if isinstance(rpc_request, list) else answers[0]

    def _write(self, line: str) -> None:
        self._proc.stdin.write(line + "\n")
        self._proc.stdin.flush()

    def close(self) -> None:
        self._proc.terminate()
        self._proc.wait()
        self._proc.stdin.close()
        self._proc.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# A throwaway sender to contrast the real deployer against (and a spare, for the
# improbable case the real deployer is the first one).
_PROBE_SENDERS = ("0x" + "11" * 20, "0x" + "22" * 20)


def deployer_derived(initcode_hex: str, deployer: str) -> bool:
    """True when the deployed runtime depends on the constructor's msg.sender (an
    immutable set from the deployer, and the like), so `from` must be recorded.

    Replays ``initcode_hex`` as a create twice in one ``evm -nx`` process — once
    as the real ``deployer``, once as a probe address — so chain state is fetched
    once and frozen for both, and any difference in the returned runtime is the
    sender alone."""
    probe = next(s for s in _PROBE_SENDERS if s != deployer.lower())
    with EvmRelay() as relay:
        actual = relay.call({"from": deployer, "data": initcode_hex})
        probed = relay.call({"from": probe, "data": initcode_hex})
    return actual != probed


def _governing_makefile(source: pathlib.Path, root: pathlib.Path) -> pathlib.Path | None:
    """Nearest ancestor of `source` (up to and including `root`) with a Makefile."""
    root = root.resolve()
    directory = source.resolve().parent
    while True:
        if (directory / "Makefile").exists() or (directory / "GNUmakefile").exists():
            return directory
        if directory == root or root not in directory.parents:
            return None
        directory = directory.parent


def evm_artifact(source: pathlib.Path, root: pathlib.Path) -> dict:
    """Assemble the `.evm` facet at `source` via its Makefile and return
    `{"initcode": <hex>, "abi": [...]}`.

    ERC-8167 `.evm` files are evm-assembler source, not raw bytecode. Projects that
    ship them attach a Makefile rule (the `ASM_ARTIFACT` macro) that assembles the
    source and merges in the matching interface ABI, writing
    `out/<stem>.evm/<stem>.json`. josuke locates the Makefile that governs the
    source's directory (a vendored submodule has its own) and builds that artifact
    with `make -C`. The artifact is the only way to get the facet's ABI."""
    stem = source.stem
    makedir = _governing_makefile(source, root)
    if makedir is None:
        raise click.ClickException(
            f"{source}: no Makefile governs this .evm source; one must build "
            f"out/{stem}.evm/{stem}.json (with an attached ABI)"
        )

    rel = f"out/{stem}.evm/{stem}.json"
    artifact = makedir / rel
    key = str(artifact.resolve())
    if key not in _built:
        run(["make", "-C", str(makedir), rel])
        _built.add(key)

    if not artifact.exists():
        raise click.ClickException(
            f"{source}: `make -C {makedir} {rel}` did not produce {artifact}"
        )

    data = json.loads(artifact.read_text())
    abi = data.get("abi")
    if not abi:
        raise click.ClickException(
            f"{artifact}: no ABI attached; the Makefile rule must merge in the "
            f"matching interface ABI so josuke can read the facet's selectors"
        )
    return {
        "initcode": data["bytecode"]["object"].removeprefix("0x"),
        "abi": abi,
    }
