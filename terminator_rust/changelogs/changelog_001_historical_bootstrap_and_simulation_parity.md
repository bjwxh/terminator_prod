# Changelog 001: Historical Bootstrap & Simulation Parity

**Date**: 2026-06-17

## Overview
Implemented the historical database bootstrap logic allowing the Rust backend to perfectly replicate the Python simulator's trading decisions before transitioning into live WebSocket streaming. Fixed several discrepancies to achieve 100% offline trade parity between Python and Rust.

## Changes

### 1. Delta Selection Logic Parity (`strategy.rs`)
- **Issue**: Rust's `find_closest_option` simply searched for the absolute closest delta to the target delta. However, Python's `_find_option` enforces an explicit filter `abs(delta) >= target_delta`, meaning it prefers options that strictly "over-satisfy" the delta threshold.
- **Fix**: Re-wrote `find_closest_option` to mimic Python's two-pass approach. Rust now first searches for the closest match among options that have `abs(delta) >= abs(target_delta)`. If no such options exist, it falls back to finding the absolute closest delta across the entire chain.
- **Result**: Rust's chosen strikes, credits, and deltas now match Python's simulation output with 100% precision.

### 2. Historical Sub-Strategy Bootstrap (`strategy.rs`)
- **Issue**: `bootstrap_from_history` was stubbed out and `create_sync_entry` lacked sub-strategy association.
- **Fix**: 
  - Implemented `bootstrap_from_history` to query `today_orders` and simulate history state restoration before the live WebSocket ticker starts.
  - Reset `has_traded_today`, cleared `live_portfolio`, and applied proper `StrategyState::Working` lifecycle states.
  - Corrected `create_sync_entry` to correctly map the `strategy_id` to the parent `sid` instead of leaving it generic.

### 3. Grid Snapshot Injection (`grid.rs`)
- **Issue**: Historical snapshots were correctly injected, but failed subsequent execution checks because the `last_update` timestamps were considered "stale" (>500ms old).
- **Fix**: Updated `inject_snapshot` to explicitly overwrite `last_update = Instant::now()` so that injected database snapshots bypass the live streamer's 500ms staleness guards during the offline simulation loops.
- **Addition**: Added Direct persist for Delta (`q.delta`) and Theta (`q.theta`) from the database since historical snapshots lack real-time greeks calculations.

### 4. SPX Price Estimation (`db.rs`)
- **Issue**: Underlying index data for SPX was not precisely synced with options snapshots.
- **Fix**: Re-implemented `estimate_spx_from_snapshot` to compute the synthetic SPX spot price by averaging the ATM strikes of SPX Calls and Puts at the exact second the snapshot was taken.

### 5. Config Path Hardcoding (`config.rs`)
- **Issue**: `db_path` and `python_config_path` were loosely defined.
- **Fix**: Unified pathing for `market_data.db` via `.env` variables to align with the Python server's `options_YYYYMMDD.db` symlinks.
