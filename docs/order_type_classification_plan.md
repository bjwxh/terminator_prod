# Order Type Classification & Price Floor/Cap Enforcement

## Background

Currently the system uses market mid price to determine credit vs. debit intent and applies a floor of `$0.00` unconditionally for all order types. This is correct for known spread structures, but it should not apply to exotic/unrecognized structures where the limit price legitimately crossing zero may be valid.

This document defines the five recognized order types, how to detect them from leg topology, how to determine their structural credit/debit intent, and how price floors/caps should be enforced per type.

---

## Recognized Order Types

### 1. Single
**Example:** `+1P6000`  
**Detection:** Exactly 1 leg after rolling.  
**Structural intent:** Determined by quantity sign: `qty < 0` → CREDIT, `qty > 0` → DEBIT.  
**Floor/Cap:** Floor at `$0.00`. A sold option cannot pay you more than its full premium (and the single-leg price is always non-negative in broker terms).

### 2. Spread (Vertical)
**Example:** `+1P6000, -1P6100`  
**Detection:** Exactly 2 legs, one long (+) and one short (−), with equal absolute quantities. May be same or different option types (put spread, call spread, or risk reversal).  
**Structural intent:** Determined by market mid price (which leg is more expensive at time of order).  
**Floor/Cap:** Floor at `$0.00`. Once classified as credit or debit at mid, the limit price cannot cross to the other side.

### 3. Butterfly
**Example:** `+1P6000, -2P6050, +1C6150` (iron butterfly) or `+1P6000, -2P6050, +1P6100` (standard)  
**Detection:** Exactly 3 legs after rolling. Quantity ratios are `+1/−2/+1` (long butterfly) or `−1/+2/−1` (short butterfly) when legs are sorted by strike.  
**Structural intent:**
- `+1/−2/+1` (long the wings, short the body) → DEBIT (buying the butterfly)
- `−1/+2/−1` (short the wings, long the body) → CREDIT (selling the butterfly)  

**Floor/Cap:** Floor at `$0.00`.

### 4. Condor (Same-Type)
**Example:** `+1P6000, -1P6050, -1P6100, +1P6150`  
**Detection:** Exactly 4 legs, all the same option side (all PUT or all CALL). Quantity ratios `+1/−1/−1/+1` or `−1/+1/+1/−1` when sorted by strike.  
**Structural intent:**
- `+1/−1/−1/+1` (long outer, short inner) → DEBIT (long condor)
- `−1/+1/+1/−1` (short outer, long inner) → CREDIT (short condor)

**Floor/Cap:** Floor at `$0.00`.

### 5. Iron Condor (Mixed-Type)
**Example:** `+1P6000, -1P6050, -1C6100, +1C6150`  
**Detection:** Exactly 4 legs, exactly 2 PUT legs and 2 CALL legs. The put side has one long and one short; the call side has one long and one short.  
**Structural intent:**
- Short inner put (higher put strike) + short inner call (lower call strike) → CREDIT
- Long inner put + long inner call → DEBIT (long / reverse iron condor)

Formally: if `short_put.strike > long_put.strike` AND `short_call.strike < long_call.strike` → CREDIT, otherwise DEBIT.

**Floor/Cap:** Floor at `$0.00`.

---

## Unknown / Exotic Structures

Any leg combination not matching the 5 types above (e.g., 5-leg combos, ratio spreads, diagonal spreads with unequal quantities) is treated as **UNKNOWN**.

For UNKNOWN types: **no floor/cap is enforced**. The limit price may go either credit or debit. The system uses `NET_CREDIT` or `NET_DEBIT` dynamically based on the signed market mid price, and the user can freely adjust the limit price in either direction from the UI.

---

## Price Floor/Cap Logic Summary

| Type         | Known? | Credit/Debit From    | Floor at $0.00? |
|--------------|--------|----------------------|-----------------|
| Single       | Yes    | Leg quantity sign    | Yes             |
| Spread       | Yes    | Market mid           | Yes             |
| Butterfly    | Yes    | Leg quantity ratios  | Yes             |
| Condor       | Yes    | Leg quantity ratios  | Yes             |
| Iron Condor  | Yes    | Leg strike topology  | Yes             |
| Unknown      | No     | Market mid (dynamic) | **No**          |

---

## Implementation Plan

### Step 1 — Add `classify_order_type(legs)` utility function

**Location:** New helper method in `LiveTradingMonitor` (or standalone module), called `_classify_order_type(legs: List[Leg]) -> Tuple[str, Optional[bool]]`

Returns a tuple of `(order_type, is_credit_structural)`:
- `order_type`: one of `"single"`, `"spread"`, `"butterfly"`, `"condor"`, `"iron_condor"`, `"unknown"`
- `is_credit_structural`: `True` / `False` if determinable purely from topology (butterfly, condor, iron condor, single), else `None` (spread, unknown — deferred to mid price)

