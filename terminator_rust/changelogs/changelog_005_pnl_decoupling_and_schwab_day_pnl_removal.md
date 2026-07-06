# Changelog 005: PnL Decoupling & Schwab Day PnL Removal

## Decoupled PnL from Schwab API
*   **Removed API Day PnL Dependencies**: Completely eliminated reliance on Schwab's `current_day_pnl` and `prev_close` which suffered from endpoint lag, dropped closed positions, and aggregated PnL fields for reopened lots.
*   **Dynamic MTM & Internal Trade ledger**: 
    *   Rebuilt `gross_pnl()` dynamically as `self.cash + open_value`, utilizing our internal trade cash ledger (`self.cash`) which is reconstructed accurately from `get_today_filled_orders` upon app restarts.
    *   Rebuilt `unrealized_pnl()` using dynamic mark-to-market (MTM) calculations: `(p.price - p.entry_price) * p.quantity * 100.0` for all open positions.
    *   Determined `realized_pnl()` strictly using the accounting identity: `self.net_pnl() - self.unrealized_pnl()`.
*   **Protected Internal Cost Basis**: Updated `sync_from_broker` to stop overwriting existing positions' `entry_price` with Schwab's `avg_price`. Instead, we preserve our internal cost basis calculated during `add_trade`, only falling back to Schwab's `avg_price` when initializing a newly discovered position (e.g. manual trade).

## Web UI Compatibility
*   **Calculated MTM in Web Server**: Updated `web.rs` to dynamically compute `mtm_pnl` as `(p.price - p.entry_price) * p.quantity * 100.0` for JSON payloads sent to the frontend, fully replacing the deprecated `current_day_pnl` field.
