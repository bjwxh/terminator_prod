# Plan: Live Refresh of Order Confirmation Modal When Position Gap Changes (2026-04-02)

## Problem

When the order confirmation modal is open (timer counting down or paused), the backend reconciliation loop may detect that the position gap has changed — e.g., the required trade shifts from `+1P6500 -1P6550` to `+1P6500 -1P6560`. Currently the backend does nothing with the new gap while a modal is active (`in_queue = True` path, line ~2259 in `monitor.py`). The user sees stale orders and, if they confirm, submits the wrong trade.

---

## Solution Overview

Close the current modal without penalty, then immediately open a new modal with the updated legs. Three guardrails are required.

---

## Guardrail 1: Skip the Dismiss Cooldown on System-Initiated Replacement

**The problem:** When a trade is dismissed (confirmed=False) for a `RECONCILIATION` purpose, the execution loop sets `last_dismissed_recon_time = time()`, imposing a 20-second cooldown before the next recon trade can be queued. A system-initiated replacement would trigger this cooldown and block the new modal from appearing.

**The fix:** Introduce a boolean flag `_recon_modal_replacing: bool = False` on the monitor. Set it to `True` before the system dismisses the old trade for replacement, and reset it to `False` after. In the execution loop where `last_dismissed_recon_time` is set, skip the assignment if this flag is set:

```python
if trade.purpose == TradePurpose.RECONCILIATION and not self._recon_modal_replacing:
    self.last_dismissed_recon_time = time()
```

This ensures that only genuine user dismissals (or redundancy auto-dismissals) trigger the cooldown — not system-initiated replacements.

---

## Guardrail 2: Compare Legs Structurally Before Deciding to Replace

**The problem:** Every recon cycle, market prices shift. If a replacement is triggered whenever `last_reconciliation_trade` changes, it would fire on pure mid-price movements even when the required strikes and quantities are identical. This would cause unnecessary modal churn.

**The fix:** Only trigger a replacement when the set of `(strike, side, quantity)` tuples differs between the currently-pending trade legs and the newly computed recon trade legs. Price changes alone do not warrant a replacement.

```python
def _recon_legs_changed(self, old_trade: Trade, new_trade: Trade) -> bool:
    old_legs = {(int(round(l.strike)), l.side, int(l.quantity)) for l in old_trade.legs}
    new_legs = {(int(round(l.strike)), l.side, int(l.quantity)) for l in new_trade.legs}
    return old_legs != new_legs
```

---

## Guardrail 3: 5-Second Debounce Before Replacing

**The problem:** The reconciliation loop fires on pokes (from sim trades) as well as on a 30-second periodic timer. Under active trading, pokes can arrive rapidly. A single transient intermediate position state (e.g., a partial fill mid-flight) could trigger a replacement unnecessarily.

**The fix:** Track when the new diverging leg set was first detected (`_recon_leg_change_first_seen: float`). Only proceed with the replacement if the same changed leg set has been consistently observed for ≥5 seconds (matching broker sync cadence). Reset this timestamp if the leg set reverts or changes again.

```python
# In _check_reconciliation, when in_queue is True and pending_trade is the active recon modal:
if self._recon_legs_changed(self.pending_trade, new_recon_trade):
    now = time()
    if self._recon_leg_change_first_seen == 0.0:
        self._recon_leg_change_first_seen = now  # Start debounce clock
    elif (now - self._recon_leg_change_first_seen) >= 5.0:
        self._trigger_recon_modal_replacement(new_recon_trade)
        self._recon_leg_change_first_seen = 0.0  # Reset
else:
    self._recon_leg_change_first_seen = 0.0  # Legs reverted — reset debounce
```

---

## Replacement Flow (`_trigger_recon_modal_replacement`)

