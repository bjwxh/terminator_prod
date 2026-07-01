# Changelog 003: Schwab OrderFillCompleted & Live Portfolio Fixes

## Schwab Account Activity Parser & Live Portfolio Fixes
*   **OrderFillCompleted Parsing**: Added support for mapping the `OrderFillCompleted` message type to the `Filled` status in `parser.rs`. Previously, this fell through to `Unknown`, preventing downstream actions.
*   **Live Portfolio Fast-Sync**: The change ensures that when Schwab returns an `OrderFillCompleted` event (which often occurs without a paired `ExecutionCreated` event for some account configurations or order types), the Strategy Supervisor correctly triggers the REST API fast-sync (`get_today_filled_orders`).
*   **Trades & Cash Update**: This fixes the issue where `broker_portfolio` (the live portfolio) trades stayed empty ("No recent trades") and `cash` stayed at `$0.00` instead of recording the fill credit (e.g. `$650.00` for a `$6.50` iron condor credit), which previously caused the live gross/net PnL to display incorrectly as the raw position mark-to-market value (`-$652.50` instead of `-$2.50`).
*   **Queue Dequeue**: Ensures the execution queue is immediately cleaned of filled prerequisite orders when the `OrderFillCompleted` event arrives.

## Asynchronous Fast-Sync Optimization
*   **Non-Blocking REST Operations**: Offloaded `get_working_orders` and `get_today_filled_orders` REST API HTTP requests inside `process_account_event` to background tokio tasks via `tokio::spawn`. This prevents the slow Schwab HTTP requests (which can take 1-2 seconds) from blocking the main WebSocket account activity event parsing loop.
*   **Reduced UI Latency**: Resolves the delay (1-2 seconds) in displaying live Trades and PnL updates on the web UI after an order fills, since WebSocket event dispatch and state snapshot broadcasts are no longer serialized behind slow HTTP calls.

