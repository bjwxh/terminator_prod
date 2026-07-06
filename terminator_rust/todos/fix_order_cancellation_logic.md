# Fix: Unmanaged & Cross-Strategy Order Cancellation

## Problem

`create_execution_plan` in `strategy.rs` does not match Python's cancellation behavior.
During a live session, any working order placed by a different sub-strategy (e.g. `strat_0900`
while `strat_0930` is evaluating) is silently skipped — never cancelled, never protected.
After restart, manually-placed orders with no `strategy_id` were also skipped (partially fixed
in the previous PR, but the cross-strategy case remains).

**Assumption**: every SPX 0DTE working order on this account belongs to this strategy. Orders
are either placed by the APP (with a `strategy_id`) or placed manually to intervene (no
`strategy_id`). Cross-account interference does not apply.

## Root Cause

Rust has two bugs:

1. **Early skip** (`strategy.rs:1411`): orders with a known non-matching `strategy_id` hit
   `continue` and are never evaluated or cancelled.
2. **Cancellation guard** (`strategy.rs:1460`): even if an order reaches the stale branch,
   it is only cancelled if it belongs to the current strategy or has an empty `strategy_id`.
   Orders from a different sub-strategy slip through both guards unaffected.

## Three Order Categories

| `strategy_id` on order | Situation | Correct initial `is_stale` |
|---|---|---|
| Matches current strategy | placed by this sub-strategy | `false` — let leg check decide |
| Non-empty, different | placed by another sub-strategy in same session | `true` — cancel immediately |
| Empty | manual intervention OR pre-restart order (cache lost on restart) | `false` — let leg check decide |

Pre-restart orders lose their `strategy_id` tag because the in-memory cache is cleared on
restart. They must be evaluated by their legs, not immediately discarded — a valid pre-restart
order that still matches the current trade should be protected, not cancelled and resubmitted.

## Changes Required — `terminator_rust/src/strategy.rs`

All three edits are inside `create_execution_plan`.

### 1. Remove the early skip (lines 1411–1413)

```rust
// DELETE this block entirely:
if !belongs_here && !order_strat_id.is_empty() {
    continue;
}
```

### 2. Initialize `is_stale` based on ownership AND whether strategy_id is known (line 1415)

```rust
// BEFORE:
let mut is_stale = false;

// AFTER:
let mut is_stale = !belongs_here && !order_strat_id.is_empty();
```

- Different known strategy_id → stale immediately (cancel without checking legs)
- Empty strategy_id (unmanaged / post-restart) → `false`, leg check decides
- Same strategy_id → `false`, leg check decides

The leg loop can only set `is_stale = true`, never clear it.

### 3. Remove the ownership guard on cancellation (lines 1459–1462)

```rust
// BEFORE:
if is_stale {
    if order_strat_id == trade.strategy_id || order_strat_id.is_empty() {
        to_cancel.push(wid);
    }
}

// AFTER:
if is_stale {
    to_cancel.push(wid);
}
```

### Note: keep the Rust direction check

The leg loop's explicit direction-mismatch check (opposite BUY/SELL for the same strike/side
marks the order stale) is an improvement over Python and should be kept.

## Verification

```bash
cargo build
```

Then deploy and confirm that stale orders from any sub-strategy are cancelled when a new
signal fires.
