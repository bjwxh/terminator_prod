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

*Status: Planned (2026-04-02)*
