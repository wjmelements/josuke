from json import dumps, loads
from os import environ
from requests import post
from subprocess import PIPE, Popen

from eth_utils import to_checksum_address

from .selectors import Selector


def slot_address(raw: str | None) -> str | None:
    """The delegate address held in a 32-byte storage word, or None if empty."""
    if not raw or int(raw, 16) == 0:
        return None
    return to_checksum_address("0x" + raw.removeprefix("0x").rjust(64, "0")[-40:])


class ProxyStorage:
    def __init__(self, address):
        self.address = address
        self.storage_keys = {} # Selector.selector -> storage_key
        self.storage_values = {} # Selector.selector -> storage_value

    def fetch(self, selectors: list[Selector]):
        evm = Popen(["evm", "-nx"], stdin=PIPE, stdout=PIPE, text=True, bufsize=1)
        proxy_address = self.address.lower()
        for selector in selectors:
            # Calling the method directly works even if `implementation` is not installed
            print(dumps({
                "to": proxy_address,
                "data": selector.selector
            }), file=evm.stdin)
            while True:
                request = evm.stdout.readline()
                if not (request.startswith("{") or request.startswith("[")):
                    # Discard call result and advance to next selector
                    break
                request = loads(request)
                result = post(environ['ETH_RPC_URL'], json=request)
                assert result.status_code == 200
                # Answer the request
                result = result.text
                print(result, file=evm.stdin)
                result = loads(result)
                if type(request) is dict:
                    request_list = [request]
                    result_list = [result]
                else:
                    request_list = request
                    result_list = result
                for request, result in zip(request_list, result_list):
                    is_match = request["method"] == "eth_getStorageAt" and request["params"][0] == proxy_address
                    if not is_match:
                        continue
                    if selector.selector in self.storage_keys:
                        # The proxy's dispatch SLOAD comes first; later reads are
                        # the delegate touching proxy storage itself.
                        continue
                    slot = int(request["params"][1], 16)
                    self.storage_keys[selector.selector] = f"0x{slot:064x}"
                    self.storage_values[selector.selector] = result["result"]
        evm.terminate()
