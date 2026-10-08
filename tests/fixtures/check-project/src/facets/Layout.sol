// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

// The storage every facet of the proxy shares; abstract, so not a facet itself.
abstract contract Layout {
    address internal owner_;
    uint256 internal count_;
}
