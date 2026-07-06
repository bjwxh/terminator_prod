# Fix: Position Flip — Wrong Instructions + Wrong Submission Order

## What Happened (Code-Verified)

### Setup
- Broker had pre-existing position: **-1C7385 (short), +1C7425 (long)**
- Sim target: **-3C7425, +2C7460, +1C7465, -3P7300, +3P7270**
- Net diff fed to GAP_RECON: **+1C7385, -4C7425, +2C7460, +1C7465, -3P7300, +3P7270**

### What get_smart_chunks Produced
After `unroll_legs`, iron condor extraction grouped them into three chunks (before merging):
1. **Chunk A** (Iron Condor): `[+1C7385, -1C7425, -1P7300, +1P7270]` — contains both closing legs
2. **Chunk B** (Iron Condor × 2, merged): `[-2C7425, +2C7460, -2P7300, +2P7270]` — pure open
3. **Chunk C** (Vertical): `[-1C7425, +1C7465]` — coincidentally shares the C7425 closing leg

### Bug 1 — Wrong Instructions (BUY_TO_OPEN / SELL_TO_OPEN instead of TO_CLOSE)

In `confirm_trade` (line 2253), `positions` passed to `execute_trade` comes from `live_portfolio` (the SIM target state, not the broker state):
```rust
let positions = {
    let live_port = self.live_portfolio.lock().await;
    live_port.positions.clone()  // ← SIM, not broker!
};
```

In `execute_trade` (line 1004), `is_closing` compares leg direction against these SIM positions:
- SIM has C7385 at **0** (not in sim) → `is_closing = false` → **BUY_TO_OPEN** (wrong; broker has -1C7385 short)
- SIM has C7425 at **-3** (same sign as the -1C7425 leg) → `is_closing = false` → **SELL_TO_OPEN** (wrong; broker has +1C7425 long)

Schwab rejects Chunk A (iron condor) because BUY_TO_OPEN C7385 conflicts with the existing short C7385.

### Bug 2 — Closing Quota Not Tracked Across Chunks

Even after fixing Bug 1 (using broker positions), all four -1C7425 legs (across Chunks A, B, C)
would see `broker has +1C7425 → is_closing = true` → all four tagged **SELL_TO_CLOSE**.
Only ONE should be SELL_TO_CLOSE (the one closing the +1C7425 long); the other three are new
shorts and must be SELL_TO_OPEN.

### Bug 3 — All Chunks Submitted Simultaneously; Wrong Priority Order

`execute_trade` submits Chunks A, B, C in the same call, one after another. Schwab processes
them independently and near-simultaneously. When Chunk A (which would SELL_TO_CLOSE C7425)
hasn't filled yet, Schwab rejects Chunk B's SELL_TO_OPEN C7425 legs as a position conflict.

The vertical (Chunk C: -1C7425, +1C7465) was accepted only because it doesn't involve C7385
and Schwab's validation was more lenient for a 2-leg call spread vs a 4-leg iron condor with
a mixed-instruction leg.

**User requirement**: Send position-closing orders FIRST. After they fill and flatten the
conflicting position, the next reconciliation cycle proposes the new iron condors.

---

## Fix Plan

### Fix 1 — Use broker positions for `is_closing` (line 2253 in `confirm_trade`)

**File:** `terminator_rust/src/strategy.rs`

Change:
```rust
let positions = {
    let live_port = self.live_portfolio.lock().await;
    live_port.positions.clone()
};
```

To:
```rust
let positions = {
    let broker_port = self.broker_portfolio.lock().await;
    broker_port.positions.clone()
};
```

This gives Chunk A the correct instructions:
- `+1C7385` → BUY_TO_CLOSE ✓ (broker has -1C7385 short)
- `-1C7425` (first leg) → SELL_TO_CLOSE ✓ (broker has +1C7425 long)

---

### Fix 2 — Track closing quota per symbol across all chunks (inside `execute_trade`)

