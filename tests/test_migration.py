from josuke.migration import ContractSource, Delegate, SetDelegate, Migration, SET_DELEGATE_SIZE
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