1. Set `_recon_modal_replacing = True`
2. Set `_pending_trade_confirmed = False`
3. Set `confirmation_event` to wake the execution loop — the loop will dismiss the old trade, skip the cooldown (due to flag), clean up `pending_trade = None`, and call `task_done()`
4. Enqueue the new trade via `order_queue.put_nowait(new_recon_trade)`
5. Add to `active_order_signals` and `_queued_purposes` trackers
6. Reset `_recon_modal_replacing = False` (can be done immediately after enqueue, before the loop processes it — the flag is only read in the dismiss path)

The execution loop naturally picks up the new trade from the queue, broadcasts a fresh `trade_signal`, and opens the new modal. No special fast-path needed — the gap between close and open is sub-second.

---

## Files to Modify

- **`server/core/monitor.py`**:
  - Add `_recon_modal_replacing`, `_recon_leg_change_first_seen` to `__init__`
  - Add `_recon_legs_changed()` helper method
  - Add `_trigger_recon_modal_replacement()` method
  - Modify `_check_reconciliation()`: add leg-change detection + debounce logic in the `in_queue` branch
  - Modify execution loop dismiss path: skip `last_dismissed_recon_time` when `_recon_modal_replacing` is set

- **`server/static/app.js`**: No changes required. The existing `close_modal` → `trade_signal` flow handles the UI side correctly.

---

## Edge Cases Covered

| Scenario | Handled By |
|---|---|
| User clicks Confirm on stale modal just as replacement fires | `strat_id` mismatch in `confirm_live_trade` safely rejects it |
| WS drops between close and new modal | Reconnect replay sends `pending_trade` (the new trade) |
| Transient partial fill shifts legs briefly | 5s debounce absorbs it |
| Legs change back to original within debounce window | Debounce clock resets, no replacement triggered |
| Replacement dismissed by user (not system) | `_recon_modal_replacing` is False → cooldown applies normally |

---

*Status: Implemented (2026-04-02)*

---

## Post-Deployment Bug: Modal No Longer Showing (2026-04-02)

### Symptom
After deploying the implementation, the order confirmation modal stopped appearing entirely. The backend still detects the position gap, logs "Awaiting confirmation for trade: GAP_SYNC_XXXXXX", and auto-executes after the countdown — but the frontend never shows the modal.

### What the Logs Confirm
- Backend reaches `run_order_execution_loop` and logs "Awaiting confirmation" → the broadcast **is attempted**
- Auto-execute fires at the correct timeout (30s) → the execution loop is processing normally
- No modal appears at any point during the countdown

This rules out the execution loop crashing. The broadcast path at `monitor.py:473` (`if manager.active_connections: asyncio.create_task(manager.broadcast(msg))`) was reached, but either the WS had no active connections or the frontend received the message but did not render the modal.

---

### Confirmed Bug: `time()` Import Collision in `_check_reconciliation` (line 2273)

**Status: Confirmed bug, needs fix.**

`monitor.py` line 8 imports `time` from `datetime`:
```python
from datetime import datetime, date, time, timedelta, timezone
```

The new code at line 2273 calls:
```python
now = time()
```

This calls `datetime.time()` (the time-of-day constructor), not `time.time()` (the Unix timestamp function). `datetime.time()` returns `datetime.time(0, 0, 0)` (midnight) — no exception on first call.

On the **second** recon cycle where legs are still changed, line 2281 evaluates:
```python
elif (now - self._recon_leg_change_first_seen) >= 5.0:
```

Both `now` and `self._recon_leg_change_first_seen` are `datetime.time(0, 0, 0)` objects. Subtracting two `datetime.time` objects raises:
```
TypeError: unsupported operand type(s) for -: 'datetime.time' and 'datetime.time'
```

This exception propagates up through `_check_reconciliation` and is caught by the recon loop's outer handler (`except Exception`), which logs it as an error and sleeps 1 second before continuing. **The debounce never triggers and no replacement ever fires.** However, this only affects the leg-change detection path (`else` branch when `in_queue` is True) — it does not explain why the initial modal broadcast fails to show.

**Fix:** Replace `now = time()` at line 2273 with a local import:
```python
from time import time as _time
now = _time()
```
Or use `datetime.now(CHICAGO).timestamp()` which is already available in scope.

