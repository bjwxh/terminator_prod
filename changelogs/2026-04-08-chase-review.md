# Chase Feature — Code Review Issues & Solutions

## 1. Float Rounding Bug in `chase_order()` (Medium)

**File:** `server/core/monitor.py:1889-1891`

**Problem:** `math.ceil/floor` on `abs_mark / 0.05` is vulnerable to floating-point imprecision. For example, `5.25 / 0.05` evaluates to `104.99999...` in IEEE 754, causing `math.floor` to return `104` and produce `$5.20` instead of the correct `$5.25`.

**Solution:** Use `Decimal` for the rounding arithmetic:

```python
from decimal import Decimal, ROUND_UP, ROUND_DOWN

abs_mark_d = Decimal(str(abs_mark))
if is_credit:
    new_abs_price = float(
        (abs_mark_d / Decimal('0.05')).to_integral_value(rounding=ROUND_UP) * Decimal('0.05')
    )
else:
    new_abs_price = float(
        (abs_mark_d / Decimal('0.05')).to_integral_value(rounding=ROUND_DOWN) * Decimal('0.05')
    )
```

---

## 2. Lost Strategy Mapping if `new_id` is None (Medium)

**File:** `server/core/monitor.py:1938-1941`

**Problem:** After a successful `replace_order`, the old `order_id` is unconditionally popped from `order_to_strategy`. If `new_id` is `None` (which can happen — Schwab doesn't always return the new ID in headers), the strategy mapping is permanently lost for the remainder of the session, breaking any downstream logic that looks up a strategy by order ID.

**Solution:** Restore the old mapping if no new ID is returned, so it stays intact until the next broker sync resolves the correct new order ID:

```python
if order_id in self.order_to_strategy:
    sid = self.order_to_strategy.pop(order_id)
    if new_id:
        self.order_to_strategy[str(new_id)] = sid
    else:
        self.order_to_strategy[order_id] = sid  # restore until next sync
        self.logger.warning(f"No new order ID returned for chase of {order_id}. Mapping retained.")
```

---

## 3. Typo: "order salt" in Chase Confirmation Modal (Low)

**File:** `server/static/app.js:1085`

**Problem:** The modal description reads `"order salt #${orderId}"` — "salt" is a stray word.

**Solution:**

```js
// Before
`Are you sure you want to <strong>Chase</strong> order salt <strong>#${orderId}</strong> ...`

// After
`Are you sure you want to <strong>Chase</strong> order <strong>#${orderId}</strong> ...`
```

---

## 4. No Table Refresh After Successful Chase (Low)

**File:** `server/static/app.js:1087-1095`

**Problem:** After a successful chase, the modal closes but the Working Orders table is not refreshed. The user sees the old price until the next auto-poll cycle fires.

**Solution:** Trigger a table refresh immediately after closing the modal:

```js
fetch(`/api/orders/${orderId}/chase`, { method: 'POST' })
    .then(resp => {
        if (!resp.ok) throw new Error("Chase failed");
        closeConfirmModal();
        loadWorkingOrders(); // refresh table immediately
    })
    .catch(err => {
        alert("Error chasing order: " + err.message);
    });
```

---

## 5. Dev Script in Repo Root (Low)

**File:** `test_chase_replacement.py`

**Problem:** The one-off development/debug script used to manually test the `replace_order` API is sitting in the repo root. It hardcodes assumptions (looks for a 4-leg SPX Iron Condor at ~$6.85) and directly initializes a live `LiveTradingMonitor`, making it unsuitable for automated testing.

**Solution:** Either move it to a `scripts/` folder with a clear name (e.g., `scripts/dev_test_chase.py`) and add a comment at the top noting it is a manual dev tool, or remove it entirely if it is no longer needed now that the feature is integrated.
