// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Layout} from "./Layout.sol";

contract Ownable is Layout {
    function owner() external view returns (address) {
        return owner_;
    }

    function transferOwnership(address to) external {
        owner_ = to;
    }
}
