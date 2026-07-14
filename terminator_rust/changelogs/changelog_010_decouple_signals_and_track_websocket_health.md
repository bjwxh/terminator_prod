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
