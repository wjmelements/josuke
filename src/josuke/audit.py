"""`josuke audit`: every delegate a proxy ever installed is recorded and verifiable.

`verify` checks the delegates installed *now*. A delegate that was installed and
later swapped out leaves no trace in storage, but its `SelectorDelegated` event
is still in the logs. `audit` reads those logs from the proxy's deployment
onward, requires every delegate they name to be recorded in the ledger
(`current`, `history`, or — pending `accept` — `proposed`), and verifies each
`current` facet and `history` entry against its source the way `verify` does.

`SelectorDelegated` is only RECOMMENDED by ERC-8167, and a migration is arbitrary
code, so a clean audit shows that no *announced* delegate is unaccounted for, not
that none could have been installed silently. `verify`'s storage checks cover
the live routing.
"""

import pathlib
from dataclasses import dataclass
from os import environ

import click
from eth_abi import decode as abi_decode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak, to_checksum_address

from .ethjsonrpc import RpcError, chain_id, eth_block_number, eth_get_code, eth_get_logs
from .ledger import load_ledger
from .migration import SELECTOR_DELEGATED_SIG
from .verify import Report, SourceTrees, verify_facets, verify_selectors

# event SelectorDelegated(bytes4 indexed selector, address indexed delegate)
SELECTOR_DELEGATED_TOPIC = "0x" + SELECTOR_DELEGATED_SIG.hex()
# event DiamondDelegateCall(address indexed delegate, bytes delegateCalldata)
DIAMOND_DELEGATE_CALL_TOPIC = "0x" + keccak(text="DiamondDelegateCall(address,bytes)").hex()

ZERO_ADDRESS = to_checksum_address("0x" + "00" * 20)


@dataclass(frozen=True)
class Delegation:
    """A `SelectorDelegated` log; `delegate` is the zero address for a removal."""

    block: int
    tx: str
    selector: str
    delegate: str


@dataclass(frozen=True)
class MigrationCall:
    """A `DiamondDelegateCall` log: the proxy delegatecalled `migration`."""

    block: int
    tx: str
    migration: str
    calldata: bytes


def find_deploy_block(proxy: str, latest: int) -> int:
    """The first block at which `proxy` has code.

    Bisects `eth_getCode`, so the node must serve historical state; a pruned node
    errors here rather than answering. Also errors if `proxy` has no code at
    `latest`. `--from-block` is the escape hatch for both cases.
    """

    def has_code(block: int) -> bool:
        try:
            return eth_get_code(proxy, hex(block)) not in ("0x", "0x0")
        except RpcError as e:
            raise click.ClickException(
                f"cannot find where {proxy} was deployed: {e.message}\n"
                f"(the node needs historical state; pass --from-block to skip the search)"
            )

    if not has_code(latest):
        raise click.ClickException(
            f"{proxy} has no code at block {latest}; pass --from-block to audit it anyway"
        )
    lo, hi = 0, latest
    while lo < hi:
        mid = (lo + hi) // 2
        if has_code(mid):
            hi = mid
        else:
            lo = mid + 1
    return lo


def get_logs(proxy: str, from_block: int, to_block: int) -> list[dict]:
    """Every SelectorDelegated / DiamondDelegateCall log `proxy` emitted in the
    inclusive range, oldest first.

    Nodes cap the block span (or result count) of one `eth_getLogs`, and the cap
    isn't discoverable, so an errored span is halved and retried. The span never
    grows back, so once the cap is found the rest of the scan runs at it. A
    single-block failure is real and propagates.
    """
    topics = [[SELECTOR_DELEGATED_TOPIC, DIAMOND_DELEGATE_CALL_TOPIC]]
    logs: list[dict] = []
    lo, span = from_block, to_block - from_block + 1
    while lo <= to_block:
        hi = min(lo + span - 1, to_block)
        try:
            logs.extend(eth_get_logs(proxy, topics, lo, hi))
        except RpcError:
            if span == 1:
                raise
            span //= 2
            continue
        lo = hi + 1
    return sorted(logs, key=lambda log: (int(log["blockNumber"], 16), int(log["logIndex"], 16)))


def parse_logs(logs: list[dict], report: Report) -> tuple[list[Delegation], list[MigrationCall]]:
    delegations, calls = [], []
    for log in logs:
        if log.get("removed"):
            continue
        topics, tx = log["topics"], log["transactionHash"]
        block = int(log["blockNumber"], 16)
        try:
            if topics[0] == SELECTOR_DELEGATED_TOPIC:
                if len(topics) != 3:
                    raise ValueError("expected 2 indexed arguments")
                delegations.append(
                    Delegation(block, tx, topics[1][:10], _topic_address(topics[2]))
                )
            else:
                if len(topics) != 2:
                    raise ValueError("expected 1 indexed argument")
                (calldata,) = abi_decode(["bytes"], bytes.fromhex(log["data"].removeprefix("0x")))
                calls.append(MigrationCall(block, tx, _topic_address(topics[1]), calldata))
        except (ValueError, DecodingError) as e:
            report.fail(f"audit: malformed log at block {block} (tx {tx}): {e}")
    return delegations, calls


def _topic_address(topic: str) -> str:
    return to_checksum_address("0x" + topic[-40:])


def _state_delegates(name: str, state: dict | None) -> dict[str, str]:
    """address -> `<name> <source>` for every delegate a deployment state records."""
    if not state:
        return {}
    delegates = {
        to_checksum_address(rec["address"]): f"{name} {source_id}"
        for source_id, rec in state["facets"].items()
        if rec.get("address")
    }
    if state.get("selectors"):
        delegates[to_checksum_address(state["selectors"]["address"])] = f"{name} selectors()"
    return delegates


