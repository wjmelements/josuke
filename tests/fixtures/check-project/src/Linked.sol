// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

library Doubler {
    function double(uint256 x) external pure returns (uint256) {
        return x * 2;
    }
}

contract Linked {
    function quadruple(uint256 x) external pure returns (uint256) {
        return Doubler.double(Doubler.double(x));
    }
}
