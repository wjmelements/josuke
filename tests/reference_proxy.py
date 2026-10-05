"""The reference proxy, erc8167/src/Proxy.evm, shared by the tests that run it in `evm`."""

from eth_utils import keccak

from josuke.opcodes import CALLDATASIZE, PC, PUSH0, PUSH1

# delegates[msg.sig] is at keccak(msg.sig . NAMESPACE).
NAMESPACE = "f27774d37a8b3bf2306f60b561e4e8ec22cfb23796f1f777608c0e466ef52600"
PROXY_CODE = (
    "5f5f365f585f5f377f" + NAMESPACE + "5952595f20548060435751602052635416eb985f526024601cfd"
    "5b365f5f375af43d5f5f3e6054573d5ffd5b3d5ff3"
)
_JUMP_TARGETS = (0x43, 0x54)


def prefixed_proxy(prefix: str) -> str:
    """PROXY_CODE behind `prefix`. Its PC (4) becomes PUSH1 4, one byte longer,
    so its jump targets move by the prefix's length plus one."""
    shift = len(prefix) // 2 + 1
    head = f"{PUSH0}{PUSH0}{CALLDATASIZE}{PUSH0}"
    code = PROXY_CODE.replace(f"{head}{PC}", f"{head}{PUSH1}04", 1)
    for target in _JUMP_TARGETS:
        assert target + shift < 0x100, "the prefix pushes a jump target past PUSH1"
        code = code.replace(f"{PUSH1}{target:02x}", f"{PUSH1}{target + shift:02x}")
    return prefix + code


def word(address: str) -> str:
    return "0x" + address[2:].lower().rjust(64, "0")


def mapping_slot(selector: str) -> str:
    """delegates[selector]"""
    return "0x" + keccak(bytes.fromhex(selector[2:].ljust(64, "0") + NAMESPACE)).hex()
