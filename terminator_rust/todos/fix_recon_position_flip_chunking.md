# Fix: Reconciliation Generates Duplicate-Symbol Legs That Break Chunking

## Background

When the broker holds a position that the sim does not (or vice-versa), `check_reconciliation`
computes a gap and generates a reconciliation trade. The problem described here occurs when the
reconciliation must both **close** an existing broker position AND **open** a new opposing
position on the same option symbol in the same trade.

---

## What Happened (Code-Verified, log 2026-06-26 ~18:40 CT)

### Broker State vs Sim Target

| Strike | Side | Broker (Live) | Sim Target | Net Diff |
|--------|------|--------------|------------|----------|
| C7415  | CALL | -1 (short)   | 0          | +1 (close short) |
| C7420  | CALL | +1 (long)    | -2 (short) | -3 (close 1 long + open 2 shorts) |
| C7425  | CALL | 0            | -1         | -1 (open short) |
| C7435  | CALL | 0            | +2         | +2 (open long) |
| P7270  | PUT  | 0            | +2         | +2 (open long) |
| P7280  | PUT  | 0            | +1         | +1 (open long) |
| P7305  | PUT  | 0            | -2         | -2 (open short) |
| P7315  | PUT  | 0            | -1         | -1 (open short) |

For C7420: broker is long 1 and sim wants short 2. Net diff = -3.
Because this **crosses zero** (long → short), the "NO FLIP RULE" in
`check_reconciliation` (strategy.rs ~line 1907) intentionally splits this into
two separate legs instead of one:

```rust
// NO FLIP RULE: If crossing zero, split into two separate legs
if (lq < 0 && sq > 0) || (lq > 0 && sq < 0) {
    // 1. Exit portion: gets us back to 0
    needed_adjustments.push((strike, side.clone(), -lq));   // → -1 C7420
    // 2. Entry portion: target size from 0
    needed_adjustments.push((strike, side.clone(), sq));    // → -2 C7420
}
```

### The Result: 10-Leg Trade with Duplicate Symbol

The reconciliation trade sent to `get_smart_chunks` contained:

```
+1 C7415   (close broker short)
-1 C7420   (exit portion: close broker long)   ← C7420 appears TWICE
-2 C7420   (entry portion: open new short)     ← C7420 appears TWICE
-1 C7425   (open short)
+2 C7435   (open long)
+2 P7270   (open long)
+1 P7280   (open long)
-2 P7305   (open short)
-1 P7315   (open short)
```

### What `get_smart_chunks` Produced From This

`get_smart_chunks` sees two separate C7420 legs and tries to form balanced IC
structures from all 10 legs. It does not understand "close" vs "open" semantics —
it only sees quantities and tries to find matching call+put spread patterns.
The result was 4 malformed orders shown in the confirmation modal:

- **Order #1**: `[IRON_CONDOR] LONG CALL 7415 x1 | SHORT CALL 7420 x1 | LONG PUT 7270 x1 | SHORT PUT 7305 x1` — $0.00 Cr  
  **Problem**: The call side (BUY 7415 / SELL 7420) is a DEBIT call spread (not a normal IC
  short-wing structure), accidentally mixing the close legs with put legs from new ICs.

- **Orders #2 and #3**: Both involve C7420 as a selling leg (the -2 C7420 STO entry portion
  split across two chunks). These are opening shorts on a symbol where a closing order for
  the same symbol hasn't even been sent yet — a position conflict at Schwab.

- **Order #4**: A 2-leg VERTICAL `-1C7425, +1C7455` — the odd leftover.

---

## What the Correct Behavior Should Be

### Optimal First Wave (Two Simultaneous Orders)

| Order | Legs | Rationale |
|-------|------|-----------|
| #1 | `+1C7415 BTC, -1C7420 STC` | **Flatten vertical**: closes the broker's short C7415 and long C7420 in one clean 2-leg order. No new positions, no ambiguity. |
| #2 | `-1C7425, +1C7435, -1P7315, +1P7280` | **Non-conflicting IC**: uses the C7425/C7435 call spread (no C7420 involvement) and the closer-to-ATM put spread (P7280/P7315). Closer-to-ATM puts have tighter spreads and better fill probability than the far-OTM P7270/P7305 pair. |

These two orders have **no overlapping symbols** so they can be submitted simultaneously.

### Deferred Second Wave (via execution_queue after Order #1 fills)

After `+1C7415, -1C7420` fills and the broker no longer holds a long C7420:

| Order | Legs | Rationale |
|-------|------|-----------|
| #3 | `-2C7420, +2C7435, +2P7270, -2P7305` | Clean 2-lot IC, no position conflict. Submitted automatically by the execution_queue when Order #1 is confirmed filled. |

The second reconciliation wave can also be triggered by `check_reconciliation` detecting the
remaining gap after wave 1 fills, rather than relying on the execution_queue. Either path works.

---

## Root Cause Summary

The "NO FLIP RULE" was added to prevent a single `-3 C7420` leg (which would require both
`SELL_TO_CLOSE` and `SELL_TO_OPEN` in the same leg — not a valid Schwab order format).
The split into separate legs is necessary, **but the two legs should never be bundled together
into the same reconciliation IC trade**. Doing so causes `get_smart_chunks` to mix close
and open legs into incoherent structures.

---

## Fix Plan

### Approach

Instead of generating one big mixed reconciliation trade, **split the reconciliation into two
conceptually separate batches** before ever calling `get_smart_chunks`:

