// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Layout} from "./facets/Layout.sol";

// Exports `owner()` too, so it clashes with Ownable in one proxy.
contract Clash is Layout {
    function owner() external view returns (address) {
        return owner_;
    }
}

// Same bytecode as Clash under another name.
contract ClashTwin is Layout {
    function owner() external view returns (address) {
        return owner_;
    }
}

interface IClash {
    function owner() external view returns (address);
}
