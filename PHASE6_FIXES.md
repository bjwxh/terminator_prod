# Phase 6 Web Integration — Fix Doc

## CRITICAL

### 1. REST order handlers use account number instead of account hash
**Files:** [web.rs:424](terminator_rust/src/web.rs#L424), [web.rs:437](terminator_rust/src/web.rs#L437), [web.rs:452](terminator_rust/src/web.rs#L452)

`api_cancel_order`, `api_chase_order`, and `api_cancel_all_orders` all pass
`&state.supervisor.app_config.schwab_account` (the plain account number, e.g. `"43293551"`) as
the `account_hash` parameter to `ExecutionClient`. Schwab's REST API requires the hashed
account value from `/v1/accounts/accountNumbers`, not the number itself. Every order
operation will return a 4xx from Schwab.

**Fix:** Resolve the hash from the supervisor and return `503` if it isn't ready yet.
Apply this pattern to all three handlers:
```rust
let hash = {
    let h = state.supervisor.account_hash.lock().await;
    h.clone().unwrap_or_default()
};
if hash.is_empty() {
    return (
        StatusCode::SERVICE_UNAVAILABLE,
        axum::Json(json!({ "detail": "Account hash not yet resolved, retry in a moment" })),
    );
}
// then use &hash instead of &state.supervisor.app_config.schwab_account
```

---

### 2. `session_history` MutexGuard held for the entire WebSocket connection lifetime
**File:** [web.rs:242-243](terminator_rust/src/web.rs#L242-L243)

```rust
// CURRENT — guard is a named binding and lives to end of handle_ws_socket
let history_lock: tokio::sync::MutexGuard<'_, VecDeque<...>> =
    state.supervisor.session_history.lock().await;
let history_list: Vec<_> = history_lock.iter().map(...).collect();

let news = state.news.get_latest(50).await;  // still holding history_lock
...
tokio::select! { ... }  // still holding history_lock — entire connection!
```

`tokio::sync::MutexGuard` is `Send`, so it is kept alive across every `.await` in
the function, including the `tokio::select!` that runs for the lifetime of the socket.
While a client is connected, `tick()` and `confirm_trade()` both block forever when
they try to append a `HistoryPoint`, stalling the strategy loop.

**Fix:** Use a block to drop the guard immediately after the data is collected:
```rust
let history_list: Vec<serde_json::Value> = {
    let lock = state.supervisor.session_history.lock().await;
    lock.iter()
        .map(|hp| json!({ "ts": hp.ts, "spx": hp.spx, "live_pnl": hp.live_pnl }))
        .collect()
}; // guard dropped here
```

---

## HIGH

### 3. `confirm_trade` silently ignores `LegOverride` price adjustments
**File:** [strategy.rs:552](terminator_rust/src/strategy.rs#L552)

```rust
pub async fn confirm_trade(&self, _strat_id: &str, _overrides: Vec<LegOverride>) -> Result<()> {
```

Both parameters are prefixed with `_` and never used. The UI modal lets the user edit
per-leg fill prices before confirming; those overrides are discarded and the trade is
always booked at the originally-calculated prices.

**Fix:** Apply overrides to the fill prices passed to `add_trade`:
```rust
pub async fn confirm_trade(&self, _strat_id: &str, overrides: Vec<LegOverride>) -> Result<()> {
    let pending = { self.pending_trade.lock().await.take() };
    if let Some(t) = pending {
        // Build fill_prices, applying any per-leg overrides
        let fill_prices: Vec<f64> = t.trade.legs.iter().enumerate()
            .map(|(i, leg)| {
                overrides.iter()
                    .find(|o| o.idx == i)
                    .map(|o| o.price_ea)
                    .unwrap_or(leg.price)
            })
            .collect();

        let mut port = self.live_portfolio.lock().await;
        port.add_trade(&t.trade, Some(fill_prices));
        let pnl = port.net_pnl();
        drop(port);

        let mut history = self.session_history.lock().await;
        history.push_back(HistoryPoint {
            ts: chrono::Local::now().with_timezone(&Chicago).to_rfc3339(),
            spx: self.grid.get_spx(),
            live_pnl: pnl,
        });
        if history.len() > 1000 { history.pop_front(); }
    }
    Ok(())
}
```

---

### 4. Exchange timestamp uses wrong Schwab stream field
**File:** [parser.rs:281-288](terminator_rust/src/parser.rs#L281-L288)

```rust
// CURRENT
let time_val = entry.get("1")
    .or_else(|| entry.get("QUOTE_TIME_MILLIS"))
    .or_else(|| entry.get("TRADE_TIME_MILLIS"));
```

Field `"1"` in `LEVELONE_EQUITIES` is `BID_PRICE` (a float), not a timestamp.
The implementation also deviated from the plan which specified field `"52"`
(`QUOTE_TIME_IN_LONG`). Passing a bid-price value to `set_exchange_ts_ms()` fills
the atomic with garbage bits, making every `db_status` calculation in
`build_state_snapshot` return wrong latency/health values.

**Fix:** Restore the field number from the plan:
```rust
let time_val = entry.get("52")
    .or_else(|| entry.get("QUOTE_TIME_IN_LONG"));
if let Some(t_val) = time_val {
    if let Some(ts_ms) = t_val.as_u64() {
        grid.set_exchange_ts_ms(ts_ms);
    }
}
```

---

## MEDIUM

### 5. CORS allows port 8080 (in use by another service)
**File:** [web.rs:37-40](terminator_rust/src/web.rs#L37-L40)

The plan and user confirmed 8080 is in use. The current CORS list includes both 8080
and 8090. Remove the 8080 entries:
```rust
let cors = CorsLayer::new()
    .allow_origin([
        "http://localhost:8090".parse::<HeaderValue>().unwrap(),
        "http://127.0.0.1:8090".parse::<HeaderValue>().unwrap(),
    ])
    .allow_methods([Method::GET, Method::POST])
    .allow_headers(tower_http::cors::Any);
```

---

### 6. Duplicate `config` and `app_config` fields on `StrategySupervisor`
**File:** [strategy.rs:310-311](terminator_rust/src/strategy.rs#L310-L311), [strategy.rs:349-350](terminator_rust/src/strategy.rs#L349-L350)

```rust
pub config: crate::config::AppConfig,
pub app_config: crate::config::AppConfig,  // exact duplicate
```

Both are set to the same value at construction. `strategy.rs` uses `self.config`,
`web.rs` uses `supervisor.app_config`. Pick one name (`config`) and delete the
other. Update the two call sites in `web.rs` that reference `app_config` to use
`config` instead.

---

### 7. `api_reconnect_broker` is a no-op stub
**File:** [web.rs:405-412](terminator_rust/src/web.rs#L405-L412)

The endpoint logs a message but does not trigger any reconnection of the Schwab
WebSocket. The `WebsocketClient` needs a method or channel to signal reconnection.
For now the endpoint should at minimum return an honest status:
```rust
let res = json!({ "status": "Reconnect not yet implemented in Rust backend" });
```
Or wire up an `Arc<WebsocketClient>` into `AppState` and call a reconnect signal.

---

## LOW

### 8. `pending_trade` lock held across async send in WS handler
**File:** [web.rs:267-291](terminator_rust/src/web.rs#L267-L291)

```rust
if let Some(t) = &*state.supervisor.pending_trade.lock().await {
    // ... build rec_str ...
    let _ = sender.send(Message::Text(rec_str)).await;  // await while holding lock
}
```

The guard extends into the `sender.send().await` call. This is not a deadlock but
holds `pending_trade` during a potentially slow async socket write. Fix by cloning
out of the guard first:
```rust
let pending = state.supervisor.pending_trade.lock().await.clone();
if let Some(t) = pending {
    // build and send reconnect_payload using t
}
```