---

### Suspected Primary Cause: Unknown — Needs More Evidence

The `time()` collision explains why the **replacement feature** is broken, but does not directly explain why the **initial modal** (never-before-seen trade, fresh queue entry) fails to appear. The broadcast for the initial modal occurs in a completely separate code path (execution loop, line 473) that is unaffected by the `_check_reconciliation` bug.

**Candidates to investigate, in order of likelihood:**

| # | Hypothesis | How to Verify |
|---|---|---|
| 1 | No active WS connections at moment of broadcast — browser tab disconnected or not refreshed after deployment | Check browser console for WS connection errors; check if app showed as "disconnected" |
| 2 | `_is_trade_redundant` incorrectly returns `True` on the first tick (`elapsed_total=0.0`), triggering immediate `close_modal` before user sees it | Add debug log in `_is_trade_redundant` showing return value; look for `close_modal` WS message in browser network tab immediately after `trade_signal` |
| 3 | TypeError exception in `_check_reconciliation` (from the `time()` bug) corrupts some shared state (e.g. `_recon_last_seen_legs`, `_recon_leg_change_first_seen`) in a way that indirectly affects the execution loop | Check server error logs for `TypeError: unsupported operand type(s) for -` |
| 4 | Frontend received `trade_signal` but `showTradeModal` failed silently | Check browser console for JS errors at the time a trade was expected |

**Recommended next step:** Check server error logs for `TypeError` lines, and check the browser console for WS messages and JS errors around the time a trade was expected.

---

### Verified Root Cause: `numpy.bool_` JSON Serialization Failure (Confirmed via `logs/server_production.log`)

**Status: Fixed.**

Log evidence at `11:44:53`:
```
asyncio - ERROR - Task exception was never retrieved
future: <Task finished name='Task-52' coro=<ConnectionManager.broadcast() ...>
  exception=TypeError('Object of type bool_ is not JSON serializable')>
TypeError: Object of type bool_ is not JSON serializable
```

The broadcast failed immediately after "Awaiting confirmation" was logged — the `trade_signal` payload was constructed but `json.dumps` threw before any data reached the browser.

**Cause:** In `_classify_order_type`, the iron condor branch (line 2875) computed:
```python
is_credit = (sp.strike > lp.strike and sc.strike < lc.strike)
```
`sp.strike` and `lp.strike` are `numpy.float64` (originating from a pandas DataFrame row). Comparing two `numpy.float64` values returns `numpy.bool_`, not a Python `bool`. This value propagated into the `trade_signal` payload at `get_trade_signal_payload` line 245 (`"is_credit": is_credit`), causing `json.dumps` to fail.

The chime/email notification still fired because those go through a separate code path that does not serialize the full payload.

**Fix applied** (`monitor.py` line 2875):
```python
# Before
is_credit = (sp.strike > lp.strike and sc.strike < lc.strike)

# After
is_credit = bool(sp.strike > lp.strike and sc.strike < lc.strike)
```

---

### Fix: `time()` Import Collision in Debounce Logic (Confirmed, Fixed)

**Status: Fixed.**

`monitor.py` line 8 imports `time` from `datetime`:
```python
from datetime import datetime, date, time, timedelta, timezone
```

The debounce code at line 2273 called `time()`, which resolved to `datetime.time()` (the time-of-day constructor) rather than the Unix timestamp function. On the first call it returns `datetime.time(0, 0, 0)` without raising. On the second cycle, the subtraction `now - self._recon_leg_change_first_seen` raises:
```
TypeError: unsupported operand type(s) for -: 'datetime.time' and 'datetime.time'
```
This was caught silently by the recon loop's `except Exception` handler, so the debounce clock never advanced and the modal replacement feature never triggered.

**Fix applied** (`monitor.py` line 2273):
```python
# Before
now = time()

# After
from time import time as _monotime
now = _monotime()
```

---

*Status: All bugs resolved (2026-04-02)*
