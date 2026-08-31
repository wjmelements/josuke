import pytest

from josuke.migration import (
    ContractSource,
    Delegate,
    SetDelegate,
    Migration,
    SET_DELEGATE_SIZE,
    InvalidSetDelegate,
)
from josuke.selectors import Selector

function_abi = {
    "type": "function",
    "name": "encode",
    "inputs": [
        {
            "name": "str",
            "type": "string",
            "internalType": "MyString"
        }
    ],
    "outputs": [
        {
            "name": "encoded",
            "type": "bytes",
            "internalType": "UTF8"
        }
    ],
    "stateMutability": "nonpayable"
}

def test_SetDelegate_encode_decode():
    selector = Selector.from_abi(function_abi)
    assert selector.selector == "0x6a11b2a8"
    source = ContractSource("src/lib/UTF8Library.sol", "UTF8Library")
    address = "0x1A4E1a4e1A4E1a4e1a4E1a4e1A4e1A4E1a4E1A4e"
    storage_key = "0x035e8a4aa5c458e1b67ba99aeadf723b02971482f900ce17c7ae150613e85d24"
    delegate = Delegate(address, source)
    setdelegate = SetDelegate(
        selector.selector,
        storage_key,
        delegate,
    )

    encoded = setdelegate.encode()
    assert len(encoded) == SET_DELEGATE_SIZE

    decoded = SetDelegate.decode(encoded)

    assert str(decoded) == str(setdelegate)
    assert decoded == setdelegate


def test_SetDelegate_decode_rejects_non_encoded_bytestring():
    selector = Selector.from_abi(function_abi)
    source = ContractSource("src/lib/UTF8Library.sol", "UTF8Library")
    address = "0x1A4E1a4e1A4E1a4e1a4E1a4e1A4e1A4E1a4E1A4e"
    storage_key = "0x035e8a4aa5c458e1b67ba99aeadf723b02971482f900ce17c7ae150613e85d24"
    delegate = Delegate(address, source)
    setdelegate = SetDelegate(selector.selector, storage_key, delegate)

    encoded = setdelegate.encode()

    # the untouched encoding still round-trips
    assert SetDelegate.decode(encoded) == setdelegate

    # a byte too short
    with pytest.raises(InvalidSetDelegate):
        SetDelegate.decode(encoded[:-1])

    # a byte too long
    with pytest.raises(InvalidSetDelegate):
        SetDelegate.decode(encoded + b"\x00")

    # leading opcode is not PUSH20
    with pytest.raises(InvalidSetDelegate):
        SetDelegate.decode(b"\x00" + encoded[1:])

    # trailing opcode is not SSTORE
    with pytest.raises(InvalidSetDelegate):
        SetDelegate.decode(encoded[:-1] + b"\x00")

    # an interior opcode differs (the DUP1 after the address)
    mutated = bytearray(encoded)
    mutated[21] = 0x00
    with pytest.raises(InvalidSetDelegate):
        SetDelegate.decode(bytes(mutated))

    # the SelectorDelegated event topic differs
    mutated = bytearray(encoded)
    mutated[45] ^= 0xFF
    with pytest.raises(InvalidSetDelegate):
        SetDelegate.decode(bytes(mutated))

    # the zero padding after the 4-byte selector is dirty
    mutated = bytearray(encoded)
    mutated[30] = 0x11
    with pytest.raises(InvalidSetDelegate):
        SetDelegate.decode(bytes(mutated))