```python
def _classify_order_type(self, legs) -> Tuple[str, Optional[bool]]:
    n = len(legs)
    
    if n == 1:
        is_credit = legs[0].quantity < 0
        return ("single", is_credit)
    
    if n == 2:
        # One long, one short, equal abs quantities
        quantities = [l.quantity for l in legs]
        if sorted([abs(q) for q in quantities]) == [abs(quantities[0])] * 2:
            if any(q > 0 for q in quantities) and any(q < 0 for q in quantities):
                return ("spread", None)  # credit/debit from mid price
    
    if n == 3:
        # All same side, ratios +1/-2/+1 or -1/+2/-1
        sides = set(l.side for l in legs)
        if len(sides) <= 2:  # iron butterfly allows mixed
            sorted_legs = sorted(legs, key=lambda l: l.strike)
            unit = abs(sorted_legs[0].quantity)
            if unit > 0:
                ratios = [l.quantity // unit for l in sorted_legs]
                if ratios == [1, -2, 1]:
                    return ("butterfly", True)   # long butterfly = debit (but mid determines it)
                    # Actually: long butterfly = debit, so is_credit = False
                if ratios == [-1, 2, -1]:
                    return ("butterfly", False)  # short butterfly = credit
    
    if n == 4:
        sides = [l.side for l in legs]
        put_legs = [l for l in legs if l.side == 'PUT']
        call_legs = [l for l in legs if l.side == 'CALL']
        
        # Iron Condor: 2 puts + 2 calls
        if len(put_legs) == 2 and len(call_legs) == 2:
            long_put = next((l for l in put_legs if l.quantity > 0), None)
            short_put = next((l for l in put_legs if l.quantity < 0), None)
            long_call = next((l for l in call_legs if l.quantity > 0), None)
            short_call = next((l for l in call_legs if l.quantity < 0), None)
            if all([long_put, short_put, long_call, short_call]):
                is_credit = (short_put.strike > long_put.strike and
                             short_call.strike < long_call.strike)
                return ("iron_condor", is_credit)
        
        # Same-type condor: all puts or all calls
        if len(put_legs) == 4 or len(call_legs) == 4:
            same_legs = put_legs if len(put_legs) == 4 else call_legs
            sorted_legs = sorted(same_legs, key=lambda l: l.strike)
            unit = abs(sorted_legs[0].quantity)
            if unit > 0:
                ratios = [l.quantity // unit for l in sorted_legs]
                if ratios == [1, -1, -1, 1]:
                    return ("condor", True)   # long outer = debit; is_credit = False
                    # Actually: long outer, short inner condor = debit
                if ratios == [-1, 1, 1, -1]:
                    return ("condor", False)  # short outer = credit; is_credit = True
    
    return ("unknown", None)
```

> **Note on butterfly/condor `is_credit_structural`**: The direction can be determined from ratios alone for butterfly and same-type condor. For spread, it depends on which leg is more expensive — defer to market mid price. Reconcile: if `is_credit_structural is None`, fall back to `total_chunk_credit >= 0`.

### Step 2 — Refactor price computation to use `lock_floor`

Replace the current hardcoded `max(0.0, ...)` clamp with a flag returned by `classify_order_type`:

```python
order_type, is_credit_structural = self._classify_order_type(rolled_chunk_legs)
lock_floor = (order_type != "unknown")

# Use structural intent if determinable from topology, else use mid price
if is_credit_structural is not None:
    is_credit_struct = is_credit_structural
else:
    is_credit_struct = total_chunk_credit >= 0

# ... price calculation ...

if lock_floor:
    raw_price = max(0.0, raw_price)   # cannot cross to other side
# else: allow raw_price to go negative (unknown structure, mid-priced)
```

This applies to **both** the execution loop (`run_order_execution_loop`) and the UI signal builder (`get_trade_signal_payload`).

### Step 3 — Propagate `order_type` and `lock_floor` to the UI payload

Add fields to each order dict in `get_trade_signal_payload`:

```python
orders_data.append({
    ...
    "is_credit": is_credit,
    "order_type": order_type,       # "single", "spread", "butterfly", etc.
    "lock_floor": lock_floor,       # True → enforce 0.00 floor in UI
    ...
})
```

### Step 4 — Update `adjustPrice` in `app.js`

Replace hardcoded credit/debit clamping with `lock_floor`-driven logic:

```js
function adjustPrice(idx, delta) {
    const order = currentTradeOrders[idx];
    if (!order) return;
    
    const lockFloor = order.lock_floor;
    const isCredit = order.is_credit;

    if (lockFloor) {
        // Block passive adjustments that would cross $0.00
        if (isCredit && delta < 0 && order.price_ea <= 0.001) {
            alert("Cannot be more aggressive: credit limit price must stay non-negative.");
            return;
        }
        if (!isCredit && delta > 0 && order.price_ea >= -0.001) {
            alert("Cannot be more passive: debit limit price must stay non-negative.");
            return;
        }
    }

    let newPrice = Number((order.price_ea + delta).toFixed(2));

    if (lockFloor) {
        newPrice = isCredit ? Math.max(0.00, newPrice) : Math.min(0.00, newPrice);
    }
    // else: no clamp, allow any signed price for unknown structures

    order.price_ea = newPrice;
    // ... update DOM ...
}
```

### Step 5 — Add `order_type` label to the UI (optional, low priority)

Display the detected order type in the trade confirmation modal so the user can confirm what the system classified the order as. Useful for debugging misclassifications.

---

## Files Affected

| File | Change |
|------|--------|
| `server/core/monitor.py` | Add `_classify_order_type()`, update `get_trade_signal_payload()`, update execution loop price block |
| `server/static/app.js` | Update `adjustPrice()` to use `lock_floor` from payload |

---

## Edge Cases to Handle

1. **Butterfly with mixed sides (iron butterfly):** `+1P6000, -1P6050, -1C6050, +1C6100` — detect as butterfly if the middle two legs share a strike. Add strike-based detection alongside ratio detection.
2. **Unequal leg quantities after GCD unitization:** The GCD step already normalizes, so ratios should be clean integers. Verify `quantity % unit == 0` before computing ratios.
3. **Reversed iron condor (long IC):** Structural intent → DEBIT. Confirmed by `short_put.strike < long_put.strike`.
4. **Exit orders (closing trades):** Classification and floor logic applies regardless of `TradePurpose` (ENTRY vs EXIT). The broker still rejects negative debit limits on close orders.
5. **Manual price override crossing zero:** If `lock_floor=True`, clamp the override in the backend even if the UI somehow sends a crossed price. The existing `max(0.0, ...)` already handles this; make sure it's gated on `lock_floor`.
