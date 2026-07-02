# Changelog 004: REST Fast-Sync Coalescing & Rebalance Unblocking Fixes

## Asynchronous Fast-Sync Queue & Coalescing (Race Condition Mitigation)
*   **MPSC Channel Queue**: Replaced the parallel task spawning model with a serialized background worker loop (`run_fast_sync_loop`) listening on an MPSC channel (`fast_sync_tx` and `fast_sync_rx`). This ensures all REST fast-sync operations run sequentially in order, completely eliminating the risk of network-jitter-induced out-of-order state overwrites.
*   **Debouncing & Coalescing**: Added a 50ms sleep debounce inside the worker task. Rapidly arriving WebSocket activity events (such as leg-by-leg fills) are now coalesced into a single optimized REST API sync, significantly reducing unnecessary Schwab API requests.

## Rebalance Unblocking via REST Fills
*   **Sub-Strategy Reconciliation**: Implemented `apply_broker_fills_to_strategies` to match REST-synced filled trades back to pending sub-strategies. This resolves the issue where sub-strategies remained permanently stuck in the `previous_portfolio` pending state when Schwab WebSocket streams emitted zero-leg `OrderFillCompleted` events, which previously prevented any subsequent rebalance actions.
*   **Trades List Preservation**: Updated `check_and_finalize_fill` to copy the `trades` list from `previous_portfolio` into the main `portfolio` upon fill finalization. This retains filled trades (with order-id metadata) permanently inside the sub-strategy, enabling the deduplication logic to ignore already-allocated fills on subsequent sync cycles.
*   **Option Grid Lookups**: Looked up live mid-price, delta, and theta values from the options grid `self.grid` during fill allocation, bypassing the default `0.0` prices returned by the Schwab REST orders API to ensure accurate premium, cost basis, and Greek calculations.
*   **Leg Grouping in Sim Trades**: Aggregated allocated trade legs on a per-sub-strategy basis before constructing and adding the `Trade` object. This ensures that multi-leg orders (such as iron condors) are displayed as a single grouped order in the Sim Trades UI table, matching the display behavior of Live Trades.

## Working Orders Mark Price Fix
*   **Real-time Combo Mark Pricing**: Replaced the hardcoded `null` value for working order mark prices in `web.rs` with a dynamic calculation. The backend now looks up the current streaming WebSocket mid prices from the options grid `state.grid.quotes` for each active leg of a working order, computing the exact net-mid price of the combo.
*   **Correct Debit/Credit Signage**: Set the mark price value to `-net_flow` (net debit) to perfectly align with the UI's parsing logic (where negative is rendered as `Cr` and positive as `Db`), restoring accurate real-time mark tracking (e.g. `$3.80 Db` or `$1.30 Cr` instead of `--`) for working orders.

## Live PnL Calibration & Trade Filtering Fix
*   **Prevent Historical Trade Cash Pollution**: Modified `get_today_filled_orders` in `execution.rs` to look back 24 hours prior to the start of today (to capture yesterday-entered orders filled today), but introduced a strict `closeTime` filter that discards any orders closed/filled before today's start of day (Chicago time). This prevents previous-day filled trade credits/debits from polluting today's Live Portfolio cash calculation, restoring correct PnL matching with the broker.
*   **Startup State Reconciliation (Stale previous_portfolio Reset)**: Added code to clear `previous_portfolio`, `snapshot_trade_count`, and `cancelled_at` fields in all sub-strategies during the bootstrap reset at startup. This prevents unconfirmed optimistic updates from a prior session/day from lingering in memory, which previously caused the reconciliation matching logic to try to "close" yesterday's positions and pollute active day calculations.

## Stale Working Orders (Ghost Orders) Fix
*   **Periodic 30-Second working_orders Refresh**: Added a periodic 30-second trigger in the Strategy Supervisor tick loop that sends a non-full sync signal to `fast_sync_tx`. This periodically polls the Schwab `get_working_orders` REST API to refresh the active orders list, preventing filled/canceled orders from getting permanently stuck in the UI as "ghost" working orders due to Schwab REST API DB propagation lag when WebSocket-triggered syncs execute too quickly.

## Interactive Trade Confirmation Audio Notification
*   **Web Audio Synthesis Chime**: Implemented `playNotificationSound()` in `app.js` using the standard browser Web Audio API. It synthesizes a clean, high-fidelity, two-tone double chime (880Hz sine wave for 0.4s, followed by 1100Hz for 0.5s with linear and exponential volume ramps).
*   **Chime on Modal Popup**: Triggered this play call whenever `showTradeModal()` is invoked (indicating a new order confirmation window has popped up), prompting the user to act without requiring external audio files.
*   **Audio Context Leak & Mute Fix**: Hoisted a single `notificationAudioCtx` to module scope (lazily initialized on first tone, and calling `.resume()` if suspended) to prevent browser AudioContext exhaustion cap limits. Made it strictly respect the existing `isMuted` localStorage/UI setting to avoid playing sounds when the app is muted, and removed the duplicate `playSound('alert')` call from the WebSocket trade signal handler to prevent jarring double-chimes.

## Live PnL Order Flattening & Unique Deduplication Key Fix
*   **Order Flattening in get_today_filled_orders**: Hoisted `flatten_orders` to module scope in `execution.rs` and integrated it into `get_today_filled_orders`. This recursively processes nested child strategies under `childOrderStrategies` (the standard Schwab API container structure for complex orders like Iron Condors), preventing the parser from silently skipping filled trades and restoring correct live cash and Live PnL values.
*   **Unique Leaf OrderId Deduplication**: Reverted the trade `strategy_id` key back to the unique leaf `orderId` to prevent deduplication collisions inside `apply_broker_fills_to_strategies` (which would have caused subsequent sibling child fills under the same parent to be skipped and permanently dropped).
*   **Unreachable Dead Code & Dead Variable Cleanup**: Removed the dead `strat_type == "FLATTEN"` branch inside `flatten_orders` (since `"FLATTEN"` is our app's internal purpose string rather than a Schwab API strategy value). Also completely removed the pre-existing but unused `_parent_order_id` insertion on leaf orders and stripped out the redundant `parent_order_id` argument tracking from `flatten_orders` to keep the codebase clean and surgical.

## Simplified Today-Only Daily PnL Calculation
*   **Focus on Today Only**: Removed all complex carryover tracking, yesterday's close price reconstructions (`prev_close`), and baseline starting market values (`starting_market_value`). Since the strategy trades 0DTE SPX options that expire daily, carryover values are treated as $0.0$ and we focus strictly on today's session performance.
*   **Today's PnL Formulas**: 
    *   `net_pnl()` is simply $\text{Current Open Position MV} + \text{Today's Cash Flow} - \text{Fees}$.
    *   `unrealized_pnl()` is $\sum (\text{price} - \text{entry\_price}) \times \text{quantity} \times 100$.
    *   `realized_pnl()` is $\text{Today's Cash Flow} - \text{Fees}$.
*   This keeps the codebase minimal, clean, and 100% correct for today's trading.



