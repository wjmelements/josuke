"""Ethereum JSON-RPC access: the raw `rpc` call plus thin `eth_*` wrappers."""

from itertools import count
from math import isfinite
from os import environ
from random import uniform
from time import sleep

import click
from requests import RequestException, post
from urllib3.exceptions import ProtocolError, ReadTimeoutError

from .proc import run
from .trace import brief, span


class RpcError(click.ClickException):
    """The node answered with an HTTP failure or a JSON-RPC error, or couldn't be reached.
    `status` is the HTTP failure's status, if that is what it was. From `post_batch`,
    `answers` holds the answers, by id, to the batches sent before the one that failed,
    and `failed` the ids of that one's requests; the rest weren't sent."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status
        self.answers: dict = {}
        self.failed: list = []


class RpcTimeout(RpcError):
    """No answer within ETH_RPC_TIMEOUT."""


class RpcDropped(RpcError):
    """The node took the request and closed the connection before answering in full."""


# Foundry's default for ETH_RPC_TIMEOUT (REQUEST_TIMEOUT in foundry-common).
_DEFAULT_TIMEOUT = 45.0


def rpc_timeout() -> float:
    """Seconds to wait for the node to connect, and then for each part of its answer:
    ETH_RPC_TIMEOUT, Foundry's own, which may be fractional here, else Foundry's default."""
    value = environ.get("ETH_RPC_TIMEOUT")
    if not value:
        return _DEFAULT_TIMEOUT
    try:
        seconds = float(value)
    except ValueError:
        seconds = 0.0
    if not (seconds > 0 and isfinite(seconds)):
        raise click.ClickException(f"ETH_RPC_TIMEOUT: not a number of seconds: {value}")
    return seconds


def _post(url: str, body):
    """POST `body` to `url`; failing to reach the node is an `RpcError`, which names
    ETH_RPC_URL rather than the url, as it may hold an API key."""
    seconds = rpc_timeout()
    try:
        return post(url, json=body, timeout=seconds)
    except ValueError:  # requests' InvalidSchema, MissingSchema and InvalidURL, or urllib's
        raise RpcError("ETH_RPC_URL: not an http(s) URL") from None
    except RequestException as e:
        # requests' error names the failure poorly: a timeout reading the body is a
        # ConnectionError, not a Timeout, and a ConnectTimeout is both. urllib3's error
        # below it says which, and below that the socket's, e.g. "[Errno 61] Connection
        # refused", gives the reason; requests' and urllib3's messages show the url.
        reason, dropped, cause = type(e).__name__, False, e.__cause__ or e.__context__
        while cause is not None:
            if isinstance(cause, ReadTimeoutError):
                raise RpcTimeout(f"ETH_RPC_URL: no answer within {seconds:g}s (ETH_RPC_TIMEOUT)") from None
            if isinstance(cause, ProtocolError):
                dropped, reason = True, str(cause.args[0])
            if isinstance(cause, OSError) and not isinstance(cause, RequestException):
                reason = str(cause)
                break
            cause = cause.__cause__ or cause.__context__
        if dropped:
            raise RpcDropped(f"ETH_RPC_URL: the node dropped the connection: {reason}") from None
        raise RpcError(f"ETH_RPC_URL: cannot reach the node: {reason}") from None


# Statuses a node or gateway answers with when it can't serve a request now, and how many
# times a request answered with one is sent in all. A 504 may instead mean the request is
# too heavy to answer in time, which only a lighter one avoids, so it is resent less.
_TRIES = {429: 5, 502: 5, 503: 5, 504: 2}
_BACKOFF = 1.0  # seconds before the first resend, about doubling for each one after
_MAX_WAIT = 60.0  # the longest wait before a resend, even if Retry-After asks for longer


def _wait(response, attempt: int) -> None:
    """Sleep before resending a request `response` failed, after `attempt` resends: as
    long as its Retry-After asks in seconds, else an exponential backoff with jitter."""
    try:
        seconds = float(response.headers.get("Retry-After", ""))
    except ValueError:  # absent, or an HTTP date
        seconds = float("nan")
    if not (isfinite(seconds) and seconds >= 0):
        seconds = _BACKOFF * 2**attempt * uniform(0.5, 1)
    sleep(min(seconds, _MAX_WAIT))


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
    """The node's answer to one JSON-RPC request, which may be an error; `label` heads its
    trace span. A request the node can't serve now is resent after a backoff (see `_TRIES`)."""
    for attempt in count():
        with span(f"{label} {request['method']} {brief(request.get('params', []))}"):
            response = _post(environ["ETH_RPC_URL"], request)
        if response.status_code == 200:
            break
        if attempt + 1 >= _TRIES.get(response.status_code, 1):
            raise RpcError(f"{request['method']}: HTTP {response.status_code}", response.status_code)
        _wait(response, attempt)
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
    503, which nodes also send when a batch is too large, or another status a node can't
    serve it now with (see `_TRIES`); none of those need be JSON."""
    calls = ", ".join(f"{req['method']} {brief(req.get('params', []))}" for req in chunk)
    with span(f"{label} [{len(chunk)}] {calls}"):
        response = _post(url, chunk)
    if response.status_code not in (200, 413, *_TRIES):
        raise RpcError(f"ETH_RPC_URL: HTTP {response.status_code}", response.status_code)
    try:
        body = response.json()
    except ValueError:
        if response.status_code == 200:  # else a proxy's own page
            raise RpcError(f"ETH_RPC_URL: HTTP 200, not JSON: {brief(response.text)}") from None
        body = None
    return response, body


def post_batch(batch: list[dict], label: str = "rpc") -> dict:
    """The node's answer to each request of `batch`, by id; an answer may be an error.
    It is sent in batches of at most the node's limit, and a batch the node finds too
    large is halved and resent, and one it can't serve now is resent after a backoff (see
    `_TRIES`). `label` heads each batch's trace span."""
    url = environ["ETH_RPC_URL"]
    answers = {}
    pending = list(batch)
    attempt = 0  # resends of the batch at the head of `pending`
    chunk: list[dict] = []
    try:
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
                # Overloaded (erigon and nethermind answer 503) or rate-limited, not too large a batch.
                if attempt + 1 < _TRIES[response.status_code]:
                    _wait(response, attempt)
                    attempt += 1
                    continue
                errors = [error["message"] for answer in fetched if (error := _error(answer)) and "message" in error]
                detail = errors[0] if errors else body if body is not None else brief(response.text)
                raise RpcError(f"ETH_RPC_URL: HTTP {response.status_code}: {detail}", response.status_code)
            attempt = 0
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
    except RpcError as e:
        e.answers, e.failed = answers, [req.get("id") for req in chunk]
        raise
    return answers


