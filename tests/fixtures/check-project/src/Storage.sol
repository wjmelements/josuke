// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Layout} from "./facets/Layout.sol";

// Appends slot 2 to the shared layout.
contract Tally is Layout {
    uint64 internal tally_;

    function tally() external view returns (uint64) {
        return tally_;
    }
}

// Appends the same declaration as Tally: compatible.
contract TallyToo is Layout {
    uint64 internal tally_;

    function tallyToo() external view returns (uint64) {
        return tally_;
    }
}

// Appends a different declaration at slot 2: conflicts with Tally.
contract Flag is Layout {
    address internal flag_;

    function flag() external view returns (address) {
        return flag_;
    }
}

// Ignores the shared layout: its own slot 0 conflicts with `owner_`.
contract Standalone {
    uint256 internal total_;

    function total() external view returns (uint256) {
        return total_;
    }
}
