# EOD Report — Intraday Chart Bugs (2026-03-24)

Two bugs in `_run_historical_simulation` cause the intraday analysis chart (delta drift and strike subplots) to fail completely when running the EOD report.

---

## BUG-1: Key name mismatch — `'ts'` vs `'timestamp'`

**Files:** `server/core/monitor.py:1934` · `eod/eod_report.py:164`
**Severity:** High — causes `KeyError: 'timestamp'` immediately in `generate_intraday_chart`, so the intraday chart never renders.

`_run_historical_simulation` appends history records with key `'ts'`:
```python
# monitor.py:1933–1938
history.append({
    'ts': ts.isoformat(),   # ← 'ts'
    'spx': spx,
    'sim_pnl': ...,
    'live_pnl': ...
})
```

But `generate_intraday_chart` immediately tries to read `df['timestamp']`:
```python
# eod_report.py:164
df['timestamp'] = pd.to_datetime(df['timestamp'])  # ← KeyError here
```

**Proposed fix:** Rename `'ts'` → `'timestamp'` in the `history.append()` call.

---

## BUG-2: Delta and strike fields computed but never stored in history

**File:** `server/core/monitor.py:1929–1938`
**Severity:** High — `sim_d` and `live_d` are computed via `get_all_deltas()` but immediately discarded. The chart expects `sim_sc_delta`, `sim_sp_delta`, `live_sc_delta`, `live_sp_delta`, `sim_sc_strike`, `sim_sp_strike`, `live_sc_strike`, `live_sp_strike` — none of these are in the history dict.

Current code:
```python
if collect_history:
    sim_d = self.combined_portfolio.get_all_deltas(snap)        # computed...
    live_d = self.live_combined_portfolio.get_all_deltas(snap)  # computed...

    history.append({
        'ts': ts.isoformat(),
        'spx': spx,
        'sim_pnl': round(self.combined_portfolio.net_pnl, 2),
        'live_pnl': round(self.live_combined_portfolio.net_pnl, 2)
        # ← sim_d and live_d silently dropped
    })
```

**Proposed fix:** Include the computed deltas and derive strikes from current positions:
```python
if collect_history:
    sim_d = self.combined_portfolio.get_all_deltas(snap)
    live_d = self.live_combined_portfolio.get_all_deltas(snap)

    sim_sc_legs  = [p for p in self.combined_portfolio.positions       if p.side == 'CALL' and p.quantity < 0]
    sim_sp_legs  = [p for p in self.combined_portfolio.positions       if p.side == 'PUT'  and p.quantity < 0]
    live_sc_legs = [p for p in self.live_combined_portfolio.positions  if p.side == 'CALL' and p.quantity < 0]
    live_sp_legs = [p for p in self.live_combined_portfolio.positions  if p.side == 'PUT'  and p.quantity < 0]

    history.append({
        'timestamp': ts.isoformat(),                          # also fixes BUG-1
        'spx': spx,
        'sim_pnl':  round(self.combined_portfolio.net_pnl, 2),
        'live_pnl': round(self.live_combined_portfolio.net_pnl, 2),
        'sim_sc_delta':  sim_d['abs_short_call_delta'],
        'sim_sp_delta':  sim_d['abs_short_put_delta'],
        'live_sc_delta': live_d['abs_short_call_delta'],
        'live_sp_delta': live_d['abs_short_put_delta'],
        'sim_sc_strike':  sim_sc_legs[0].strike  if sim_sc_legs  else None,
        'sim_sp_strike':  sim_sp_legs[0].strike  if sim_sp_legs  else None,
        'live_sc_strike': live_sc_legs[0].strike if live_sc_legs else None,
        'live_sp_strike': live_sp_legs[0].strike if live_sp_legs else None,
    })
```

**Note on delta semantics:** `get_all_deltas()` returns the **total aggregate delta** across all sub-strategies in the combined portfolio (e.g. 3 sub-strategies open = sum of all 3 short call deltas). The "Short Call Delta Drift" chart therefore reflects the portfolio-level total, not a single-leg delta.

**Note on strikes:** With multiple sub-strategies at different strikes, there is no single representative short call/put strike. The fix above uses the first short call/put leg found in `positions`. An alternative would be plotting per-sub-strategy series, but that would require restructuring the chart.