1. **Flatten batch**: Legs that are purely closing existing broker positions.
2. **Open batch**: Legs that are purely opening new positions (no opposing broker position).

For a flip symbol (like C7420 where `lq > 0 && sq < 0`):
- The **close portion** (`-lq = -1`) belongs in the Flatten batch.
- The **open portion** (`sq = -2`) belongs in the Open batch — but is a "deferred open" that
  must wait for the Flatten batch to fill before being submitted.

### Changes Required

#### 1. `check_reconciliation` — Separate close legs from open legs

**File:** `terminator_rust/src/strategy.rs`

After computing `needed_adjustments`, separate them into two `Vec<OptionLeg>`:

```rust
let mut close_legs: Vec<OptionLeg> = Vec::new();  // only legs closing broker positions
let mut open_legs: Vec<OptionLeg> = Vec::new();   // only legs opening new positions
```

For each `(strike, side, qty)` in `needed_adjustments`:
- If this leg closes an existing broker position (opposing direction), add to `close_legs`.
- Otherwise, add to `open_legs`.

For **flip symbols** (e.g., C7420: `lq=+1, sq=-2`):
- Close leg (`-1`): goes into `close_legs`.
- Open leg (`-2`): goes into `open_legs` and is tagged as **deferred** (must not be submitted
  until the close leg fills).

#### 2. Generate two separate trades (or one trade with the close legs promoted)

If `close_legs` is non-empty:
- **Priority option (preferred)**: Build a separate, standalone flattening order from
  `close_legs` alone. Submit it via `execute_trade` as a pure-close order. Because all its
  legs are closing existing positions, `is_closing = true` for every leg and `get_smart_chunks`
  produces a clean 2-leg vertical (or small spread).
- The `open_legs` then form the normal reconciliation IC trade. For any open leg whose symbol
  also appears in `close_legs` (the flip-open like `-2 C7420`), the existing
  `execution_queue` deferred-chunk mechanism handles deferral automatically: when
  `execute_trade` sees C7420 as a symbol being closed in another chunk, it defers the C7420
  opening chunk.

If `close_legs` is empty (no broker positions to close): behavior is unchanged — generate a
single reconciliation trade from all legs as before.

#### 3. Remove the NO FLIP RULE's two-push for the same symbol

Replace:
```rust
// NO FLIP RULE: If crossing zero, split into two separate legs
if (lq < 0 && sq > 0) || (lq > 0 && sq < 0) {
    needed_adjustments.push((strike.into_inner(), side.clone(), -lq));
    needed_adjustments.push((strike.into_inner(), side.clone(), sq));
} else {
    needed_adjustments.push((strike.into_inner(), side.clone(), diff));
}
```

With:
```rust
if (lq < 0 && sq > 0) || (lq > 0 && sq < 0) {
    // Flip: push close and open as separate legs that will be separated into
    // close_legs and open_legs batches below.
    close_legs_raw.push((strike.into_inner(), side.clone(), -lq));  // exit to zero
    open_legs_raw.push((strike.into_inner(), side.clone(), sq));    // new position
} else {
    // Simple add or reduce with no sign crossing
    if /* leg closes broker position */ {
        close_legs_raw.push((strike.into_inner(), side.clone(), diff));
    } else {
        open_legs_raw.push((strike.into_inner(), side.clone(), diff));
    }
}
```

#### 4. Confirmation UI

The flatten trade (close_legs) should appear in the confirmation modal alongside the
open IC orders, clearly labeled (e.g., "Flatten Order" vs "New IC"). Both groups are
shown before the user hits "Send Order Now." Upon confirmation, both are submitted together;
the execution_queue ensures the deferred C7420 opens wait for the flatten fill.

---

## Expected Outcome After Fix

**Scenario**: broker has {-1 C7415 short, +1 C7420 long}, sim targets 3 iron condors.

Confirmation modal shows:
- **Order #1** (Flatten): `+1C7415, -1C7420` | Qty: 1 | $X.XX Dr/Cr
- **Order #2** (New IC): `-1C7425, +1C7435, -1P7315, +1P7280` | Qty: 1 | $X.XX Cr

Both submitted simultaneously on confirm. The `-2C7420 STO` deferred chunk sits in
`execution_queue` waiting for the Filled event on Order #1's order ID. When it fires:
- **Order #3** (auto-submitted): `-2C7420, +2C7435, +2P7270, -2P7305` | Qty: 2 IC

No weird debit-spread ICs. No position-conflict rejections. No duplicate-symbol confusion
in `get_smart_chunks`.

---

## Files to Change

| File | Change |
|------|--------|
| `terminator_rust/src/strategy.rs` | `check_reconciliation`: replace NO FLIP RULE with close/open leg separation; generate flatten trade separately |
| `terminator_rust/src/web.rs` | Optionally label flatten orders differently in confirmation modal UI |
| `terminator_rust/tests/strategy_tests.rs` | Add test: broker has opposing position → confirm flatten order + IC appear separately, deferred C7420 in execution_queue |

---

## Verification

1. **Unit test**: Set broker position `+1 C7420`, sim target `-2 C7420, +1 C7435, -1 C7425, +1 P7280, -1 P7315`. Confirm reconciliation generates:
   - Flatten trade: `{-1 C7420 STC}`
   - IC trade: `{-1 C7425, +1 C7435, -1 P7315, +1 P7280}`
   - execution_queue: one deferred chunk `{-2 C7420 STO}` with `awaiting_order_id` = flatten order ID.
2. **No regression**: When broker has no opposing positions, single-trade reconciliation behavior is unchanged.
3. `cargo test` passes.
