from eth_utils import to_checksum_address

from .evm import EvmRelay
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
        proxy_address = self.address.lower()
        with EvmRelay() as relay:
            for selector in selectors:
                # Calling the method directly works even if `implementation` is not installed
                relay.call(
                    {"to": proxy_address, "data": selector.selector},
                    on_exchange=lambda req, resp, s=selector: self._note_slot(s, proxy_address, req, resp),
                )

    def _note_slot(self, selector, proxy_address, request, result):
        requests = request if isinstance(request, list) else [request]
        results = result if isinstance(result, list) else [result]
        for request, result in zip(requests, results):
            is_match = request["method"] == "eth_getStorageAt" and request["params"][0] == proxy_address
            if not is_match:
                continue
            if selector.selector in self.storage_keys:
                # The proxy's dispatch SLOAD comes first; later reads are the
                # delegate touching proxy storage itself.
                continue
            slot = int(request["params"][1], 16)
            self.storage_keys[selector.selector] = f"0x{slot:064x}"
            self.storage_values[selector.selector] = result["result"]
