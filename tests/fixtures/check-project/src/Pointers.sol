// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

// Internal function pointers are code offsets, which an upgrade doesn't keep;
// external ones are an address and a selector.
contract Pointers {
    struct Hooks {
        function(uint256) internal returns (uint256) transform;
    }

    function() internal hook;
    mapping(uint256 => Hooks) internal hooks;
    function() external callback;

    function ping() external {}
}