Before the chunk loop, pre-compute how many contracts can be closed per symbol:
```rust
// closing_remaining[symbol] = how many contracts at the broker are "opposing" the trade
let mut closing_remaining: std::collections::HashMap<String, i32> = std::collections::HashMap::new();
for p in positions {
    closing_remaining.insert(p.symbol.clone(), p.quantity.abs());
}
```

Inside the chunk loop, when building `legs_collection`, replace the binary `is_closing` check with a quota-aware one:
```rust
let is_closing = {
    let remaining = closing_remaining.get(&leg.symbol).copied().unwrap_or(0);
    let broker_opposing = positions.iter().any(|p|
        p.symbol == leg.symbol && (p.quantity as f64).signum() != (leg.quantity as f64).signum()
    );
    broker_opposing && remaining > 0
};

if is_closing {
    *closing_remaining.entry(leg.symbol.clone()).or_insert(0) -= 1;
}
```

**Effect**: For C7425, only the FIRST -1C7425 leg (in Chunk A) gets SELL_TO_CLOSE; the
remaining three (Chunks B and C) get SELL_TO_OPEN. The quota is shared across chunks because
the loop processes chunks in order.

---

### Fix 3 — Submit closing chunks first; defer pure-opening chunks to next reconciliation

After building all chunks (but before submitting), separate them:
```rust
// A chunk is "conflict-resolving" if it contains ≥1 leg that is closing a broker position.
// The positions snapshot and closing_remaining are reset here for classification only.
let conflict_chunks: Vec<usize> = chunks.iter().enumerate()
    .filter(|(_, chunk)| chunk.iter().any(|leg| {
        positions.iter().any(|p|
            p.symbol == leg.symbol && (p.quantity as f64).signum() != (leg.quantity as f64).signum()
        )
    }))
    .map(|(i, _)| i)
    .collect();

let has_conflicts = !conflict_chunks.is_empty();
```

Then in the chunk submission loop:
```rust
for (i, chunk) in chunks.iter().enumerate() {
    // If there are position conflicts, skip pure-opening chunks.
    // They will be proposed by the next check_reconciliation cycle after the
    // conflict-resolving chunks fill and flatten the opposing broker positions.
    if has_conflicts && !conflict_chunks.contains(&i) {
        info!("Deferring chunk {} (pure opening) until position conflicts are resolved", i);
        continue;
    }
    // ... normal submission path
}
```

**Effect**:
- Only Chunk A (has C7385 + C7425 closing legs) is submitted now
- Chunks B and C are deferred
- After Chunk A fills, broker no longer has -1C7385 or +1C7425
- Next `check_reconciliation` sees the remaining gap (-3C7425, +2C7460, +1C7465, -2P7300, +2P7270 still needed) and pops up a new GAP_RECON for the pure opening iron condors
- No position conflicts at that point → all 3 iron condors submitted cleanly

---

## Edge Cases

- **No conflicts** (normal case): `conflict_chunks` is empty → `has_conflicts = false` → all chunks submitted as before. No change to existing behavior.
- **Multiple closing symbols**: Each gets its own quota. Chunk A handles both C7385 and C7425 closings. ✓
- **Partial broker position**: e.g., broker has +2C7425 and trade needs -4C7425 → 2 legs tagged SELL_TO_CLOSE, 2 tagged SELL_TO_OPEN. ✓
- **Dry run**: Same logic applies; DRY_RUN_FILL allocations still work because positions/quotas are consistent.

## Verification

1. With pre-existing -1C7385, +1C7425 at broker and sim wanting 3 new iron condors:
   - Confirmation modal shows 3 orders but only Order #1 (Chunk A, the conflict-resolving iron condor) is actually submitted
   - After Chunk A fills: a new GAP_RECON pops up with the remaining 2 iron condors + vertical
   - No position-conflict rejections from Schwab
2. Normal entry with no pre-existing opposing positions: all 3 orders submitted at once, behavior unchanged
3. `cargo test` must pass
