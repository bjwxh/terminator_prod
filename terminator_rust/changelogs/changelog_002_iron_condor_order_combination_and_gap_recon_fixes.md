# Changelog 002: Iron Condor Order Combination & GAP_RECON Fixes

**Date**: 2026-06-18

## Overview
The Rust app was sending each iron condor leg as a separate order to the exchange instead of combining them into a single 4-leg combo order. This changeset ports the Python app's `_get_smart_chunks` / `run_order_execution_loop` combo-order logic to Rust, fixes a silent trade-drop bug in GAP_RECON confirmation, resolves a reconciliation loop that kept re-popping the confirmation modal, and corrects the sim/live portfolio display attribution.

---

## Changes

### 1. Multi-leg Order Combination Engine (`strategy.rs`)

**Issue**: The old `execute_trade` sent all legs of a trade as a single flat order, but lacked the chunk-splitting logic needed to handle multi-unit positions and per-chunk price overrides. More critically, the confirmation UI had no way to show a grouped iron condor card — it fell back to rendering one card per leg.

**Fix**: Ported the Python `_get_smart_chunks` pipeline in full:

- **`gcd`** – greatest-common-divisor helper for computing spread units.
- **`unroll_legs`** – expands N-unit legs into unit legs (qty ±1 each), sorted PUT-first then by strike.
- **`roll_legs`** – re-aggregates unit legs by symbol, collapsing duplicates.
- **`classify_order_type`** – identifies the structure of a leg set: `single`, `vertical`, `butterfly`, `condor`, `iron_condor`, `iron_fly`, or `unknown`. Returns `(type_str, Option<is_credit>)`.
- **`extract_chunk`** – combinatorial extractor that finds the first N-leg subset satisfying a constraint (uniqueness-checked by side+strike).
- **`get_smart_chunks`** – priority-ordered chunker: iron condors → same-side 4-leg rolls → verticals → residuals. Consolidates identical combo signatures into higher-quantity orders.
- **New `execute_trade`** – iterates chunks, computes per-chunk net credit with tick rounding, applies `LegOverride` by chunk index, sets `complexOrderStrategyType` (IRON_CONDOR / VERTICAL / CUSTOM), and returns `Vec<Option<String>>` (one order ID per chunk).
- **`OptionLeg`** – added `instruction: Option<String>` field (skip-serialized when None) to allow explicit BTO/STO/BTC/STC overrides from bootstrap-loaded trades.
- **Stale-quote guard** – relaxed from 500 ms to 5 000 ms to match the ~1 Hz WebSocket tick rate and avoid false staleness exclusions.

### 2. GAP_RECON Confirmation Bug (`strategy.rs`)

**Issue**: `confirm_trade` dispatched via `strats.get_mut(strat_id)`. Since `"GAP_RECON"` is not a registered sub-strategy, this always returned `None` and the trade was silently discarded — the pending trade was consumed but no order was ever placed.

**Fix**: Added an explicit `GAP_RECON` / `RECONCILIATION` branch at the top of `confirm_trade` that:
1. Reads live positions from `live_portfolio` (the reconciliation ground-truth side).
2. Calls `execute_trade` with those positions and the user's per-chunk price overrides.
3. Pushes resulting order IDs into `working_orders`.
4. Does **not** call `live_portfolio.add_trade` — `live_portfolio` already represents the target state; adding the recon legs again would double every position and inflate live PnL.

### 3. Reconciliation Loop Guard (`strategy.rs`)

**Issue**: After a GAP_RECON order was confirmed and submitted, the working order was not yet filled at the broker. The next `check_reconciliation` tick saw the same live_portfolio vs broker discrepancy and generated a second (then third, etc.) confirmation modal, causing duplicate orders.

**Fix**: Added an early-return guard at the top of `check_reconciliation`:

```rust
let working = self.working_orders.lock().await;
if working.iter().any(|o| o.get("strategy_id")... == Some("GAP_RECON")) {
    return Ok(());
}
```

Reconciliation is suppressed while any GAP_RECON order is still listed in `working_orders`.

### 4. Confirmation UI: Single Iron Condor Card (`web.rs`, `app.js`)

**Issue**: The heartbeat `pending_trade` JSON did not include an `orders` field. The frontend fell back to rendering one card per leg (4 cards for an iron condor), and overrides were per-leg indices — mismatched against chunk-indexed `execute_trade`.

**Fix**:
- Added `build_orders_val(legs, order_offset)` to `web.rs`: calls `get_smart_chunks`, computes per-chunk ticked credit, builds a JSON array where each element describes one chunk (idx, type, qty, desc, price_ea, is_credit, lock_floor, legs).
- Both the heartbeat state snapshot and the WebSocket reconnect `trade_signal` message now include `trade.orders: orders_val`.
- `app.js` updated to read `tradeData.trade.orders` (with a per-leg fallback for backward compatibility). A single iron condor chunk renders as one adjustable price card; overrides sent back use chunk indices matching `execute_trade`.

### 5. Option Book Display (`web.rs`)

**Issue**: The option book was filtered by active WebSocket subscription status, causing strikes to disappear when subscriptions lagged.

**Fix**: Replaced subscription-set filter with an OTM-window filter (`[spx − otm_offset, spx + otm_offset]`). The window slides with SPX automatically and does not depend on subscription state. Call/put sides are independently gated by the ATM overlap constant from `manager.rs`.

### 6. Sim / Live Portfolio Attribution (`strategy.rs`, `web.rs`)

**Issue 1** (wrong panel): `web.rs` swapped `live_snap` into `live_payload` when `dry_run=false` and zeroed `sim_payload`. Since `live_portfolio` is the sim ground-truth (used by `check_reconciliation`), all strategy PnL appeared in the Live panel and Sim showed $0.

**Issue 2** (no separation): There was no portfolio that tracked only trades physically sent to the exchange, making it impossible to distinguish sim PnL from broker-confirmed PnL.

**Fix**:
- Added `broker_portfolio: Arc<Mutex<Portfolio>>` to `StrategySupervisor`. It is populated in `confirm_trade` only when `order_ids` contains at least one real (non-None) order ID — i.e., only when an actual REST call reached the exchange.
- `web.rs` always maps `live_portfolio` → `sim_payload` and `broker_portfolio` → `live_payload`, regardless of `dry_run`. Dry-run sessions show $0 live (broker_portfolio stays empty); live-trading sessions show only genuinely broker-confirmed fills in the Live panel.
