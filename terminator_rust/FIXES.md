# Terminator Rust — Phase 1–4 Code Review Fixes

---

## 🔴 Bug 1: Timezone Assumption in `calculate_t_to_expiration` (Critical)

**File**: [src/greeks.rs:84-105](src/greeks.rs#L84-L105)

**Problem**: Uses `Local::now()` with a hardcoded `NaiveTime` of `15:00` as the SPX close. If the machine is not running in CT (e.g., running on a server in UTC or PT), the time-to-expiration `T` value passed to BSM will be wrong, producing incorrect deltas for the entire grid.

**Fix**: Add `chrono-tz` to `Cargo.toml` and use `America/Chicago` explicitly:

```toml
# Cargo.toml
chrono-tz = "0.9"
```

```rust
// greeks.rs
pub fn calculate_t_to_expiration() -> f64 {
    use chrono::TimeZone;
    use chrono_tz::America::Chicago;

    let now_ct = Chicago.from_utc_datetime(&chrono::Utc::now().naive_utc());
    let close_naive = now_ct.date_naive().and_hms_opt(15, 0, 0).unwrap();
    let close_ct = Chicago.from_local_datetime(&close_naive).single().unwrap_or(now_ct);

    let seconds_left = (close_ct - now_ct).num_seconds() as f64;
    if seconds_left <= 0.0 {
        return 1.0 / (365.0 * 24.0 * 3600.0);
    }
    seconds_left / (365.0 * 24.0 * 3600.0)
}
```

---

## 🟠 Bug 2: New `reqwest::Client` Created on Every Reconnect (Medium)

**File**: [src/websocket.rs:96](src/websocket.rs#L96)

**Problem**: `fetch_streamer_info` creates a fresh `reqwest::Client::new()` on every call. Since `fetch_streamer_info` is called at the start of every `connect_and_stream` invocation (i.e., every reconnect), a new connection pool is allocated each time. This defeats the persistent pooling goal described in the plan.

**Fix**: Add a `http_client: reqwest::Client` field to `WebsocketClient`, initialize it once in `new()`, and reuse it in `fetch_streamer_info`:

```rust
// In WebsocketClient struct
pub struct WebsocketClient {
    // ...existing fields...
    http_client: reqwest::Client,
}

// In WebsocketClient::new()
let http_client = reqwest::Client::builder()
    .timeout(Duration::from_secs(10))
    .build()?; // propagate with ?

// In fetch_streamer_info(), replace:
//   let client = reqwest::Client::new();
// with:
//   let client = &self.http_client;
```

---

## 🟠 Bug 3: Hardcoded Fallback Account Number in `config.rs` (Medium)

**File**: [src/config.rs:37](src/config.rs#L37)

**Problem**: The account number is hardcoded as a fallback string literal, directly contradicting the "Zero Hardcoded Secrets" requirement in the plan.

```rust
// Current — hardcoded fallback:
let schwab_account = std::env::var("SCHWAB_ACCOUNT")
    .unwrap_or_else(|_| "43293551".to_string());
```

**Fix**: Fail loudly if the env var is absent so misconfiguration is caught at startup, not at order placement:

```rust
let schwab_account = std::env::var("SCHWAB_ACCOUNT")
    .context("SCHWAB_ACCOUNT environment variable is not set")?;
```

---

## 🟡 Issue 4: `config` Crate Declared but Never Used (Minor)

**File**: [Cargo.toml:24](Cargo.toml#L24)

**Problem**: `config = "0.14"` is listed as a dependency but is never imported anywhere in the source. The implementation uses only `dotenvy` + plain `std::env::var`, which is simpler and works correctly.

**Fix**: Remove the unused dependency:

```toml
# Remove this line from Cargo.toml:
config = "0.14"
```

---

## 🟡 Issue 5: IV Bisection Upper Bound May Be Too Low (Minor)

**File**: [src/greeks.rs:40](src/greeks.rs#L40)

**Problem**: The bisection solver caps at `high = 5.0` (500% IV). Very short-dated OTM options in the first minutes of trading (e.g., 08:30 CT, T ≈ 6.5h) can have IVs well above 500%, especially if the market opens with a large gap. When `market_price` implies IV > 500%, the bisection returns `5.0` silently instead of the real value, producing a wrong delta.

**Fix**: Raise the cap to a safer ceiling:

```rust
// greeks.rs line 40
let mut high = 20.0; // 2000% — covers all realistic SPX 0DTE scenarios
```

---

## 🔵 Note: Strategy Supervisor in `main.rs` is a Stub (Expected — Phase 5)

**File**: [src/main.rs:133-139](src/main.rs#L133-L139)

The `acct_rx` consumer loop logs events but does not call `check_entry` or `execute_trade` from `strategy.rs`. This is expected given Phase 5 is incomplete. The `SubStrategy`, `check_entry`, and `execute_trade` implementations in `strategy.rs` are ready and well-structured; they just need to be wired into this supervisor loop.

---

## Summary

| # | Severity | File | Issue |
|---|----------|------|-------|
| 1 | 🔴 Critical | `greeks.rs` | Timezone bug — uses local time instead of CT for SPX close |
| 2 | 🟠 Medium | `websocket.rs` | New `reqwest::Client` allocated on every WS reconnect |
| 3 | 🟠 Medium | `config.rs` | Hardcoded account number fallback violates zero-secrets policy |
| 4 | 🟡 Minor | `Cargo.toml` | `config` crate declared but unused |
| 5 | 🟡 Minor | `greeks.rs` | IV bisection ceiling of 500% may miss extreme early-session 0DTE IVs |
| 6 | 🔵 Info | `main.rs` | Strategy supervisor is a logging stub pending Phase 5 wiring |
