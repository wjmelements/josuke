// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Layout} from "./Layout.sol";

contract Counter is Layout {
    function count() external view returns (uint256) {
        return count_;
    }

    function increment() external {
        count_ += 1;
    }
}
