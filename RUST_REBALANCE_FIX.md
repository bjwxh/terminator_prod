# Rust Rebalance & Exit Fix

**Date:** 2026-05-27  
**Branch:** `rust`  
**Severity:** High — live Rust positions are never adjusted after entry, diverging materially from Python behaviour throughout the day.

---

## Problem Statement

The Python monitor rebalances open iron condor legs continuously throughout the session whenever the live delta of a leg drifts too far from its **time-decayed target**. The Rust supervisor opens the iron condor once per sub-strategy and then holds forever — it has no rebalance or end-of-day exit logic at all.

---

## Root Cause

### Python `_monitor_step()` — three-branch state machine
([`server/core/monitor.py:1244-1258`](server/core/monitor.py#L1244-L1258))

```python
if not s.has_traded_today:
    trade = self._check_entry(s, snap, ts)        # open iron condor

elif s.has_traded_today:
    if t_time >= end_time_obj:
        trade = self._create_exit_trade(...)       # close all at 15:00 CT
    else:
        trades = self._check_rebalance(...)        # roll legs when delta drifts
```

### Rust `tick()` — entry only
([`terminator_rust/src/strategy.rs:687-741`](terminator_rust/src/strategy.rs#L687-L741))

```rust
if s.state == StrategyState::Idle && current_time >= s.trade_start_time {
    // entry → transitions to Working
}
// No else-if for Working → no rebalance, no exit
```

Once a sub-strategy becomes `Working`, the tick loop skips it on every subsequent 5-second tick. The `StrategyState::Exiting` variant and the `rebalance_threshold` / `long_leg_rebalance_delta_threshold` config fields are loaded but never used.

---

## Missing Functions

Three new public functions are needed in `strategy.rs`, plus call-sites in `tick()` and `run_historical_catchup()`.

---

## Fix Plan

### Step 1 — Add helper: `get_leg_deltas()`

Add to `strategy.rs` (or `portfolio.rs`). Returns the current abs delta of each of the four condor legs from live portfolio positions.

```rust
pub struct CondorDeltas {
    pub abs_short_call: f64,
    pub abs_long_call:  f64,
    pub abs_short_put:  f64,
    pub abs_long_put:   f64,
}

pub fn get_condor_deltas(portfolio: &crate::portfolio::Portfolio) -> CondorDeltas {
    let find = |is_call: bool, is_short: bool| -> f64 {
        portfolio.positions.iter()
            .filter(|p| {
                let right_side = if is_call { p.side == "CALL" } else { p.side == "PUT" };
                let right_dir  = if is_short { p.quantity < 0 } else { p.quantity > 0 };
                right_side && right_dir
            })
            .map(|p| p.delta.abs())
            .next()
            .unwrap_or(0.0)
    };
    CondorDeltas {
        abs_short_call: find(true,  true),
        abs_long_call:  find(true,  false),
        abs_short_put:  find(false, true),
        abs_long_put:   find(false, false),
    }
}
```

---

### Step 2 — Add `check_rebalance()`

Mirrors Python `_check_rebalance` + `_create_rebalance_trade`.

**Signature:**
```rust
pub fn check_rebalance(
    grid: &OptionsGrid,
    s: &SubStrategy,
    portfolio: &crate::portfolio::Portfolio,
    now: chrono::DateTime<Tz>,
    start_time: NaiveTime,
    end_time: NaiveTime,
    rebalance_threshold: f64,
    long_leg_rebalance_delta_threshold: f64,
    max_diff: f64,
    min_credit: f64,
    commission_per_contract: f64,
    stale_guard_min_price: f64,
) -> Vec<Trade>
```

**Logic (mirrors Python exactly):**

```
t_short = calculate_delta_decay(now, s.init_s_delta, start_time, end_time)
t_long  = calculate_delta_decay(now, s.init_l_delta, start_time, end_time)
deltas  = get_condor_deltas(portfolio)

for side in [CALL, PUT]:
    abs_short_delta = deltas.abs_short_{side}
    abs_long_delta  = deltas.abs_long_{side}

    sn_needs = |abs_short_delta - t_short| > rebalance_threshold        // 0.075 default
    ln_needs = |abs_long_delta  - t_long|  > long_leg_rebalance_delta_threshold  // 0.13 default

    // Width-cap override: also trigger long rebalance if spread exceeds max_diff
    if !ln_needs:
        short_strike = portfolio.short_{side}_strike
        long_strike  = portfolio.long_{side}_strike
        if |long_strike - short_strike| > max_diff:
            ln_needs = true

    on_side = positions with this side

    if on_side.is_empty() && (sn_needs || ln_needs):
        trade = create_new_spread_trade(side, t_short, t_long)   // re-open a bare vertical
    elif sn_needs:
        trade = create_rebalance_short(side, t_short, t_long)    // close old short, open new
    elif ln_needs:
        trade = create_rebalance_long(side, t_long)              // close old long, open new

    if |trade.credit| < min_credit * 100 → skip
    else → push to result Vec
```

**`create_rebalance_short(side, t_short, t_long)`**  
1. Find `old_short` leg in portfolio (same side, qty < 0).  
2. Call `find_closest_option(grid, t_short, is_call, ...)` → `new_short`.  
3. Legs: `[close old_short (+qty), open new_short (-qty)]`.  
4. Width-cap check: if `|new_short.strike - old_long.strike| > max_diff`, also roll the long: `[close old_long (-qty), open new_long at t_long (+qty)]` — up to 4 legs total.  
5. Purpose string: `"REBALANCE_SHORT"`.

**`create_rebalance_long(side, t_long)`**  
1. Find `old_long` leg (same side, qty > 0).  
2. Call `find_closest_option(grid, t_long, is_call, Some(max_diff), Some(short_strike), ...)` → `new_long`.  
3. Legs: `[close old_long (-qty), open new_long (+qty)]`.  
4. Purpose string: `"REBALANCE_LONG"`.

> **Note:** `find_closest_option()` already exists in `strategy.rs` — reuse it directly.

---

### Step 3 — Add `check_exit()`

Mirrors Python `_create_exit_trade`. Called when `current_time >= end_time`.

```rust
pub fn check_exit(
    grid: &OptionsGrid,
    portfolio: &crate::portfolio::Portfolio,
    now: chrono::DateTime<Tz>,
    strategy_id: &str,
    commission_per_contract: f64,
) -> Option<Trade>
```

**Logic:**
```
For each position in portfolio.positions:
    close_qty = -position.quantity          // flip the sign to close
    exit_price = current mid from grid      // 0.0 if not found (expires worthless)
    
    leg = OptionLeg { ..., quantity: close_qty, price: exit_price }

credit = sum(-l.quantity * l.price for l in legs) * 100
commission = commission_per_contract * legs.len()
purpose = "EXIT"
```

Python uses `price = 0.0` for OTM legs at expiry (they expire worthless), and the real mid for any ITM legs. The Rust implementation can simply use whatever the grid currently quotes — near expiry the OTM legs will already be near zero.

---

### Step 4 — Wire into `tick()`

Replace the current `tick()` strategy loop body
([`strategy.rs:679-741`](terminator_rust/src/strategy.rs#L679-L741)):

```rust
// BEFORE (entry only):
if s.state == StrategyState::Idle && current_time >= s.trade_start_time {
    // ...entry logic...
}

// AFTER (full state machine):
if s.state == StrategyState::Idle && current_time >= s.trade_start_time {
    // --- ENTRY (unchanged) ---
    if let Some(trade) = check_entry(...) {
        // ...existing logic...
    }

} else if s.state == StrategyState::Working {

    let s_port = s.portfolio.lock().await;

    if current_time >= end_time {
        // --- EXIT ---
        if !s_port.positions.is_empty() {
            drop(s_port);
            if let Some(exit_trade) = check_exit(&self.grid, &*s.portfolio.lock().await, now_ct, &s.sid, ...) {
                // execute_trade() → transition to Exiting → update portfolio
                s.state = StrategyState::Exiting;
                execute_trade(&self.execution_client, &account_hash, &exit_trade, self.config.dry_run).await;
                s.portfolio.lock().await.add_trade(&exit_trade, None);
                self.live_portfolio.lock().await.add_trade(&exit_trade, None);
            }
        }

    } else {
        // --- REBALANCE ---
        let rebal_trades = check_rebalance(
            &self.grid,
            s,
            &*s_port,
            now_ct,
            start_time, end_time,
            self.config.rebalance_threshold,
            self.config.long_leg_rebalance_delta_threshold,
            self.config.max_spread_diff,
            self.config.min_credit,
            self.config.commission_per_contract,
            self.config.stale_guard_min_price,
        );
        drop(s_port);

        for trade in rebal_trades {
            info!("⚖️ Rebalance signal for {}: {:?}", s.sid, trade.purpose);
            execute_trade(&self.execution_client, &account_hash, &trade, self.config.dry_run).await.ok();
            let mut p = s.portfolio.lock().await;
            p.add_trade(&trade, None);
            let mut lp = self.live_portfolio.lock().await;
            lp.add_trade(&trade, None);
        }
    }
}
```

---

### Step 5 — Wire into `run_historical_catchup()`

The bootstrap replay loop
([`strategy.rs:561-598`](terminator_rust/src/strategy.rs#L561-L598))
also needs the same three branches so the historical chart/PnL matches the live session.

Replace the inner loop body:

```rust
// Existing entry-only check:
if s.state == StrategyState::Idle && current_time >= s.trade_start_time {
    if let Some(trade) = check_entry(...) { ... }
}

// ADD: rebalance + exit during replay
else if s.state == StrategyState::Working {
    let s_port = s.portfolio.lock().await;

    if current_time >= end_time {
        if !s_port.positions.is_empty() {
            drop(s_port);
            if let Some(exit_trade) = check_exit(&self.grid, &*s.portfolio.lock().await, ts, &sid, ...) {
                s.state = StrategyState::Exiting;
                let mut sp = s.portfolio.lock().await;
                sp.add_trade(&exit_trade, None);
                let mut lp = self.live_portfolio.lock().await;
                lp.add_trade(&exit_trade, None);
            }
        }
    } else {
        let rebal_trades = check_rebalance(&self.grid, s, &*s_port, ts, start_time, end_time, ...);
        drop(s_port);
        for tr in rebal_trades {
            let mut sp = s.portfolio.lock().await;
            sp.add_trade(&tr, None);
            let mut lp = self.live_portfolio.lock().await;
            lp.add_trade(&tr, None);
        }
    }
}
```

---

### Step 6 — Add `short_call_strike` / `short_put_strike` tracking to `Portfolio`

Python uses `s.portfolio.short_call_strike` etc. in the width-cap check. Rust's `Portfolio` struct has no such fields. Two options:

**Option A (simple):** Derive them on the fly inside `get_condor_deltas()` by scanning `portfolio.positions`. No struct change needed.

**Option B (explicit):** Add four optional fields to `Portfolio`:

```rust
pub short_call_strike: Option<f64>,
pub long_call_strike:  Option<f64>,
pub short_put_strike:  Option<f64>,
pub long_put_strike:   Option<f64>,
```

Set them in `add_trade()` when `trade.purpose == "IRON_CONDOR"`, clear them when all positions close out.

**Recommendation:** Option A for now — simpler, no struct migration needed.

---

### Step 7 — Add `"REBALANCE_SHORT"` / `"REBALANCE_LONG"` / `"REBALANCE_NEW"` / `"EXIT"` to `Trade.purpose`

Currently `Trade.purpose` is a plain `String`. The existing values are `"IRON_CONDOR"` and `"EXIT"` (unused). No type change is needed — just ensure the string literals match exactly so the web UI and backtest reporting can distinguish them. The Python `TradePurpose` enum values to match:

| Python value | Rust string to use |
|---|---|
| `IRON_CONDOR` | `"IRON_CONDOR"` ✅ already used |
| `REBALANCE_SHORT` | `"REBALANCE_SHORT"` |
| `REBALANCE_LONG` | `"REBALANCE_LONG"` |
| `REBALANCE_NEW` | `"REBALANCE_NEW"` |
| `EXIT` | `"EXIT"` |

---

## Delta Decay — Alignment Note

Python's `calculate_delta_decay()` in `utils.py` uses a **power-law curve** loaded from a JSON file for `init_leg_delta > 0.20`, and falls back to **linear decay** for `init_leg_delta <= 0.20`.

Rust `calculate_delta_decay()` in `strategy.rs` only implements **linear decay** for all delta values.

The short leg delta is typically 0.15–0.20 (linear range), so this does not cause a material difference today. However the discrepancy should be documented and the power-law path ported to Rust later if delta targets above 0.20 are used.

---

## Files to Change

| File | Changes |
|---|---|
| `terminator_rust/src/strategy.rs` | Add `check_rebalance()`, `check_exit()`, `get_condor_deltas()`; extend `tick()` and `run_historical_catchup()` with Working-state branches |
| `terminator_rust/src/portfolio.rs` | Optionally add strike-tracking fields (Step 6 Option B); otherwise no change |
| `terminator_rust/src/config.rs` | No change needed — `rebalance_threshold` and `long_leg_rebalance_delta_threshold` already parsed |

---

## Tests to Write

1. **Unit: `check_rebalance` short-leg trigger** — Build a mock grid and portfolio where short call delta has drifted past threshold; assert one `REBALANCE_SHORT` trade is returned.
2. **Unit: `check_rebalance` no trigger** — Delta within threshold → empty Vec.
3. **Unit: `check_rebalance` width-cap** — Long strike too far from short → `ln_needs = true` even when delta is fine.
4. **Unit: `check_exit`** — Portfolio with two open legs → exit trade has opposite quantities.
5. **Integration: full tick cycle** — Bootstrap replay over synthetic historical data; assert rebalance trades appear mid-day and exit appears at `end_time`.

Existing test file: [`terminator_rust/tests/strategy_tests.rs`](terminator_rust/tests/strategy_tests.rs)

---

## Acceptance Criteria

- [ ] Running Rust side-by-side with Python on historical data produces rebalance trades at the same timestamps (within the 5s tick granularity vs Python's 30s).
- [ ] Positions are closed at `end_time` every session.
- [ ] `dry_run = true` mode shows rebalance trades in the web UI PnL chart.
- [ ] `rebalance_threshold` and `long_leg_rebalance_delta_threshold` config changes take effect without recompile (they are already runtime-loaded from `config.json`).
