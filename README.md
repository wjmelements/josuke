# josuke

Automated, verifiable upgrades for [ERC-8167](https://eips.ethereum.org/EIPS/eip-8167) modular dispatch proxies.

## Installation

josuke needs these on `PATH`:

- Python 3.10+
- [Foundry](https://getfoundry.sh)'s `forge`/`cast`
- the [`evm`](https://github.com/wjmelements/evm) assembler/interpreter

```
# EVM assembler/interpreter
git clone --recurse-submodules https://github.com/wjmelements/evm.git
make -C evm bin/evm
sudo install evm/bin/evm /usr/local/bin/evm

# Foundry (forge, cast)
curl -L https://foundry.paradigm.xyz | bash
foundryup

# josuke
git clone https://github.com/wjmelements/josuke.git
pip install ./josuke
```

## CLI

```
josuke init                     # create an empty josuke.json in the cwd
josuke add <address> <facet>...   # add facet sources to a proxy, registering it if new
josuke deploy                    # deploy changed/new facets, record them under `proposed`
josuke verify                    # check the ledger against the chain in $ETH_RPC_URL
josuke accept                    # after migrating, merge `proposed` into `current`
josuke audit                     # cross-check every delegate ever installed against the ledger
josuke check                     # offline: what the working tree would change, for pull requests
```

Pass `-f/--file` to any command to point at a ledger other than `./josuke.json`.
Pass `--all` to `deploy` to redeploy every facet, even ones whose bytecode is
unchanged.

For each proxy, `deploy` resolves `facetSrc`, builds each facet's init bytecode
from `HEAD` (reusing recorded `constructorArgs`, prompting for any that are
missing), and deploys only those whose bytecode differs from what `current`
records for that chain. When two proxies resolve to the same init bytecode, it
is deployed once and both proxies point at that address.

When no facet in the set implements ERC-8167 `selectors()`, `deploy` generates
that method from the full selector set (every facet selector plus `selectors()`
itself), deploys it with the `evm -C` universal constructor, and records it under
`selectors`.

It then builds the migration script from the facets' ABIs: each selector the
proposed facets expose is pointed at its facet, and each selector `current`'s
facets exposed at its `gitCommit` but the proposed ones don't is zeroed. Every
route that changes becomes one `SelectorDelegated` + `SSTORE`, to the storage
slot found by simulating a dispatch call against the live proxy with `evm -nx`.
Selectors are grouped by facet so the event signature is pushed once and each
facet address once, roughly halving the script's codesize. The
script is deployed with the `evm -C` universal constructor unless an identical one
is already recorded under `proposed`.

Since a wrong slot guess goes unnoticed until the migration runs, `deploy` first
rehearses the migration in `evm -nx` against the live proxy, before sending
anything. The migration runs as the proxy's code, with every facet in `current`
and `proposed` stood in for by a stub returning its own address. Then the
proxy's real dispatcher is asked where each selector goes: each proposed selector
must reach its facet, and each dropped one must revert.

For a `<path>.evm` facet (evm-assembler source), josuke reads bytecode and ABI
from the artifact its governing Makefile produces, building it with
`make -C <dir> out/<name>.evm/<name>.json` where `<dir>` is the nearest directory
above the source that has a Makefile.

The result is written back as a fresh `proposed` state (facets + `selectors` +
`migration`) stamped with the current git commit; `current` is left untouched.

For a proxy with a pending upgrade, `verify` first prints a summary of what
`proposed` changes: each facet marked new, changed or removed, with its address
and constructor arguments, and its selectors grouped beneath it — selectors not
served by `current` are listed up front. The pass/fail checks follow.

`verify` checks the recorded state against the chain, rebuilding each recorded
`gitCommit` in a throwaway `git worktree`. For `current`: every facet's
`initcodeHash` recomputes from its commit, its `codeHash` matches the code at
the recorded address, and the proxy dispatches each of its selectors to that
address. For `proposed`: the same hash checks, plus `proposed.facets` is exactly
what `facetSrc` resolves to, and the on-chain `migration` installs every
proposed selector and zeroes every selector dropped since `current`, both decoded
and rehearsed as `deploy` does. A recorded
`selectors` delegate must hold the method josuke generates for that facet set.
It reports all mismatches and exits non-zero if any.

`accept` is run once the migration has executed against the proxy. It checks on
chain that the migration took effect — every `proposed` selector now routes to
its facet (new and unchanged alike) and every selector dropped since `current`
is cleared — then merges `proposed` into `current` so the ledger's `current`
matches the live code, and removes `proposed`. It exits non-zero without
touching the ledger if any check fails. It also archives each delegate of the
old `current` that the accepted state no longer installs into `history`, keyed
by address, so `audit` still has a source to check it against.

`audit` reads the proxy's `SelectorDelegated` and `DiamondDelegateCall` logs
from its deployment onward (bisecting `eth_getCode` to find that block, unless
`--from-block` is given) and requires every delegate they name to be recorded
somewhere in the ledger — `current` or `history`. A delegate recorded only in
`proposed` is a warning: the migration has run, so `accept` it. Each `current`
facet and `history` entry is then verified against its recorded commit the same
way `verify` checks `current`. `SelectorDelegated` is only RECOMMENDED by ERC-8167 and a migration
can run arbitrary code, so a clean audit means no delegate the proxy announced
is unaccounted for, not that none could have been installed silently.
`DiamondDelegateCall` invocations are printed but not yet verified.

`check` needs no RPC and no keys: it builds the working tree with `forge` and
compares it with the ledger, so it fits a pull-request job. It fails when the
ledger breaks the schema or lists a proxy twice, when `facetSrc` doesn't resolve
to facets with creation code, when facets `deploy` would put at different
addresses export the same selector, when two facets declare different state
variables over the same storage bytes, when storage a recorded deployment
(`legacy`, `current`, `proposed` or a retired facet in `history`) declares would
read back differently at HEAD, when recorded constructor args don't encode, or
when a facet's runtime exceeds the 24,576-byte EIP-170 limit. The generated
`selectors()` contract is size-checked too. Solidity runtime size comes from the
build artifact; `.evm` artifacts must include `deployedBytecode` for this check,
otherwise a warning reports the missing size check.
A variable or struct member may be renamed (a warning), dropped (a warning: its
data stays), and a struct or fixed array may grow where its new bytes were free;
a moved, retyped or overlapped variable or member fails.
Renames are allowed between deployments, for example `viewContractAddress` to
`_viewContractAddress`, provided positions and types stay compatible. Facets
installed together must agree on names, including members of ERC-7201 structs.

`legacy` records, by hand, the implementation a proxy ran before its first
ERC-8167 migration, such as a UUPS contract:
`"legacy": {"source": "src/Service.sol:Service", "gitCommit": "<sha>"}` under the
chain in `deployments`. Keep it after the migration: its storage stays.

The storage checks read solc's storage layout: declared state variables, plus
every struct annotated `@custom:storage-location erc7201:<id>` that the facets'
sources import, placed at its ERC-7201 slot. The annotation is trusted, not the
slot the code computes. Other fixed-slot access and `.evm` facets are invisible.
solc produces a layout from analysis alone, so neither HEAD nor a recorded
commit gets a second build for it. Each recorded commit is
checked out, so a shallow CI clone needs them fetched (`fetch-depth: 0`). It lists each facet as new, changed, unchanged or removed against
`current`, and the constructor args `deploy` will ask for. It also compares HEAD
with the staged deployment (`proposed`, else `current`); `--strict` makes a
difference there fail, for release branches. `--chain` limits it to one chain,
and `--format markdown` suits `$GITHUB_STEP_SUMMARY`. It trusts the recorded
hashes; `verify` is what ties them to the chain.
For ordinary pull requests, run without `--strict`: source changes relative to a
deployment are expected. CI needs the recorded commits, submodules and their
Solidity compiler versions available locally. Build scripts and sources are
executed, so run untrusted pull requests without credentials.

`deploy` builds with `forge` and broadcasts with `cast`, sending its
deployments together and reporting each as it confirms. It reads the
environment:

| Variable | Purpose |
| --- | --- |
| `ETH_RPC_URL` | Target chain endpoint. Required; also fixes the chain id. |
| `ETH_KEYSTORE_ACCOUNT` | Keystore account name under `~/.foundry/keystores`. |
| `ETH_PASSWORD` | Path to that keystore's password file. Optional; see below. |
| `ETH_KEYSTORE` | Path to a keystore file or directory (alternative to the above). |
| `ETH_FROM` | Sender address, e.g. for an unlocked node account. |
| `JOSUKE_TRACE` | Set to log timestamped RPC calls, `evm` calls and subprocesses to stderr. |

The wallet variables are Foundry's own; any wallet `cast` accepts via the
environment works.

With a keystore and no `ETH_PASSWORD`, `deploy` prompts for the password once,
before its first transaction, and checks it with `cast` before spending any gas.
It hands the password to each `cast send` through an anonymous file on stdin,
never through argv, the environment, or a named file.

## josuke.json

`josuke.json` is the deployment ledger.
It records, per chain, which facet is installed where, what source and constructor arguments produced it, and any pending upgrade.
The deployment script reads it to decide what to redeploy, writes back a `proposed` upgrade, and once the migration is completed, `accept` merges `proposed` into `current`.
Anyone can replay it to verify a live deployment against the source.

### Schema

The full JSON Schema (draft 2020-12) is [`josuke.schema.json`](josuke.schema.json).
The file is an array of proxy entries.

Notes:

- `codeHash` answers "is the right code live?" cheaply from-chain; `initcodeHash`
  answers "was it built from this commit with these args?" and is what the
  verifier recomputes from source. A facet with no constructor has
  `initcodeHash == keccak256(initcode)` and omits `constructorArgs`.
- `from`, `nonce` and `block` record the environment a constructor read, so
  the verifier can replay it: `from` its sender, `nonce` (with `from`) its own
  address, as for an `address(this)` immutable, and `block` the values of its
  block it read, each keyed by the camelCase of its Solidity `block.<member>`
  (`block.basefee` is `baseFee`). Once a facet's creation is mined, `deploy`
  replays it in that block with `evm -nx`, traced, and records only what the
  constructor read, failing if the replay doesn't reproduce the code on chain.
  The chain id is never recorded: the ledger is keyed by it. `verify` replays
  with what was recorded, so it needs no archive node, and checks that `from`
  and `nonce` derive the facet's address.
- One `gitCommit` covers a whole `deploymentState`. When `proposed` is promoted,
  unchanged facets keep their existing entries, so a long-lived `current` can
  contain facets whose bytecode predates its `gitCommit`; only the facets
  redeployed in that upgrade are guaranteed to match it.
- For that reason, a facet whose metadata hash changed is redeployed even when
  its code did not. By default solc's metadata hash covers every source file's
  contents, so a comment or NatSpec edit redeploys the facet. `deploy` warns
  about this; set `bytecode_hash = "none"` in `foundry.toml` to redeploy only
  when code changes, or keep the hash to ship documentation updates as upgrades.
- The proxy `address` is assumed identical across chains; per-chain divergence
  would need a per-deployment address field.
- The migration script carries no hashes because its bytecode is recomputable
  from the `deploymentState`: one delegate-assignment fragment per selector, with
  the `evm -C` universal constructor (`600b380380600b3d393df3`) prepended.
- `selectors` carries no hashes for the same reason: it is the `selectors()`
  method generated from the set of selectors the `deploymentState` installs. It
  is absent when a facet implements `selectors()` itself.
- `history` records every former delegate of a proxy, keyed by address, so it
  can still be verified against source. `accept` adds to it; `audit` reads it.

### Example

See [`josuke.example.json`](josuke.example.json).
