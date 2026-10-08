import json
import os
import pathlib
import selectors
import subprocess
from dataclasses import dataclass

import click

from .ethjsonrpc import post_batch, post_request
from .proc import run
from .trace import brief, log, span

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
    return run(["evm", "-x"], stdin=stdin).strip()


class EvmRelay:
    """A running ``evm -nx``. Feed it eth_call-shaped requests with :meth:`call`;
    the JSON-RPC state fetches it emits on stdout are forwarded to ``ETH_RPC_URL``
    and its result line is returned.

    Every RPC result is memoised by ``(method, params)`` in ``cache`` (a dict you
    may pass in to share across relays), so repeated reads — and repeated runs
    over the same initcode, in this process or the next — hit the node once. Use
    as a context manager so the process is always reaped.

    ``json_output`` runs ``evm -nxs``, whose result lines are JSON objects that
    also report the block values each request read. ``on_trace`` receives each
    line of evm's EIP-3155 trace, parsed, over a pipe rather than a file. evm
    flushes its trace before each line it writes to stdout, so by the time a state
    fetch is answered, or :meth:`call` returns, every step before it has been
    delivered (a step awaiting the fetch is delivered after it). ``trace_ops``
    limits ``on_trace`` to the steps of those opcodes, steps that failed (with an
    ``error``, such as "out of gas"), and each call's summary line, sparing the
    cost of parsing the rest."""

    def __init__(
        self, cache: dict | None = None, json_output: bool = False, on_trace=None, trace_ops: set[str] | None = None
    ):
        self.cache = {} if cache is None else cache
        args = ["evm", "-nxs" if json_output else "-nx"]
        trace_write = None
        self._on_trace = on_trace
        self._trace_ops = None if trace_ops is None else {op.encode() for op in trace_ops}
        self._selector = selectors.DefaultSelector()
        if on_trace is not None:
            self._trace, trace_write = os.pipe()
            args += ["-t", "-T", f"/dev/fd/{trace_write}"]
            # Drained alongside stdout: a full pipe would block evm while we wait on its stdout.
            os.set_blocking(self._trace, False)
            self._selector.register(self._trace, selectors.EVENT_READ)
            self._trace_buf = b""
        # Spans the process's lifetime; each call inside it has a span of its own.
        self._lifetime = span(f"run {' '.join(args[:4])}")
        self._lifetime.__enter__()
        self._proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0,
            pass_fds=() if trace_write is None else (trace_write,),
        )
        if trace_write is not None:
            os.close(trace_write)  # evm holds the only write end, so the trace ends when it exits
        self._stdout = self._proc.stdout.fileno()
        self._selector.register(self._stdout, selectors.EVENT_READ)
        self._out = b""

    def call(self, request: dict) -> str:
        """Run one request (a ``{"data": ...}`` create or a ``{"to": ...}`` call)
        and return evm's output line: the runtime hex for a create, ``""`` on a
        revert, or a JSON object with ``json_output``."""
        with span(f"evm {brief(request)}"):
            self._write(json.dumps(request))
            while True:
                line = self._readline()
                if line == "":
                    raise click.ClickException("evm -nx exited before returning a result")
                line = line.strip()
                if line[:1] not in ("{", "["):
                    return line
                rpc_request = json.loads(line)
                if isinstance(rpc_request, dict) and "method" not in rpc_request:
                    return line  # a JSON result, not a state fetch
                self._write(json.dumps(self._answer(rpc_request)))

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
                log(f"evm rpc cache hit {req['method']} {brief(req.get('params', []))}")
            else:
                misses.append((i, req, key))
        if misses:
            payload = [req for _, req, _ in misses]
            if isinstance(rpc_request, list):
                by_id = post_batch(payload, label="evm rpc")
            else:
                by_id = {payload[0].get("id"): post_request(payload[0], label="evm rpc")}
            for i, req, key in misses:
                answer = by_id[req.get("id")]
                if "result" in answer:
                    self.cache[key] = answer["result"]
                answers[i] = answer
        return answers if isinstance(rpc_request, list) else answers[0]

    def storage_at(self, address: str, keys) -> dict[str, str]:
        """Fetch `keys` of `address`'s storage in one batch, at the block evm reads, into
        the cache as evm would fetch them. The relay must have run a call first."""
        block = self.cache[("eth_blockNumber", "[]")]
        batch = [
            {"jsonrpc": "2.0", "id": i, "method": "eth_getStorageAt", "params": [address, key, block]}
            for i, key in enumerate(keys)
        ]
        answers = self._answer(batch)
        if any("result" not in answer for answer in answers):
            raise click.ClickException(f"ETH_RPC_URL: eth_getStorageAt {address} failed")
        return {req["params"][1]: answer["result"] for req, answer in zip(batch, answers)}

    def _readline(self) -> str:
        """evm's next stdout line, or "" once it has exited, after the trace it flushed before it."""
        while b"\n" not in self._out:
            ready = {key.fd for key, _ in self._selector.select()}
            if self._on_trace is not None and self._trace in ready:
                self._read_trace()
            if self._stdout in ready:
                chunk = os.read(self._stdout, 1 << 16)
                if not chunk:
                    return ""
                self._out += chunk
        if self._on_trace is not None:
            self._read_trace()
        line, _, self._out = self._out.partition(b"\n")
        return line.decode() + "\n"

    def _read_trace(self) -> None:
        """Deliver every complete trace line in the pipe."""
        while True:
            try:
                chunk = os.read(self._trace, 1 << 16)
            except BlockingIOError:
                return
            if not chunk:
                if self._trace in self._selector.get_map():
                    self._selector.unregister(self._trace)
                return
            *lines, self._trace_buf = (self._trace_buf + chunk).split(b"\n")
            for line in lines:
                if self._trace_ops is None or _delivered(line, self._trace_ops):
                    self._on_trace(json.loads(line))

    def _write(self, line: str) -> None:
        self._proc.stdin.write(line.encode() + b"\n")

    def close(self) -> None:
        # Each call's trace was delivered as it returned, so what is left belongs to one an
        # exception cut short. Unread, it would stall such a call, which may never end.
        self._proc.stdin.close()
        if self._on_trace is not None:
            os.close(self._trace)
        # At end of input evm exits on its own; terminate only a stuck one.
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            self._proc.wait()
        self._proc.stdout.close()
        self._selector.close()
        self._lifetime.__exit__(None, None, None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _delivered(line: bytes, ops: set[bytes]) -> bool:
    """Whether an EIP-3155 trace line is a summary, a step that failed, or a step of one of `ops`."""
    start = line.find(b'"opName":"')
    if start < 0 or b'"error":' in line:
        return True
    start += len(b'"opName":"')
    return line[start : line.index(b'"', start)] in ops


# Opcodes that observe the created contract's own address, which CREATE derives
# from the sender and its nonce. A CALLER read below depth 1 is a callee seeing the
# new contract as its caller; the trace doesn't say which contract is executing, so
# that, like ADDRESS in a callee, is counted conservatively.
_ADDRESS_OPS = {"ADDRESS", "CREATE", "CREATE2"}
_SENDER_OPS = {"CALLER", "ORIGIN"}


@dataclass
class Replay:
    runtime: str | None  # hex, no 0x; None when the constructor reverted
    reads_sender: bool  # msg.sender or tx.origin: the replay needs `from`
    reads_address: bool  # its own address: the replay needs `from` and `nonce`
    block_overrides: dict  # the block values it read, as `evm` reports them
    revert_data: str = ""  # hex, no 0x; what a reverting constructor returned

    def revert_detail(self) -> str:
        """" with 0x<data>" for a revert that returned data, else ""."""
        return f" with 0x{self.revert_data}" if self.revert_data else ""


def replay_create(initcode_hex: str, request: dict | None = None, cache: dict | None = None, trace: bool = False) -> Replay:
    """Replay a contract creation through ``evm -nx`` against live state.

    ``request`` carries the environment to replay in: ``from``, ``nonce`` and
    ``blockOverrides``, as ``evm`` accepts them. With ``trace``, the EIP-3155 trace
    tells which of the sender and the contract's own address the constructor read;
    without it both are reported False."""
    reads = {"sender": False, "address": False}

    def note(step: dict) -> None:
        op = step.get("opName")  # absent on the per-request summary line
        if op in _ADDRESS_OPS or (op == "CALLER" and step["depth"] > 1):
            reads["address"] = True
        elif op in _SENDER_OPS:
            reads["sender"] = True

    with EvmRelay(cache=cache, json_output=True, on_trace=note if trace else None) as relay:
        result = json.loads(relay.call({**(request or {}), "data": initcode_hex}))
    data = result["returnData"].removeprefix("0x")
    reverted = int(result["status"], 16) == 0
    return Replay(
        None if reverted else data,
        reads["sender"],
        reads["address"],
        result.get("blockOverrides", {}),
        data if reverted else "",
    )


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
        "runtime": data.get("deployedBytecode", {}).get("object"),
    }
