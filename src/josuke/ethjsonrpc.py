"""Ethereum JSON-RPC access: the raw `rpc` call plus thin `eth_*` wrappers."""

import subprocess
from os import environ

import click
from requests import post

from .trace import brief, span


class RpcError(click.ClickException):
    """The node answered with an HTTP failure or a JSON-RPC error."""


def rpc(method: str, params: list):
    answer = post_request({"id": 1, "jsonrpc": "2.0", "method": method, "params": params})
    if answer.get("error"):
        raise RpcError(f"{method}: {answer['error']}")
    return answer["result"]


def rpc_batch(calls: list[tuple[str, list]]) -> list:
    """Results of several `(method, params)` calls, in order, from as few HTTP round
    trips as the node allows. An error in any call fails the whole batch."""
    by_id = post_batch([{"id": i, "jsonrpc": "2.0", "method": m, "params": p} for i, (m, p) in enumerate(calls)])
    results = []
    for i, (method, params) in enumerate(calls):
        if by_id[i].get("error"):
            raise RpcError(f"{method} {brief(params)}: {by_id[i]['error']}")
        results.append(by_id[i]["result"])
    return results


def post_request(request: dict, label: str = "rpc") -> dict:
    """The node's answer to one JSON-RPC request, which may be an error; `label` heads its trace span."""
    with span(f"{label} {request['method']} {brief(request.get('params', []))}"):
        response = post(environ["ETH_RPC_URL"], json=request)
    if response.status_code != 200:
        raise RpcError(f"{request['method']}: HTTP {response.status_code}")
    try:
        answer = response.json()
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        raise RpcError(f"{request['method']}: not a JSON-RPC answer: {brief(response.text)}")
    return answer


# Batches carry at most this many requests (erigon's default limit), fewer once a node
# rejects that many.
_BATCH_LIMIT = 100
_batch_limits: dict[str, int] = {}  # ETH_RPC_URL -> the most requests it has taken in one batch
# Rejecting a whole batch as too large: geth and erigon -32600; jsonrpsee (reth, forest)
# -32010 for its requests, -32011 for its response, -32007 for its body. -32005: _too_large.
_BATCH_TOO_LARGE = {-32600, -32007, -32010, -32011}
# geth's answer to the requests left once a batch's response grows too large.
_RESPONSE_TOO_LARGE = {-32003}


def _error(answer) -> dict | None:
    error = answer.get("error") if isinstance(answer, dict) else None
    return error if isinstance(error, dict) else None


def _too_large(answer, codes: set[int]) -> bool:
    """Whether `answer` is an error saying a batch was too large, by one of `codes`, or by
    -32005 naming the batch: nethermind's "Batch size limit exceeded" and
    "MaxBatchResponseBodySize … exceeded", and besu's "… exceeds max batch size". Nodes
    send -32005 for other limits too: erigon's and nethermind's overload ("server
    overloaded", "Too many requests"), providers' rate limits, and limits of a method."""
    error = _error(answer)
    if error is None:
        return False
    if error.get("code") == -32005:
        return "batch" in str(error.get("message", "")).lower()
    return error.get("code") in codes


def _post_chunk(url: str, chunk: list[dict], label: str):
    """POST one batch; the response and its decoded body. It may come back with 413 or
    503, which nodes also send when a batch is too large, and a 413 may not be JSON."""
    calls = ", ".join(f"{req['method']} {brief(req.get('params', []))}" for req in chunk)
    with span(f"{label} [{len(chunk)}] {calls}"):
        response = post(url, json=chunk)
    if response.status_code not in (200, 413, 503):
        raise RpcError(f"ETH_RPC_URL: HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        if response.status_code != 413:  # a proxy's own page for too large a body
            raise RpcError(f"ETH_RPC_URL: HTTP {response.status_code}, not JSON: {brief(response.text)}") from None
        body = None
    return response, body


def post_batch(batch: list[dict], label: str = "rpc") -> dict:
    """The node's answer to each request of `batch`, by id; an answer may be an error.
    It is sent in batches of at most the node's limit, and a batch the node finds too
    large is halved and resent. `label` heads each batch's trace span."""
    url = environ["ETH_RPC_URL"]
    answers = {}
    pending = list(batch)
    while pending:
        chunk = pending[: _batch_limits.get(url, _BATCH_LIMIT)]
        response, body = _post_chunk(url, chunk, label)
        fetched = body if isinstance(body, list) else [body]
        by_id = {answer.get("id"): answer for answer in fetched if isinstance(answer, dict)}
        missing = [req for req in chunk if req.get("id") not in by_id]
        if response.status_code == 413 or missing and any(_too_large(answer, _BATCH_TOO_LARGE) for answer in fetched):
            # The node answered the batch with one error rather than each request.
            if len(chunk) == 1:
                raise RpcError(f"ETH_RPC_URL: rejected a batch of one request: {body}")
            _batch_limits[url] = len(chunk) // 2
            continue
        if response.status_code != 200:
            # Overloaded (erigon and nethermind answer 503), not too large a batch.
            errors = [error["message"] for answer in fetched if (error := _error(answer)) and "message" in error]
            raise RpcError(f"ETH_RPC_URL: HTTP {response.status_code}: {errors[0] if errors else body}")
        if missing:
            methods = ", ".join(req["method"] for req in missing)
            raise RpcError(f"ETH_RPC_URL: no answer to {methods} in a batch: {body}")
        # Requests cut off by too large a response are resent in a smaller batch.
        cut = [req for req in chunk if _too_large(by_id[req.get("id")], _RESPONSE_TOO_LARGE)]
        if cut and len(chunk) > 1:
            _batch_limits[url] = max(1, len(chunk) - len(cut))
        else:
            cut = []
        answers.update((req.get("id"), by_id[req.get("id")]) for req in chunk if req not in cut)
        pending = cut + pending[len(chunk) :]
    return answers


def eth_get_code(address: str, block: str = "latest") -> str:
    """The 0x-prefixed runtime bytecode at `address`."""
    return rpc("eth_getCode", [address, block])


def eth_block_number() -> int:
    return int(rpc("eth_blockNumber", []), 16)


def eth_get_logs(address: str, topics: list, from_block: int, to_block: int) -> list:
    """Logs emitted by `address` over the inclusive block range, matching `topics`."""
    return rpc(
        "eth_getLogs",
        [
            {
                "address": address,
                "topics": topics,
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
            }
        ],
    )


def chain_id() -> str:
    """Decimal chain id string, from `cast` when available, else `eth_chainId`."""
    try:
        out = subprocess.run(
            ["cast", "chain-id"], capture_output=True, text=True, check=True
        ).stdout.strip()
        return str(int(out))
    except (FileNotFoundError, subprocess.CalledProcessError):
        return str(int(rpc("eth_chainId", []), 16))
