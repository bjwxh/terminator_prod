# Changelog 010: Decoupled Signal Evaluation and WebSocket Health Tracking

## Changes

### 1. Decoupled Signal Evaluation Ticker
- Extracted entry/exit/rebalance strategy signal checks from the slow 5s `tick()` loop into a public method `StrategySupervisor::evaluate_signals`.
- Created a background `run_signal_loop()` task running every **500ms** to execute `evaluate_signals()`, resulting in a 10x signal latency improvement while capping CPU and locking overhead.

### 2. WebSocket Health Tracking
- Added `last_stream_frame_at` atomic tracker to `WebsocketClient` to record the timestamp of *any* received frame (including Schwab's heartbeats and unhandled NOTIFY messages).
- Exposed a thread-safe `is_healthy() -> bool` check (15s threshold) on `WebsocketClient`.
- Wired `heartbeat_failures` in `StrategySupervisor` to increment when the WebSocket stream goes unhealthy.

### 3. Conditional Position Reconciliation
- Configured the 5s `tick()` loop to run REST-based `check_reconciliation` only if the stream is unhealthy (every 5s) or if the stream is healthy but ≥60 seconds have elapsed since the last reconciliation pass (safety net).
- Forces one immediate reconciliation resync pass upon WebSocket stream recovery before returning to the 60s relaxed poll.
- Bypassed the 60s reconciliation cooldown in test environments (`TERMINATOR_TEST_ENV`) to ensure immediate test evaluations.

### 4. Integrated Ticker Wiring
- Modified `StrategySupervisor::new` to accept `Option<Arc<WebsocketClient>>` for health checking.
- Updated `src/main.rs` to wire the WebSocket client into the supervisor and spawn the new `run_signal_loop` task.
- Updated `tests/strategy_tests.rs` to use the new constructor signature and explicitly call `evaluate_signals()` in tests.

### 5. Schwab Timestamp Parsing Fix
- Implemented `parse_schwab_timestamp` helper to gracefully handle Schwab ISO8601 timestamps that have missing colons in their timezone offset (e.g. `+0000`).
- Updated the soft bootstrap matching loops in `bootstrap_from_history` to use this helper, resolving a bug where live trades were silently skipped during bootstrap.

### 6. Concurrent Bootstrap Race Condition Fix
- Added `bootstrap_complete` atomic boolean flag to `StrategySupervisor`.
- Gated the `evaluate_signals` ticker to exit early if bootstrap is still active (`!bootstrap_complete`). This prevents the fast background ticker task from racing with and preempting the bootstrap snapshot replay (which previously caused sub-strategies to prematurely transition to missed/skipped states during replay).
- Updated remaining Schwab timestamp parsing calls in `bootstrap_from_history` to use `parse_schwab_timestamp` helper.

### 7. Immediate Trade Reconciliation & Delayed Email Alerts
- **Immediate Signaling & Cooldown**: Added MPSC channel signaling (`reconcile_tx` / `reconcile_rx`) and an atomic `force_reconciliation` flag to `StrategySupervisor`.
- **Supervisor Wakeup**: Modified the 5-second sleep in `run_supervisor_loop()` to wait on both the sleep timer and `reconcile_rx` using `tokio::select!`. This wakes up the supervisor loop immediately when a simulated trade is registered in `evaluate_signals()`.
- **Bypassing the Cooldown**: Configured the healthy-stream branch of `should_reconcile` in `tick()` to intercept and clear the `force_reconciliation` flag, triggering an immediate reconciliation pass and updating `last_reconciled_at` to the current time, which resets the 60-second cooldown timer.
- **Configurable Email Alert Delay**: Added `email_alert_delay_seconds` configuration parameter to `config.json` and parsed it inside `config.rs`.
- **Email Alert Suppression**: Updated `check_reconciliation()` to delay spawning the email script by the configured delay duration. After the delay, the task borrows `pending_trade` using `.as_ref()` and compares the pending trade's unique timestamp string against the proposed trade's timestamp. If the user has confirmed or dismissed the trade (clearing the option), the email alert is successfully suppressed.
- **Unit Testing**: Added the `test_immediate_reconciliation_trigger` integration test to `tests/strategy_tests.rs` to verify the signaling and immediate reconciliation trigger flow.