# Code at `latest` by lowercase address, kept for the run: deployed code doesn't
# change, but an address may get code later, so empty code isn't kept.
_codes: dict[str, str] = {}


def _keep_code(address: str, code: str) -> None:
    if code not in ("0x", "0x0"):
        _codes[address.lower()] = code


def eth_get_code(address: str, block: str = "latest") -> str:
    """The 0x-prefixed runtime bytecode at `address`."""
    if block != "latest":
        return rpc("eth_getCode", [address, block])
    if (code := _codes.get(address.lower())) is None:
        code = rpc("eth_getCode", [address, block])
        _keep_code(address, code)
    return code


def prefetch_code(addresses) -> None:
    """Read the code at `addresses` for `eth_get_code` in as few round trips as the
    node allows. Best effort: a failure is left for the read that needs the code."""
    wanted = list(dict.fromkeys(a.lower() for a in addresses if a and a.lower() not in _codes))
    if not wanted:
        return
    try:
        codes = rpc_batch([("eth_getCode", [address, "latest"]) for address in wanted])
    except RpcError:
        return
    for address, code in zip(wanted, codes):
        _keep_code(address, code)


def eth_block_number() -> int:
    return int(rpc("eth_blockNumber", []), 16)


def eth_get_logs_request(address: str, topics: list, from_block: int, to_block: int, request_id: int = 1) -> dict:
    """An eth_getLogs request for the logs `address` emitted over the inclusive block
    range, matching `topics`, to send with `post_request` or `post_batch`."""
    params = {"address": address, "topics": topics, "fromBlock": hex(from_block), "toBlock": hex(to_block)}
    return {"id": request_id, "jsonrpc": "2.0", "method": "eth_getLogs", "params": [params]}


def chain_id() -> str:
    """Decimal chain id string, from `cast` when available, else `eth_chainId`."""
    try:
        return str(int(run(["cast", "chain-id"]).strip()))
    except click.ClickException:  # no `cast`, or it failed
        return str(int(rpc("eth_chainId", []), 16))
