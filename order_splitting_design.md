# Order Splitting & Gap Reconciliation Design

## 1. Problem Statement
During high-volatility events (e.g., the SPX spike on March 31, 11:38 AM), a gap can form between the simulated portfolio and the live broker positions if the trading system lags or is disabled/re-enabled. To "bridge" this gap, the application must reconcile the **Frozen Live** state with the **Current Sim** state.

## 2. Core Architectural Principles
To ensure execution stability and safety, the following rules MUST be followed:

1. **The "No-Flip" Rule**: A single order cannot take a position from negative to positive (or vice-versa). 
   - *Example*: Crossing from -2 to +1.
   - *Solution*: Must be split into a `BUY_TO_CLOSE` (2 units) in Order A, and a `BUY_TO_OPEN` (1 unit) in Order B.
2. **Hedging Priority**: Orders should prioritize balanced spreads (Long/Short pairs) to maintain delta-net-neutrality during the execution sequence.
3. **Instruction Clarity**: A single order must consist of unique symbols to avoid instruction ambiguity at the broker level.

## 4. Prioritized Splitting Hierarchy
When dividing a large set of adjustments into chunks (limited to 4 legs by Schwab), `_get_smart_chunks` should perform a greedy search in this order:

| Priority | Structure | Description |
| :--- | :--- | :--- |
| **1** | **Iron Condor** | 4 unique strikes (SC+LC+SP+LP). Best for full entry/exit. |
| **2** | **Side-Specific Roll**| 4 unique strikes on one side (e.g., 4 CALLS). Pairs 1 exit vertical with 1 entry vertical. |
| **3** | **Vertical Spread** | 2 unique strikes (1 Long + 1 Short). The fallback unit of risk. |
| **4** | **Residuals** | Any remaining legs that cannot be paired. |

---

## 5. Proposed Implementation (`monitor.py`)

### A. Detecting Flipped Positions
In `_check_reconciliation`, detect when a position crosses zero and split it into two legs:

```python
# monitor.py -> _check_reconciliation
if (lq < 0 and sq > 0) or (lq > 0 and sq < 0):
    self.logger.info(f"Flipping detected for {k}: Splitting leg.")
    # 1. Exit portion: suggest the size that gets us back to 0
    needed_adjustments.append((k[0], k[1], -lq))
    # 2. Entry portion: suggest the target size from 0
    needed_adjustments.append((k[0], k[1], sq))
```

### B. The Greedy Chunk Search
The `_get_smart_chunks` function should implement the hierarchy using `itertools.combinations` for a deterministic search:

```python
def _get_smart_chunks(self, legs: List[OptionLeg]) -> List[List[OptionLeg]]:
    unrolled = self._unroll_legs(legs)
    if not unrolled: return []
    
    chunks = []
    remaining = list(unrolled)

    def extract_chunk(num_legs, constraint_func):
        nonlocal remaining
        for indices in combinations(range(len(remaining)), num_legs):
            combo = [remaining[indices[i]] for i in range(num_legs)]
            
            # UNIQUE STRIKE RULE: No repeated strikes in one order
            strike_keys = {(l.strike, l.side) for l in combo}
            if len(strike_keys) != num_legs: continue
            
            if constraint_func(combo):
                matched = combo
                for i in sorted(indices, reverse=True):
                    remaining.pop(i)
                return matched
        return None

    # Priority 1: Iron Condors
    while len(remaining) >= 4:
        ic = extract_chunk(4, lambda c: ...) # IC Logic
        if not ic: break
        chunks.append(ic)

    # Priority 2: Side-Specific Condors / Rolls
    while len(remaining) >= 4:
        roll = extract_chunk(4, lambda c: sum(l.qty > 0) == 2 and sum(l.qty < 0) == 2)
        if not roll: break
        chunks.append(roll)

    # Priority 3: Vertical Spreads
    while len(remaining) >= 2:
        vs = extract_chunk(2, lambda c: sum(l.qty > 0) == 1 and sum(l.qty < 0) == 1)
        if not vs: break
        chunks.append(vs)

    return chunks
```

## 6. Verification Case Study
**Scenario**: Bridge `(-1 C6500, -1 C6520, +2 C6550)` to `(+1 C6500, -2 C6520, +1 C6550)`.
1. **Order 1 (Reduction)**: `+1 C6500 (BTC), -1 C6550 (STC)`. (Safe Roll).
2. **Order 2 (Entry)**: `+1 C6500 (BTO), -1 C6520 (STO)`. (Safe Spread).
*Net Result*: Successfully flipped 6500 from short to long without a naked exposure period or broker rejection.
