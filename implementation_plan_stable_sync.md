# Implementation Plan: Stable Trade Sync & Auto-Dismissal

To resolve the issue of redundant order confirmation popups, we will unify the definition of "Reality," implement a deterministic order-splitting algorithm, and enable automatic dismissal of the trade window when broker state matches the strategic intent.

## 1. Redefining "Reality" Sync
We will shift from a "filled-only" perspective to a **Total Exposure** perspective.
*   **Effective Position** = (Net Filled Contracts) + (Currently Working Orders).
*   **The Match Rule**: If `Strategic Simulation - Effective Position == 0`, the app concludes that the broker is already aligned with the model (either filled or in the process of filling).

## 2. Deterministic "Stable" Splitting
We will port the `get_smart_chunks` logic from `spt_v4` and ensure it is mathematically stable.
*   **Sorting Phase**: All legs in a trade will be sorted by **Strike Price (Ascending)** before any splitting occurs.
*   **Chunking Phase**: Legs will be "unrolled" to individual contracts and regrouped into balanced Iron Condors (max 4 legs per chunk).
*   **Stability Goal**: Given the same 8 legs, the app will *always* produce the exact same two 4-leg orders. This allows us to unambiguously identify if an existing broker order "matches" the current plan.

## 3. Auto-Dismissal Logic
The `execute_net_trade` workflow will be updated to handle "Zero Gap" scenarios silently:
*   **The Trigger**: On every broker sync (every 5 seconds), the open order confirmation window will re-run the `create_execution_plan`.
*   **The Action**: If the plan becomes **Empty** (meaning `Simulation == Reality`), the order confirmation window will **automatically dismiss itself**.
*   **User Experience**: If you manually fill the order at the broker or if a working order finally fills, the popup window on your screen will simply disappear, acknowledging the sync is complete.

## 4. Cancellation & Replacement
*   **No Price Threshold**: Working orders will **not** be cancelled due to limit price deviations.
*   **Mismatch Rule**: If the `Reality` (Filled + Working) does not exactly match the `Simulation`, the app will:
    1. Stay in the order confirmation modal.
    2. Cancel the mismatched working orders for that strategy.
    3. Propose a clean set of stable orders to bridge the gap.

## 5. Implementation Steps
1.  **Refactor `_get_smart_chunks`**: Port the `spt_v4` logic and add Strike-based sorting.
2.  **Update `create_execution_plan`**: Ensure it accurately aggregates `working_orders` into the `Effective Position`.
3.  **Update `execute_net_trade` UI side**: Add a hook to `close()` the modal if `plan` is empty.
4.  **Remove Price-based Culling**: Strip out logic that triggers replacements solely based on the 10-cent price offset.

---
**Status**: Reviewed by Claude (2026-03-27). See notes below.

---

## Code Review Notes

### Steps 1 & 2 — Already Implemented

**Step 1 (Strike-based sorting)** is already done. `_unroll_legs` (monitor.py ~L2501) sorts by `(side, strike)` before unrolling, and the greedy chunking algorithm in `_get_smart_chunks` picks legs in that sorted order. Before coding, verify that `spt_v4` contains no additional logic beyond what is already in place — this step may be a no-op.

**Step 2 (Total Exposure model)** is already done. `create_execution_plan` (~L2615–2639) already deducts working order quantities from needed quantities, and `_get_effective_live_positions` (~L1921) already combines filled + working positions. No changes needed here.

---

### Step 3 — Auto-Dismissal: Timer-based vs. Sync-triggered

The auto-dismissal mechanism already exists. The modal confirmation loop (~L457–465) checks `_is_trade_redundant` every 5 seconds and broadcasts a `close_modal` WebSocket message if true. **However, it is timer-based, not sync-triggered.** The check fires on a fixed interval and may run just before a fresh sync arrives, causing a lag of up to 4.9 seconds or a stale check on old data.

The plan's intent — "on every broker sync, re-run the check" — requires wiring the redundancy check into `_sync_broker_data`. Add the following at the end of the successful sync path (inside the `with self._data_lock` block), after working orders are updated:

```python
# At the end of the successful sync block in _sync_broker_data:
if self.pending_trade and self._is_trade_redundant(self.pending_trade):
    self.confirmation_event.set()
```

This is data-driven (fires when fresh broker data confirms alignment) rather than time-driven, which is the correct behaviour. Without this, the plan's stated trigger is not actually implemented.

---

### Step 4 — Price-based Culling: Likely Already Gone

No price-based cancellation logic was found in `create_execution_plan`. The staleness check there is already purely content-based (strike + side). **Confirm this is still present before spending time on it.** If it has already been removed in a prior cleanup, this step can be marked done.

---

### Bug (Not in Plan): Stale Order Detection Breaks on First Matching Leg

**File:** `monitor.py` ~L2652–2660 in `create_execution_plan`

The current staleness check marks a working order as stale only if **none** of its legs match the target trade:

```python
is_stale = True
for leg in wo.get('orderLegCollection', []):
    ...
    if any((strike, side) == k for k in trade.legs):
        is_stale = False
        break  # ← exits on first matching leg
```

An order with 3 correct legs and 1 wrong leg (e.g., stale strikes from a prior model tick) will **not** be cancelled, because the first correct leg short-circuits the loop. Under the Total Exposure model, this partially-wrong order is counted as covering contracts it shouldn't, which will block the correct order from being submitted and leave the gap permanently open.

**Fix:** Invert the logic. An order should be cancelled if **any** of its legs are at wrong strikes, not only if all are wrong:

```python
is_stale = False
for leg in wo.get('orderLegCollection', []):
    inst_obj = leg.get('instrument', {})
    k = (int(round(float(inst_obj.get('strikePrice', 0)))), inst_obj.get('putCall', ''))
    if not any((int(round(float(tl.strike))), tl.side) == k for tl in trade.legs):
        is_stale = True
        break  # Any leg not in the target plan makes the whole order stale
if is_stale:
    to_cancel.append(wo)
```

---

### Revised Implementation Checklist

1. **`_get_smart_chunks`**: Verify against `spt_v4` — likely already correct, confirm before changing.
2. **`create_execution_plan`**: Already implements Total Exposure — no changes needed.
3. **Auto-dismissal trigger**: Add `confirmation_event.set()` hook at end of `_sync_broker_data` successful path (see Step 3 note above).
4. **Price-based culling**: Confirm it is already removed — likely a no-op.
5. **Stale order detection fix**: Fix the break-on-first-match logic in `create_execution_plan` (see Bug note above). **This is the highest-priority item.**
