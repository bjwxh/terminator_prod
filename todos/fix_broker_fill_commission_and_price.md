# Fix: Sim fees undercounted and realized/unrealized PnL diverges from live

## Symptoms

- Live shows correct fees ($22.60 for 20 contracts). Sim shows $4.52 for the
  same 20 contracts.
- Live and sim net PnL match closely (small gap explained by live slippage),
  but realized PnL and unrealized PnL individually differ significantly
  between live and sim, even though live fills follow sim fills within
  seconds.

## Root cause

Both issues come from the same code: the `BROKER_FILL` trade-reconciliation
path in `terminator_rust/src/strategy.rs`. When a live order fills, the
filled quantity is distributed across whichever sub-strategies are waiting
for it and injected into each one's `previous_portfolio` as a synthetic
`Trade` (this is the portfolio object used to reconcile/compare sim state
against the live book). There are several call sites that construct this
kind of synthetic trade (`strategy.rs:1948-1965`, `1971-1988`,
`2410-2427`, `2477-2484`, and one more near `2918`). All of them hardcode:

```rust
price: mid_price,      // current mid-quote, not the real fill price
commission: 0.0,       // no commission attached
```

### Bug 1 — fees undercounted

`Portfolio::fees()` (`portfolio.rs:80-82`) sums `commission` across all
trades in the portfolio. On the sim side, the only trade that carries a
real commission is the original synthetic entry from `check_entry()`
(`strategy.rs:340`), which is priced off the sub-strategy's static
`unit_size` config value (`config.json` has `"default_unit_size": 1`):

```
commission = commission_per_contract * legs.len() * unit_size
           = 1.13 * 4 * 1 = $4.52
```

All of the additional contracts that grew the real position up to 20
contracts arrive later via `BROKER_FILL` trades, which set
`commission: 0.0`. So sim's total fee stat never reflects the real
commission paid on the additional fills.

Live computes commission directly from actual filled contract counts in
`execution.rs:744` (`total_contracts * commission_per_contract`), which is
correct: `1.13 * 20 = $22.60`.

### Bug 2 — realized/unrealized PnL split diverges

`BROKER_FILL` trades mark both the trade price and the resulting position's
`entry_price` at the *current mid-quote* at the moment of reconciliation,
not the actual fill price reported by the broker. `unrealized_pnl()`
(`portfolio.rs:101-105`) is `(mark_price - entry_price) * qty * 100`, so an
incorrect `entry_price` shifts the realized/unrealized split even when net
PnL happens to converge (`net_pnl = realized + unrealized`, and net PnL is
comparatively insensitive to this error while the split is not).

### Not a bug — `INTERNAL_NETTING` trades

The `commission: 0.0` trades at `strategy.rs:1961` and `strategy.rs:1984`
are `purpose: "INTERNAL_NETTING"` — these represent two sub-strategies
netting demand against each other with no order sent to the broker, so zero
commission is correct there and should NOT be changed.

## Fix plan

1. **Pass the real fill price and per-fill commission into the
   `BROKER_FILL` / `GAP_RECON` trade constructors**, instead of hardcoding
   `mid_price` / `0.0`. This requires:
   - Confirming what fill data is available at each call site (order fill
     price and allocated commission should be derivable from the same
     event/order data that already triggers this reconciliation code, e.g.
     the order-fill event or `get_today_filled_orders` results — needs to be
     traced per call site since some sites currently only have access to
     `self.grid.quotes` mid-price, not the fill event's actual price).
   - If a call site only has quantity information and no fill price at hand
     (e.g. `2477-2484` builds a `GAP_RECON` trade purely for the purpose of
     re-submitting a deferred chunk to the broker — it isn't a completed
     fill yet, so this one may be a different case and should be reviewed
     separately from the others).
   - For commission, prorate `commission_per_contract * alloc_qty` (the
     quantity actually allocated to that sub-strategy in the fill) rather
     than 0.0, at each of the identified `BROKER_FILL` sites
     (`strategy.rs:1948-1965`, `1971-1988`, `2410-2427`, `~2918`).

2. **Leave `INTERNAL_NETTING` trades' `commission: 0.0` as-is** — those are
   not real broker fills.

3. **Verify fix**:
   - Add/extend a unit test around `Portfolio::fees()` and
     `Portfolio::unrealized_pnl()` that simulates: one `check_entry` trade
     at `unit_size=1`, followed by multiple `BROKER_FILL` trades that scale
     the position up to a larger real quantity, and assert `fees()` equals
     `commission_per_contract * total_contracts` and that `entry_price` on
     the resulting position matches the fill price, not a stale mid-quote.
   - Manually compare live vs. sim fee and realized/unrealized PnL on the
     EC2 instance for a live session after the fix, confirming the fee
     total matches contract count and the realized/unrealized split tracks
     live within a small tolerance.

## Open questions before implementing

- At each `BROKER_FILL` call site, what's the actual source of "real fill
  price" available in scope (order fill event data vs. only mid-quote)? This
  needs to be traced per call site before writing the fix — using mid-quote
  as a fallback where no real fill price is available would just move the
  bug rather than fix it.
- Should commission be prorated per allocated quantity per sub-strategy, or
  attributed to whichever sub-strategy the live order was originally placed
  for? (Matters if a single live fill gets split across multiple waiting
  sub-strategies.)
