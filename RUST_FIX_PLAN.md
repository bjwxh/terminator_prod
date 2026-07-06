# Rust App — Comprehensive Fix Plan

**Date:** 2026-06-17  
**Branch:** `rust`  
**Severity:** Critical — Rust app does not rebalance, does not exit, sends wrong Schwab instructions for non-entry orders, and has no runtime config for trading parameters.

Supersedes `RUST_REBALANCE_FIX.md` (which had several inaccuracies — see Notes inline).

---

## Root Cause Summary

The Python monitor is a three-branch state machine (entry → rebalance → exit). The Rust `tick()` only implements the entry branch. Everything else is missing at the structural level: no per-strategy portfolio, no supervisor-level portfolio, no trading config parameters in `AppConfig`, no close instructions for exit/rebalance orders, and sub-strategy parameters hardcoded rather than read from `config.json`.

---

## Issues

### CRITICAL

#### Issue 1 — No rebalance or exit logic in `tick()`
**File:** [strategy.rs:396-418](terminator_rust/src/strategy.rs#L396-L418)

Once a sub-strategy enters `Working` state it is skipped on every subsequent 5-second tick. The `StrategyState::Exiting` enum variant exists but is unreachable. Python's `_monitor_step()` runs three branches every 30 s:

```python
if not s.has_traded_today and t_time < end_time_obj:
    trade = self._check_entry(s, snap, ts)
elif s.has_traded_today:
    if t_time >= end_time_obj and s.portfolio.positions:
        trade = self._create_exit_trade(...)
    elif t_time < end_time_obj:
        trades = self._check_rebalance(...)
```

Rust `tick()` has only:
```rust
if s.state == StrategyState::Idle && current_time >= s.trade_start_time {
    // entry only
}
// Working state → nothing
```

#### Issue 2 — `SubStrategy` has no `portfolio` field
**File:** [strategy.rs:46-55](terminator_rust/src/strategy.rs#L46-L55)

Python `SubStrategy` carries `portfolio: Portfolio` tracking each strategy's positions independently. Rust `SubStrategy` has no such field. Rebalance and exit logic cannot be written without it.

#### Issue 3 — `StrategySupervisor` has no portfolio
**File:** [strategy.rs:276-282](terminator_rust/src/strategy.rs#L276-L282)

Python has `self.combined_portfolio` and `self.live_combined_portfolio`. Rust has neither. There is no aggregated position state anywhere in the app.

#### Issue 4 — `AppConfig` is missing all trading parameters
**File:** [config.rs](terminator_rust/src/config.rs)

`AppConfig` only holds Schwab credentials, `otm_offset`, `buffer_zone`, and `dry_run`. Every trading config field Python reads at runtime is absent:

| Python key | Default | Rust status |
|---|---|---|
| `initial_sum_delta` | 0.35 | ❌ missing — `init_s_delta` hardcoded as 0.175 |
| `init_wing_delta` | 0.16 | ❌ missing — `init_l_delta` hardcoded as 0.025 |
| `rebalance_threshold` | 0.075 | ❌ missing |
| `long_leg_rebalance_delta_threshold` | 0.13 | ❌ missing |
| `min_credit` | 0.05 | ❌ missing |
| `max_spread_diff` | 50.0 | ❌ hardcoded as literal 50.0 in `check_entry` |
| `commission_per_contract` | 1.13 | ❌ hardcoded as literal 1.13 in `check_entry` |
| `order_offset` | 0.15 | ❌ missing entirely |
| `stale_guard_min_price` | 0.20 | ❌ missing |
| `portfolio_start_time` | 09:01:00 | ❌ hardcoded |
| `portfolio_end_time` | 10:31:00 | ❌ hardcoded |
| `portfolio_interval_minutes` | 30 | ❌ hardcoded |
| `default_unit_size` | 1 | ❌ hardcoded as 1 |

> **Note:** The old fix doc (`RUST_REBALANCE_FIX.md`) claims "rebalance_threshold and long_leg_rebalance_delta_threshold already parsed" — **this is incorrect**. Neither field exists in `config.rs`.

#### Issue 5 — `execute_trade()` always sends OPEN instructions
**File:** [strategy.rs:243](terminator_rust/src/strategy.rs#L243)

```rust
let inst = if leg.quantity > 0 { "BUY_TO_OPEN" } else { "SELL_TO_OPEN" };
```

Exit and rebalance trades close existing positions and must use `BUY_TO_CLOSE` / `SELL_TO_CLOSE`. Schwab will reject OPEN instructions on positions that are already short/long. Python correctly selects instruction based on `trade.purpose`:

```python
if leg.quantity > 0:
    inst = "BUY_TO_CLOSE" if trade.purpose == TradePurpose.EXIT else "BUY_TO_OPEN"
else:
    inst = "SELL_TO_CLOSE" if trade.purpose == TradePurpose.EXIT else "SELL_TO_OPEN"
```

---

### HIGH

#### Issue 6 — Sub-strategy list is hardcoded
**File:** [strategy.rs:291-303](terminator_rust/src/strategy.rs#L291-L303)

Rust hardcodes exactly four strategies at 9:01, 9:31, 10:01, 10:31. Python generates the list dynamically from `portfolio_start_time`, `portfolio_end_time`, `portfolio_interval_minutes`. The git log already shows `config: reduce portfolio_end_time to 10:03:00` — any such change silently diverges from the Rust hardcoded list.

#### Issue 7 — Sub-strategy delta targets are hardcoded
**File:** [strategy.rs:302](terminator_rust/src/strategy.rs#L302)

```rust
let s = SubStrategy::new(sid.clone(), t, 0.175, 0.025, 1);
```

Python derives:
```python
s.init_s_delta = config['initial_sum_delta'] / 2          # 0.35/2 = 0.175
s.init_l_delta = max(initial_sum_delta/2 - init_wing_delta, 0.025)  # max(0.015, 0.025) = 0.025
s.unit_size = config.get('default_unit_size', 1)
```

Currently these happen to match because the default config produces the same values, but any config change to `initial_sum_delta` or `init_wing_delta` will silently diverge.

#### Issue 8 — Delta computed via Black-Scholes, not Schwab stream
**File:** [grid.rs:80,87](terminator_rust/src/grid.rs#L80) and [greeks.rs](terminator_rust/src/greeks.rs)

Rust derives delta by inverting IV from mid-price then applying BS delta. Python uses Schwab's streamed `delta` field. These differ — especially intraday as skew and term structure move — meaning Rust and Python will select different strikes for the same delta target.

This is a deeper issue requiring either: (a) streaming greeks from Schwab's options chain feed instead of computing them, or (b) accepting this as a known approximation difference. **Document as known divergence; defer to a separate issue.**

#### Issue 9 — No `order_offset` applied to limit price
**File:** [strategy.rs:255](terminator_rust/src/strategy.rs#L255)

Python adds `order_offset: 0.15` to the mid-price before submitting:
```python
signed_target = signed_mid + offset   # mid + 0.15
```

Rust sends the raw mid price. This makes Rust limit orders 15¢/spread less aggressive and materially reduces fill rate versus Python.

#### Issue 10 — No tick rounding on limit prices
**File:** [strategy.rs:255](terminator_rust/src/strategy.rs#L255)

Python rounds limit prices to `$0.05` increments for multi-leg spreads:
```python
tick = 0.05  # spreads always use $0.05 ticks
ticked_price = round(round(price / tick) * tick, 2)
```

Rust sends raw floating-point prices (e.g. `"1.2370001"`). Schwab may reject these.

#### Issue 11 — `reconcile_startup_positions` blanket-marks all strategies as Working
**File:** [strategy.rs:357-368](terminator_rust/src/strategy.rs#L357-L368)

If the app restarts mid-session with positions open for only one sub-strategy, all four are marked `Working`. Python reconciliation matches open positions to specific sub-strategies by symbol/strike. The Rust implementation will prevent entry for sub-strategies that have no open positions.

---

### MEDIUM

#### Issue 12 — Delta decay is linear-only
**File:** [strategy.rs:73-91](terminator_rust/src/strategy.rs#L73-L91)

Python uses a power-law curve (from `spx_0dte_delta_decay_power_law.json`) when `init_leg_delta > 0.20`, falling back to linear for `<= 0.20`. The current `init_s_delta = 0.175` is in the linear range so this causes no divergence today, but raising the short delta target above 0.20 will break silently.

#### Issue 13 — `min_credit` filter applied to wrong trade types (fix doc error)
**File:** `RUST_REBALANCE_FIX.md` Step 2 pseudocode

Python only applies `if abs(credit) <= min_credit * 100: return None` inside `_create_rebalance_trade` when `is_short=True` (`REBALANCE_SHORT`). `REBALANCE_LONG` and `REBALANCE_NEW` have no credit filter. The old fix doc applies it uniformly to all rebalance types, which would suppress valid LONG and NEW rebalances.

#### Issue 14 — `run_historical_catchup()` referenced in fix doc does not exist
**File:** `RUST_REBALANCE_FIX.md` Step 5

The old doc references `run_historical_catchup()` at `strategy.rs:561`. The current `strategy.rs` is 446 lines and contains no such function. Step 5 of the old fix doc describes wiring into a function that does not exist. The historical replay path must be built from scratch when it is needed.

#### Issue 15 — Theta stored as mid price
**File:** [grid.rs:81,88](terminator_rust/src/grid.rs#L81)

```rust
call.theta = call.mid; // Schwab mid-price convention for 0DTE Theta
```

Theta is set to the mid-price, not the actual theta. Python uses streamed theta from Schwab. The theta values in portfolio snapshots and UI displays are wrong. **Low trading impact** (theta is not used in entry/rebalance decisions), but misleading in the web UI.

---

## Fix Plan

### Step 1 — Extend `AppConfig` to load all trading parameters

**File:** `terminator_rust/src/config.rs`

Add all trading fields to `AppConfig` and parse them from `config.json`. The `config.json` already exists and has the correct format (see `config.example.json`).

```rust
pub struct AppConfig {
    // Schwab credentials (existing)
    pub schwab_token_path: PathBuf,
    pub schwab_account: String,
    pub schwab_api_key: String,
    pub schwab_api_secret: String,
    pub schwab_callback_url: String,
    pub dry_run: bool,

    // Trading parameters (NEW — loaded from config.json)
    pub initial_sum_delta: f64,        // 0.35
    pub init_wing_delta: f64,          // 0.16
    pub rebalance_threshold: f64,      // 0.075
    pub long_leg_rebalance_delta_threshold: f64, // 0.13
    pub min_credit: f64,               // 0.05
    pub max_spread_diff: f64,          // 50.0
    pub commission_per_contract: f64,  // 1.13
    pub order_offset: f64,             // 0.15
    pub stale_guard_min_price: f64,    // 0.20
    pub default_unit_size: i32,        // 1
    pub portfolio_start_time: NaiveTime,  // 09:01:00
    pub portfolio_end_time: NaiveTime,    // 10:31:00
    pub portfolio_interval_minutes: u32,  // 30
    pub start_time: NaiveTime,         // 08:30:00
    pub end_time: NaiveTime,           // 15:00:00
    pub otm_offset: f64,               // 250.0
    pub buffer_zone: f64,              // 10.0
    pub web_port: u16,                 // 8090
}
```

Parse from a JSON config file (path from env var `CONFIG_PATH`, defaulting to `config.json`). Remove all hardcoded literals in `strategy.rs` that duplicate these values.

---

### Step 2 — Add `portfolio` to `SubStrategy` and `StrategySupervisor`

**File:** `terminator_rust/src/strategy.rs`

```rust
pub struct SubStrategy {
    pub sid: String,
    pub trade_start_time: NaiveTime,
    pub has_traded_today: bool,
    pub state: StrategyState,
    pub unit_size: i32,
    pub init_s_delta: f64,
    pub init_l_delta: f64,
    pub active_order_id: Option<String>,
    pub portfolio: Arc<Mutex<Portfolio>>,   // NEW
}
```

Update `SubStrategy::new()` to accept `unit_size`, `init_s_delta`, `init_l_delta` from config (not hardcoded) and create a fresh `Portfolio`.

Add to `StrategySupervisor`:
```rust
pub live_portfolio: Arc<Mutex<Portfolio>>,  // aggregated across all strategies
```

Update `StrategySupervisor::new()` to derive the sub-strategy list dynamically from config:
```rust
let mut t = config.portfolio_start_time;
while t <= config.portfolio_end_time {
    let sid = format!("strat_{}", t.format("%H%M"));
    let init_s = config.initial_sum_delta / 2.0;
    let init_l = (init_s - config.init_wing_delta).max(0.025);
    sub_strategies.insert(sid.clone(), SubStrategy::new(sid, t, init_s, init_l, config.default_unit_size));
    t += Duration::from_secs(config.portfolio_interval_minutes as u64 * 60);
}
```

---

### Step 3 — Fix `execute_trade()`: instructions, order_offset, tick rounding

**File:** `terminator_rust/src/strategy.rs`

```rust
pub async fn execute_trade(
    client: &ExecutionClient,
    account_hash: &str,
    trade: &Trade,
    dry_run: bool,
    order_offset: f64,
) -> Result<Option<String>> {
    // ...
    for leg in &trade.legs {
        let is_closing = matches!(trade.purpose.as_str(), "EXIT" | "REBALANCE_SHORT" | "REBALANCE_LONG" | "REBALANCE_NEW");
        let inst = match (leg.quantity > 0, is_closing) {
            (true,  true)  => "BUY_TO_CLOSE",
            (true,  false) => "BUY_TO_OPEN",
            (false, true)  => "SELL_TO_CLOSE",
            (false, false) => "SELL_TO_OPEN",
        };
        // ...
    }

    // Credit per unit with offset, rounded to $0.05 tick
    let unit_qty = trade.legs[0].quantity.abs() as f64;
    let raw_mid = (trade.credit / (unit_qty * 100.0)).abs();
    let with_offset = raw_mid + order_offset;
    let ticked = (with_offset / 0.05).round() * 0.05;
    let price_str = format!("{:.2}", ticked);
    // ...
}
```

Pass `order_offset` from `self.config.order_offset` at call sites.

> **Note on REBALANCE_NEW:** A new spread opened on a flat side should use OPEN instructions, not CLOSE. The `is_closing` check above works because `REBALANCE_NEW` uses OPEN. Verify leg-level intent once `check_rebalance()` is implemented.

---

### Step 4 — Implement `get_condor_deltas()`

**File:** `terminator_rust/src/strategy.rs`

Returns the current absolute delta for each leg of the condor by scanning live portfolio positions. Uses signed delta×quantity (matching Python's `get_all_deltas`) then takes absolute value.

```rust
pub struct CondorDeltas {
    pub abs_short_call: f64,
    pub abs_long_call:  f64,
    pub abs_short_put:  f64,
    pub abs_long_put:   f64,
    pub short_call_strike: Option<f64>,
    pub long_call_strike:  Option<f64>,
    pub short_put_strike:  Option<f64>,
    pub long_put_strike:   Option<f64>,
}

pub fn get_condor_deltas(portfolio: &Portfolio) -> CondorDeltas {
    let mut d = CondorDeltas { abs_short_call: 0.0, abs_long_call: 0.0,
                               abs_short_put: 0.0,  abs_long_put: 0.0,
                               short_call_strike: None, long_call_strike: None,
                               short_put_strike: None,  long_put_strike: None };
    for p in &portfolio.positions {
        let signed = p.delta * p.quantity as f64; // negative for shorts
        match (p.side.as_str(), p.quantity < 0) {
            ("CALL", true)  => { d.abs_short_call = signed.abs(); d.short_call_strike = Some(p.strike); }
            ("CALL", false) => { d.abs_long_call  = signed.abs(); d.long_call_strike  = Some(p.strike); }
            ("PUT",  true)  => { d.abs_short_put  = signed.abs(); d.short_put_strike  = Some(p.strike); }
            ("PUT",  false) => { d.abs_long_put   = signed.abs(); d.long_put_strike   = Some(p.strike); }
            _ => {}
        }
    }
    d
}
```

This replaces the Option A / Option B choice from the old fix doc — strikes are derived on the fly, no struct migration needed.

---

### Step 5 — Implement `check_rebalance()`

**File:** `terminator_rust/src/strategy.rs`

Mirrors Python `_check_rebalance` + `_create_rebalance_trade` + `_create_new_spread_trade`.

```rust
pub fn check_rebalance(
    grid: &OptionsGrid,
    s: &SubStrategy,
    portfolio: &Portfolio,
    now: DateTime<Tz>,
    start_time: NaiveTime,
    end_time: NaiveTime,
    config: &AppConfig,
) -> Vec<Trade>
```

**Logic:**

```
t_short = calculate_delta_decay(now, s.init_s_delta, start_time, end_time)
t_long  = calculate_delta_decay(now, s.init_l_delta, start_time, end_time)
d = get_condor_deltas(portfolio)

for side in [CALL, PUT]:
    abs_short_delta = d.abs_short_{side}
    abs_long_delta  = d.abs_long_{side}

    sn_needs = |abs_short_delta - t_short| > config.rebalance_threshold
    ln_needs = |abs_long_delta  - t_long|  > config.long_leg_rebalance_delta_threshold

    // Width-cap override on long leg
    if !ln_needs {
        let (short_strike, long_strike) = match side {
            CALL => (d.short_call_strike, d.long_call_strike),
            PUT  => (d.short_put_strike,  d.long_put_strike),
        };
        if let (Some(ss), Some(ls)) = (short_strike, long_strike) {
            let width = if side == CALL { ls - ss } else { ss - ls };
            if width > config.max_spread_diff { ln_needs = true; }
        }
    }

    on_side = portfolio.positions filtered by side

    trade = if on_side.is_empty() && (sn_needs || ln_needs):
        create_new_spread_trade(grid, s, t_short, t_long, side, now, config)
    elif sn_needs:
        create_rebalance_short(grid, s, portfolio, t_short, t_long, side, now, config)
    elif ln_needs:
        create_rebalance_long(grid, s, portfolio, t_long, side, now, config)
    else:
        None

    if let Some(t) = trade { push to result }
```

**`create_rebalance_short()`:**
1. Find `old_short` in `portfolio.positions` (same side, qty < 0).
2. `new_short = find_closest_option(grid, t_short, is_call, None, None)?`
3. Legs: `[close old_short (+qty), open new_short (-qty)]`
4. Width-cap: if `|new_short.strike - old_long.strike| > max_spread_diff`, also roll long:
   - `[close old_long (-qty), open new_long at t_long (+qty)]` (up to 4 legs total)
5. `credit = sum(-l.quantity * l.price for l in legs) * 100`
6. `commission = commission_per_contract * legs.len()` (per leg, not per contract)
7. **Only** apply `min_credit` filter here: `if credit.abs() <= config.min_credit * 100 { return None }`
8. `purpose = "REBALANCE_SHORT"`

**`create_rebalance_long()`:**
1. Find `old_long` (same side, qty > 0). Find `short_strike` from positions (same side, qty < 0).
2. `new_long = find_closest_option(grid, t_long, is_call, Some(max_spread_diff), Some(short_strike))?`
3. Legs: `[close old_long (-qty), open new_long (+qty)]`
4. No `min_credit` filter (matches Python).
5. `purpose = "REBALANCE_LONG"`

**`create_new_spread_trade()`:**
1. `opt_s = find_closest_option(grid, t_short, is_call, None, None)?`
2. `opt_l = find_closest_option(grid, t_long, is_call, Some(max_spread_diff), Some(opt_s.strike))?`
3. Legs: `[-s.unit_size short, +s.unit_size long]`
4. No `min_credit` filter (matches Python).
5. `purpose = "REBALANCE_NEW"`

---

### Step 6 — Implement `check_exit()`

**File:** `terminator_rust/src/strategy.rs`

Mirrors Python `_create_exit_trade`. Needs the current SPX price to determine ITM/OTM.

```rust
pub fn check_exit(
    grid: &OptionsGrid,
    portfolio: &Portfolio,
    now: DateTime<Tz>,
    strategy_id: &str,
    commission_per_contract: f64,
    spx_price: f64,
) -> Option<Trade>
```

**Logic:**
```
for p in portfolio.positions:
    is_itm = (p.side == "CALL" && spx_price > p.strike)
          || (p.side == "PUT"  && spx_price < p.strike)

    exit_price = if is_itm {
        grid.quotes.get(p.strike).and_then(|q| q.call/put.mid).unwrap_or(p.price)
    } else {
        0.0    // OTM expires worthless
    }

    leg = OptionLeg { quantity: -p.quantity, price: exit_price, ... }

credit   = sum(-l.quantity * l.price for l in legs) * 100
commission = commission_per_contract * legs.len()
purpose  = "EXIT"
```

The current SPX price is available from `OptionsGrid` (stored during grid updates). Add `pub last_spx_price: AtomicF64` or read from the existing price field in the grid.

---

### Step 7 — Wire `tick()` to the full state machine

**File:** `terminator_rust/src/strategy.rs`

Replace the current loop body:

```rust
for (sid, s) in strats.iter_mut() {
    let end_time = self.config.end_time;
    let start_time = self.config.start_time;

    if s.state == StrategyState::Idle && current_time >= s.trade_start_time {
        // --- ENTRY (unchanged logic, but now uses config values) ---
        if let Some(trade) = check_entry(&self.grid, s, now_ct, self.config.max_spread_diff) {
            s.state = StrategyState::EnteringSpread;
            match execute_trade(&self.execution_client, &account_hash, &trade,
                                self.config.dry_run, self.config.order_offset).await {
                Ok(order_id) => {
                    s.state = StrategyState::Working;
                    s.has_traded_today = true;
                    s.active_order_id = order_id;
                    s.portfolio.lock().await.add_trade(&trade, None);
                    self.live_portfolio.lock().await.add_trade(&trade, None);
                }
                Err(e) => { s.state = StrategyState::Idle; }
            }
        }

    } else if s.state == StrategyState::Working {
        let s_port = s.portfolio.lock().await;

        if current_time >= end_time {
            // --- EXIT ---
            if !s_port.positions.is_empty() {
                let spx = self.grid.last_spx_price();
                drop(s_port);
                if let Some(exit_trade) = check_exit(&self.grid, &*s.portfolio.lock().await,
                                                      now_ct, &s.sid,
                                                      self.config.commission_per_contract, spx) {
                    s.state = StrategyState::Exiting;
                    execute_trade(&self.execution_client, &account_hash, &exit_trade,
                                  self.config.dry_run, self.config.order_offset).await.ok();
                    s.portfolio.lock().await.add_trade(&exit_trade, None);
                    self.live_portfolio.lock().await.add_trade(&exit_trade, None);
                }
            }

        } else {
            // --- REBALANCE ---
            let rebal_trades = check_rebalance(&self.grid, s, &*s_port, now_ct,
                                               start_time, end_time, &self.config);
            drop(s_port);
            for trade in rebal_trades {
                execute_trade(&self.execution_client, &account_hash, &trade,
                              self.config.dry_run, self.config.order_offset).await.ok();
                s.portfolio.lock().await.add_trade(&trade, None);
                self.live_portfolio.lock().await.add_trade(&trade, None);
            }
        }
    }
}
```

---

### Step 8 — Fix `reconcile_startup_positions` to be position-specific

**File:** `terminator_rust/src/strategy.rs`

Current behavior marks all strategies Working if any positions exist. Correct behavior:

```rust
pub async fn reconcile_startup_positions(&self, positions: &[BrokerPosition]) {
    if positions.is_empty() {
        info!("No open positions at startup. Starting fresh.");
        return;
    }
    let mut strats = self.sub_strategies.lock().await;
    // Mark only strategies that own these symbols as Working.
    // For now: if we can identify by symbol which sub-strategy traded, mark only those.
    // Minimum viable: if any positions exist, mark the earliest strategy as Working
    // and leave later-entry strategies as Idle so they can still enter.
    // Full solution: store strategy_id in broker order metadata and match here.
    for (sid, s) in strats.iter_mut() {
        // Conservative: if positions exist, mark all as Working to prevent duplicate entries.
        // TODO: match positions to specific sub-strategies by symbol prefix or order metadata.
        warn!("Startup guard: marking {} as Working due to {} open positions", sid, positions.len());
        s.has_traded_today = true;
        s.state = StrategyState::Working;
    }
}
```

Minimum viable fix: document that the blanket approach is safe (no duplicate entries) but prevents same-session re-entry by strategies that had no positions. A full fix requires storing the `strategy_id` tag in order metadata submitted to Schwab, then reading it back from the broker position feed. Defer full fix to a separate issue.

---

### Step 9 — Add `order_offset` + tick rounding to `check_entry()`

**File:** `terminator_rust/src/strategy.rs`

The `check_entry()` function returns a `Trade` with `credit` calculated from raw mids. The `execute_trade()` caller (after Step 3) will apply `order_offset` when computing the limit price. Ensure the credit stored on the `Trade` struct remains the raw mid credit (for PnL accounting) — only the broker submission price gets the offset applied. This matches Python's behavior.

---

### Step 10 — Document delta source divergence as known issue

**File:** this plan / `RUST_FIX_PLAN.md`

Rust computes delta from Black-Scholes IV inversion over the streaming mid-price. Python uses Schwab's streamed `delta` field directly. These will differ intraday, causing Rust to select slightly different strikes than Python. The magnitude depends on how much Schwab's model differs from BS.

Options:
- **(Preferred long-term):** Stream the `delta` field from Schwab's OPTIONS_CHAIN subscription and store it directly in `OptionLegQuote`, bypassing the BS calculation.
- **(Acceptable short-term):** Keep BS delta. The difference is typically < 0.01 for ATM strikes and causes at most one strike width of selection difference.

Defer to a separate issue. Add a comment in `grid.rs` documenting this.

---

### Step 11 — Fix theta stored as mid price

**File:** [grid.rs:81,88](terminator_rust/src/grid.rs#L81)

```rust
call.theta = call.mid; // wrong — theta ≠ mid
```

For 0DTE options, theta ≈ −mid / (hours_remaining) is a rough approximation. A better proxy is to compute true BS theta, or stream it from Schwab. This does not affect trading decisions (theta is not used in entry/rebalance) but corrupts the UI theta display and portfolio snapshot. Compute actual BS theta or set to `0.0` with a TODO rather than using `mid`. Defer to the same issue as Step 10 (streamed greeks).

---

## Files to Change

| File | Changes |
|---|---|
| `terminator_rust/src/config.rs` | Add all trading params; parse from config.json (Step 1) |
| `terminator_rust/src/strategy.rs` | Add portfolio to SubStrategy + Supervisor; dynamic sub-strategy list; fix execute_trade; add get_condor_deltas, check_rebalance, check_exit; extend tick() (Steps 2–9) |
| `terminator_rust/src/grid.rs` | Expose `last_spx_price()` for exit ITM check; document delta divergence (Step 10) |
| `terminator_rust/config.example.json` | Already correct — used as reference |

---

## Implementation Order

The steps have hard dependencies:

```
Step 1 (config)
  └─ Step 2 (portfolios + dynamic sub-strategies)
       ├─ Step 3 (execute_trade fix)
       ├─ Step 4 (get_condor_deltas)          ─┐
       ├─ Step 5 (check_rebalance)  ← needs 4  │
       ├─ Step 6 (check_exit)                   │ all needed before
       └─ Step 7 (wire tick())      ← needs all ┘ Step 7
Step 8 (reconcile fix)      — independent, can be done anytime
Step 9 (order_offset note)  — covered by Step 3
Steps 10–11 (greeks/theta)  — defer, document as known divergence
```

Steps 1 → 2 → 3+4+5+6 (parallel) → 7 is the critical path.

---

## Acceptance Criteria

- [ ] Rust `tick()` produces rebalance trades when short delta drifts beyond `rebalance_threshold`
- [ ] Rust `tick()` closes all positions at `end_time` via EXIT trade
- [ ] EXIT and REBALANCE trades reach Schwab with `BUY_TO_CLOSE` / `SELL_TO_CLOSE` instructions
- [ ] Limit prices sent to Schwab use `order_offset` and `$0.05` tick rounding
- [ ] Sub-strategy list in Rust matches the Python list for the same `config.json`
- [ ] `dry_run = true` shows rebalance and exit trades in the web UI PnL chart
- [ ] All trading parameters (thresholds, times, deltas) change at runtime by editing `config.json`; no recompile required

---

## Notes on Old Fix Doc (`RUST_REBALANCE_FIX.md`)

| Old doc claim | Actual status |
|---|---|
| "rebalance_threshold already parsed" | ❌ Not in config.rs — must add (Step 1) |
| References `run_historical_catchup()` at line 561 | ❌ Function does not exist; file is 446 lines |
| `min_credit` applied uniformly to all rebalance types | ❌ Python only applies to REBALANCE_SHORT |
| `CondorDeltas` struct (Option A) | ✅ Retained but extended with strike fields |
| Step 6 (add strike fields to Portfolio) | ✅ Replaced by on-the-fly derivation in get_condor_deltas |
| Delta decay linear note | ✅ Confirmed; documented as Step 10 (defer) |