def recorded_delegates(history: dict) -> dict[str, str]:
    """address -> where the ledger records it, preferring `current`, then `history`,
    then `proposed`, so a `proposed` label means it is recorded nowhere else."""
    archived = {
        to_checksum_address(address): f"history {entry.get('source', 'selectors()')}"
        for address, entry in history.get("history", {}).items()
    }
    return (
        _state_delegates("proposed", history.get("proposed"))
        | archived
        | _state_delegates("current", history.get("current"))
    )


def verify_history_entry(address: str, entry: dict, tree: pathlib.Path, report: Report, rpc_cache: dict) -> None:
    """A `history` entry is built from its recorded commit and is live at `address`.

    Rewrapped as a one-facet deployment state so it goes through the same checks
    as `current` and `proposed`."""
    label = f"history {address}"
    if "selectorsFor" in entry:
        state = {
            "facets": {source_id: {} for source_id in entry["selectorsFor"]},
            "selectors": {"address": address},
        }
        verify_selectors(state, label, tree, report)
        return
    rec = {k: v for k, v in entry.items() if k not in ("source", "gitCommit")}
    rec["address"] = address
    verify_facets({"facets": {entry["source"]: rec}}, label, tree, report, rpc_cache)


def audit_proxy(
    proxy: str,
    history: dict,
    chain: str,
    latest: int,
    from_block: int | None,
    trees: SourceTrees,
    report: Report,
    rpc_cache: dict,
) -> int:
    """Audit one proxy; returns how many installed delegates are recorded only in `proposed`."""
    if from_block is None:
        from_block = find_deploy_block(proxy, latest)
        origin = f"deployed at block {from_block}"
    else:
        origin = f"from block {from_block} (--from-block)"
    click.echo(f"{proxy} chain {chain}: {origin}, scanning through block {latest}")

    delegations, calls = parse_logs(get_logs(proxy, from_block, latest), report)

    for call in calls:
        detail = f", calldata 0x{call.calldata.hex()}" if call.calldata else ""
        click.echo(f"  migration {call.migration} at block {call.block} (tx {call.tx}){detail}")

    recorded = recorded_delegates(history)
    installs: dict[str, list[Delegation]] = {}  # delegate -> its installs, in log order
    for d in delegations:
        if d.delegate != ZERO_ADDRESS:
            installs.setdefault(d.delegate, []).append(d)

    unaccepted = 0

    for delegate, ds in installs.items():
        first = ds[0]
        where = recorded.get(delegate)
        summary = f"{len(ds)} selector install(s), first at block {first.block}"
        if where:
            click.echo(f"  delegate {delegate} {where}: {summary}")
            if where.startswith("proposed "):
                unaccepted += 1
                click.echo(
                    f"warning: delegate {delegate} ({where}) is installed but only recorded in proposed",
                    err=True,
                )
        else:
            click.echo(f"  delegate {delegate} UNRECORDED: {summary}")
            report.fail(
                f"audit: delegate {delegate} installed for {first.selector} at block "
                f"{first.block} (tx {first.tx}) is not recorded in current, proposed or history"
            )

    # SelectorDelegated is only RECOMMENDED, so a delegate missing from the logs is not a failure.
    current = history.get("current")
    if current:
        for address, where in _state_delegates("current", current).items():
            if address not in installs:
                click.echo(f"warning: {where} {address} was never installed per the logs", err=True)
        tree = trees.get(current["gitCommit"])
        verify_facets(current, "current", tree, report, rpc_cache)
        verify_selectors(current, "current", tree, report)

    for address, entry in history.get("history", {}).items():
        address = to_checksum_address(address)
        if address not in installs:
            click.echo(f"warning: history {address} was never installed per the logs", err=True)
        verify_history_entry(address, entry, trees.get(entry["gitCommit"]), report, rpc_cache)
    return unaccepted


def run_audit(ledger_path, from_block: int | None = None):
    if "ETH_RPC_URL" not in environ:
        raise click.ClickException("ETH_RPC_URL is not set")

    root = pathlib.Path.cwd()
    ledger = load_ledger(ledger_path)
    chain = chain_id()
    latest = eth_block_number()  # fixed up front so every proxy is audited through the same block
    report = Report()
    rpc_cache: dict = {}
    audited = 0
    unaccepted = 0

    with SourceTrees(root) as trees:
        for entry in ledger:
            report.proxy = proxy = to_checksum_address(entry["address"])
            history = entry.get("deployments", {}).get(chain)
            if not history:
                click.echo(f"{proxy}: nothing recorded on chain {chain}")
                continue
            before = len(report.failures)
            unaccepted += audit_proxy(
                proxy, history, chain, latest, from_block, trees, report, rpc_cache
            )
            if len(report.failures) == before:
                audited += 1
                click.secho(f"✓ {proxy}: every installed delegate is recorded and verified", fg="green")

    if unaccepted:
        click.echo(
            f"warning: {unaccepted} installed delegate(s) are recorded only in proposed; "
            f"run `josuke accept` to move them into current",
            err=True,
        )

    if report.failures:
        raise click.ClickException(
            f"{len(report.failures)} audit failure(s):\n"
            + "\n".join(f"  - {failure}" for failure in report.failures)
        )
    click.secho(
        f"✓ audited chain {chain} through block {latest}: {audited}/{len(ledger)} "
        f"proxy entr{'y' if len(ledger) == 1 else 'ies'} clean",
        fg="green", bold=True,
    )
