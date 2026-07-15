# Changelog 011: Configurable Option Quote Freshness Threshold

## Changes

### 1. Configurable Quote Freshness Threshold
- Added `stale_quote_threshold_secs` parameter to `config.json` (set to `600` seconds to effectively disable the stale quote guard under ordinary trading conditions).
- Updated `config.rs` (`ConfigJson`, `AppConfig`, `AppConfig::load()`, and `impl Default for AppConfig`) to parse and expose `stale_quote_threshold_secs` with a default value of `60` seconds.

### 2. Core Option Selection Integration
- Modified `find_closest_option` inside `strategy.rs` to accept `stale_quote_threshold: Option<Duration>`. If `None` is provided, it defaults to the old `5` seconds fallback to preserve backward compatibility.
- Modified `check_entry` inside `strategy.rs` to accept `stale_quote_threshold: Option<Duration>` and propagate it.
- Updated `check_entry` calls inside the bootstrap and live path loops in `StrategySupervisor` to pass `Some(Duration::from_secs(self.config.stale_quote_threshold_secs))`.
- Updated `find_closest_option` calls inside rebalance helpers (`create_new_spread_trade`, `create_rebalance_short`, `create_rebalance_long`) to load the value from `config.stale_quote_threshold_secs`.

### 3. Backtest & Test Suite Compatibility
- Updated `check_entry` calls to pass `None` in `backtest_today.rs`, `opt_sim.rs`, and `run_rust_backtest.rs`, keeping their behavior identical for static datasets.
- Updated local `ConfigJson` and `app_cfg` initialization in `backtest_today.rs` to support `stale_quote_threshold_secs`.
- Updated unit tests in `strategy_tests.rs` to pass `None` to `find_closest_option` and `check_entry` calls.
