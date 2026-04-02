# Trade Execution Security & UI Fixes (2026-04-02)

This document summarizes the investigation and resolution of the trade execution issues encountered on 2026-04-02 regarding incorrect $0.00 limit prices and missing UI confirmation windows.

## 1. Issue: Call Spread Sent at $0.00 Limit
### Root Cause: Persistent Price Override Leak
The `price_overrides` dictionary in the backend, used to store manual price adjustments from the GUI, was keyed by the `strategy_id`. For all reconciliation trades, this ID was statically set to `GAP_SYNC`.
- **The Bug**: Overrides were never cleared after a trade was filled, failed, or cancelled.
- **The Collision**: A manual override of **$7.65 (Credit)** from an earlier Iron Condor persisted in memory. When a new **Call Spread (Debit)** appeared with the same `GAP_SYNC` ID, the backend tried to apply the $7.65 credit to a debit structure.
- **The Safety Clamp**: The execution logic `max(0.0, -override)` correctly prevented a catastrophic "buy at credit" order by clamping the resulting value to **$0.00**.

### Solution
- **Self-Cleaning Overrides**: Modified `server/core/monitor.py` to automatically `pop` (delete) any overrides for a `strategy_id` immediately after the trade execution phase completes.
- **Price Sanity Warnings**: Added a logger warning to detect if a trade is being submitted at $0.00 when the market mid-price is significantly higher (> $0.05).

## 2. Issue: Missing Order Confirmation Modal
### Root Cause: Overlapping/Blocked UI Signals
- **The Bug**: The frontend `app.js` contained a defensive check that ignored new `trade_signal` events if a modal was already visible and "Paused." 
- **The Stale Context**: If a previous trade reached a timeout or was auto-executed by the server, or if the user had left the tab in a state where a modal was hidden but still logically "open," new signals were discarded.
- **Browser Behavior**: While the chime (audio) would play, the browser prohibits background tabs from forcing themselves to the foreground.

### Solution
- **UI Force-Refresh**: Updated `app.js` to always call `closeTradeModal()` before showing a new signal. This clears the DOM state, resets timers, and ensures the freshest trade data handles the UI interaction.
- **Unique Recon IDs**: Reconciliation trades now use unique IDs (e.g., `GAP_SYNC_093139`) instead of a static string. This provides better log tracing and further prevents cross-trade state contamination.

## Implementation Details
- **Backend**: `server/core/monitor.py` modified to handle `GAP_SYNC` uniqueness and override purging.
- **Frontend**: `server/static/app.js` modified to prioritize the most recent trade signal.

---
*Status: Implemented and Deployed (2026-04-02)*
