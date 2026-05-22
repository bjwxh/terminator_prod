# Terminator Rust — Phase 5 Code Review Fixes

---

## 🔴 Bug 1: `self.config.dry_run` Field Does Not Exist — Compile Error

**File**: [src/strategy.rs:378](src/strategy.rs#L378)

**Problem**: `StrategySupervisor::tick()` references `self.config.dry_run`, but `AppConfig` has no such field. This prevents the crate from compiling.

```rust
// strategy.rs:378 — compile error
match execute_trade(&self.execution_client, &account_hash, &trade, self.config.dry_run).await {
```

**Fix Option A** — Add `dry_run` to `AppConfig` and `.env`:

```rust
// config.rs — add field
pub struct AppConfig {
    // ...
    pub dry_run: bool,
}

// In AppConfig::load()
let dry_run = std::env::var("DRY_RUN")
    .ok()
    .and_then(|v| v.parse::<bool>().ok())
    .unwrap_or(true); // default safe: dry run enabled
```

```
# .env
DRY_RUN=true
```

**Fix Option B** — Hardcode `true` in `tick()` until the config field is added (temporary):

```rust
match execute_trade(&self.execution_client, &account_hash, &trade, true).await {
```

Option A is recommended — explicit config makes dry-run mode visible at startup.

---

## 🔴 Bug 2: Timezone Bug in `tick()` and `check_entry()` — Wrong Trading Hours on Non-CT Machines

**Files**: [src/strategy.rs:351](src/strategy.rs#L351), [src/strategy.rs:355-358](src/strategy.rs#L355-L358), [src/strategy.rs:147-148](src/strategy.rs#L147-L148)

**Problem**: This is the same class of bug fixed in `greeks.rs` (Phase 1–4 Bug 1). `tick()` calls `Local::now()` and compares the result against hardcoded `NaiveTime` values that are meant to represent Chicago (CT) trading hours. On a UTC server, 8:30 CT is 13:30 UTC, so the gate fires five hours late. `check_entry()` has the same issue with its delta decay window.

```rust
// strategy.rs:351 — Local::now() is machine-local, not CT
let now = Local::now();
let current_time = now.time();

let start_time = NaiveTime::from_hms_opt(8, 30, 0).unwrap(); // meant to be CT
let end_time   = NaiveTime::from_hms_opt(15, 0, 0).unwrap(); // meant to be CT

if current_time < start_time || current_time > end_time {
    return Ok(());
}
```

**Fix**: Use `chrono_tz::America::Chicago` exactly as `calculate_t_to_expiration()` already does.

```rust
// strategy.rs — tick()
use chrono::TimeZone;
use chrono_tz::America::Chicago;

let now_ct = Chicago.from_utc_datetime(&chrono::Utc::now().naive_utc());
let current_time = now_ct.time();

let start_time = NaiveTime::from_hms_opt(8, 30, 0).unwrap();
let end_time   = NaiveTime::from_hms_opt(15, 0, 0).unwrap();
if current_time < start_time || current_time > end_time {
    return Ok(());
}

// pass now_ct to check_entry — change its signature:
if let Some(trade) = check_entry(&self.grid, s, now_ct, 50.0) {
```

`check_entry` and `calculate_delta_decay` should also accept `DateTime<chrono_tz::Tz>` (or `DateTime<Utc>` converted to CT inside) instead of `DateTime<Local>`:

```rust
// strategy.rs — check_entry signature
pub fn check_entry(
    grid: &OptionsGrid,
    s: &SubStrategy,
    now: chrono::DateTime<chrono_tz::Tz>,  // was DateTime<Local>
    max_diff: f64,
) -> Option<Trade>

// calculate_delta_decay signature
pub fn calculate_delta_decay(
    now: chrono::DateTime<chrono_tz::Tz>,  // was DateTime<Local>
    init_leg_delta: f64,
    start_time: NaiveTime,
    end_time: NaiveTime,
) -> f64
```

The `SubStrategy.trade_start_time` comparison in `tick()` also uses `current_time` from `Local::now()`:

```rust
if s.state == StrategyState::Idle && current_time >= s.trade_start_time {
```

Once `current_time` is derived from `now_ct` (CT), this comparison is correct.

---

## 🟠 Bug 3: `process_account_event` Applies Events to All Active Sub-Strategies Without Order ID Matching

**File**: [src/strategy.rs:401-414](src/strategy.rs#L401-L414)

**Problem**: When an `OrderActivityEvent` arrives (fill, cancel), the code iterates all sub-strategies in `EnteringSpread` or `Working` state and applies the status change to every one of them. If two strategies have open orders simultaneously, a single fill event incorrectly transitions both to `Working`.

```rust
// strategy.rs:401-414 — no order_id matching
for (sid, s) in strats.iter_mut() {
    if s.state == StrategyState::EnteringSpread || s.state == StrategyState::Working {
        if event.status == "Filled" {
            s.state = StrategyState::Working; // applied to ALL matching strategies
        }
    }
}
```

**Fix**: Add `active_order_id: Option<String>` to `SubStrategy`, set it when `place_order` returns, and match on it in `process_account_event`:

```rust
// strategy.rs — SubStrategy struct
pub struct SubStrategy {
    pub sid: String,
    pub trade_start_time: NaiveTime,
    pub has_traded_today: bool,
    pub state: StrategyState,
    pub unit_size: i32,
    pub init_s_delta: f64,
    pub init_l_delta: f64,
    pub active_order_id: Option<String>,  // add this
}

// In tick(), after successful place_order:
Ok(order_id) => {
    s.state = StrategyState::Working;
    s.has_traded_today = true;
    s.active_order_id = order_id;  // store returned order ID
}

// In process_account_event():
for (sid, s) in strats.iter_mut() {
    let is_our_order = s.active_order_id.as_deref() == Some(&event.order_id);
    if !is_our_order {
        continue;
    }
    if event.status == "Filled" {
        s.state = StrategyState::Working;
    } else if event.status == "Cancelled" {
        s.state = StrategyState::Idle;
        s.has_traded_today = false;
        s.active_order_id = None;
    }
}
```

---

## 🟠 Bug 4: No Position Reconciliation at Startup — Risks Double-Entry After Restart

**File**: [src/strategy.rs:323-347](src/strategy.rs#L323-L347)

**Problem**: `has_traded_today` resets to `false` on every process start. If the engine restarts mid-session after trades have already been placed, the supervisor will attempt to enter positions again on top of existing open legs. `ExecutionClient::get_live_positions()` exists but is never called during supervisor initialization.

**Fix**: After resolving the account hash in `run_supervisor_loop`, fetch live positions and mark any sub-strategy whose strike range overlaps an existing open position as already traded:

```rust
// run_supervisor_loop — after resolving account hash
match self.execution_client.get_live_positions(&hash).await {
    Ok(positions) if !positions.is_empty() => {
        warn!("Found {} existing open options positions at startup. Marking affected strategies as already traded.", positions.len());
        let mut strats = self.sub_strategies.lock().await;
        for (_, s) in strats.iter_mut() {
            // If any open position exists, conservatively treat this strategy as working
            s.has_traded_today = true;
            s.state = StrategyState::Working;
        }
    }
    Ok(_) => {
        info!("No open positions found at startup. Supervisor starting fresh.");
    }
    Err(e) => {
        warn!("Could not verify open positions at startup: {:?}. Proceeding with fresh state.", e);
    }
}
```

A more precise implementation would match positions by symbol to individual sub-strategies, but the conservative blanket-mark is safe for now.

---

## 🟡 Issue 5: `commission` Does Not Scale With `unit_size`

**File**: [src/strategy.rs:213](src/strategy.rs#L213)

**Problem**: Commission is `1.13 * legs.len()` = $4.52 regardless of `unit_size`. At `unit_size > 1`, the commission figure shown in logs and `Trade` records is understated.

```rust
let commission = 1.13 * legs.len() as f64; // always $4.52 for 4-leg condor
```

**Fix**:

```rust
let commission = 1.13 * legs.len() as f64 * s.unit_size as f64;
```

---

## 🟡 Issue 6: Duplicate OCC Symbol Parsing Logic

**Files**: [src/strategy.rs:267-276](src/strategy.rs#L267-L276), [src/execution.rs:207-225](src/execution.rs#L207-L225)

**Problem**: `parse_strike_from_symbol()` in strategy.rs and `parse_schwab_symbol()` in execution.rs both parse the OCC-format symbol string (last 8 chars = strike ×1000, preceding char = C/P). Duplicating this is a maintenance hazard if Schwab ever changes the format.

**Fix**: Move the parsing logic to a shared utility — either in a new `src/occ.rs` module or in `parser.rs` as a `pub fn` — and import it from both callers. The execution.rs version (`parse_schwab_symbol`) is the more complete one (extracts both strike and side), so base the shared function on that.

---

## Summary

| # | Severity | File | Issue |
|---|----------|------|-------|
| 1 | 🔴 Critical | `strategy.rs:378` | `self.config.dry_run` field missing from `AppConfig` — compile error |
| 2 | 🔴 Critical | `strategy.rs:351,147` | `Local::now()` used for CT trading hours gate — wrong on non-CT machines |
| 3 | 🟠 Medium | `strategy.rs:401` | Account events applied to all active strategies — missing order ID matching |
| 4 | 🟠 Medium | `strategy.rs:323` | No startup position reconciliation — double-entry risk after restart |
| 5 | 🟡 Minor | `strategy.rs:213` | `commission` doesn't scale with `unit_size` |
| 6 | 🟡 Minor | `strategy.rs:267`, `execution.rs:207` | OCC symbol parsing duplicated in two files |
