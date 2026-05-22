/// Highly accurate approximation of the cumulative standard normal distribution N(x).
fn ndtr(x: f64) -> f64 {
    let a1 = 0.319381530;
    let a2 = -0.356563782;
    let a3 = 1.781477937;
    let a4 = -1.821255978;
    let a5 = 1.330274429;
    let l = x.abs();
    let k = 1.0 / (1.0 + 0.2316419 * l);
    let mut w = 1.0 - 1.0 / (2.0 * std::f64::consts::PI).sqrt() * (-l * l / 2.0).exp() 
        * (a1 * k + a2 * k.powi(2) + a3 * k.powi(3) + a4 * k.powi(4) + a5 * k.powi(5));
    if x < 0.0 {
        w = 1.0 - w;
    }
    w
}

/// Black-Scholes-Merton pricing formula for European options.
pub fn black_scholes_price(s: f64, k: f64, t: f64, r: f64, sigma: f64, is_call: bool) -> f64 {
    if t <= 0.0 {
        if is_call {
            return (s - k).max(0.0);
        } else {
            return (k - s).max(0.0);
        }
    }
    let d1 = ((s / k).ln() + (r + 0.5 * sigma * sigma) * t) / (sigma * t.sqrt());
    let d2 = d1 - sigma * t.sqrt();
    if is_call {
        s * ndtr(d1) - k * (-r * t).exp() * ndtr(d2)
    } else {
        k * (-r * t).exp() * ndtr(-d2) - s * ndtr(-d1)
    }
}

/// Newton-Raphson & Bisection hybrid solver for European Implied Volatility (IV).
pub fn implied_volatility(market_price: f64, s: f64, k: f64, t: f64, r: f64, is_call: bool) -> f64 {
    let mut low = 1e-4;
    let mut high = 20.0; // 2000% — covers all realistic SPX 0DTE scenarios
    
    // Intrinsic value boundary check
    let intrinsic = if is_call { (s - k).max(0.0) } else { (k - s).max(0.0) };
    if market_price <= intrinsic {
        return low;
    }

    // 40 iterations of Bisection converges to very high precision
    for _ in 0..40 {
        let mid = (low + high) / 2.0;
        let price = black_scholes_price(s, k, t, r, mid, is_call);
        if price < market_price {
            low = mid;
        } else {
            high = mid;
        }
    }
    (low + high) / 2.0
}

/// Black-Scholes-Merton Delta formula.
pub fn black_scholes_delta(s: f64, k: f64, t: f64, r: f64, sigma: f64, is_call: bool) -> f64 {
    if t <= 0.0 {
        if is_call {
            return if s >= k { 1.0 } else { 0.0 };
        } else {
            return if s < k { -1.0 } else { 0.0 };
        }
    }
    let d1 = ((s / k).ln() + (r + 0.5 * sigma * sigma) * t) / (sigma * t.sqrt());
    if is_call {
        ndtr(d1)
    } else {
        ndtr(d1) - 1.0
    }
}

/// Calculates local Delta from the option's current market midprice.
pub fn calculate_delta(market_price: f64, s: f64, k: f64, t: f64, r: f64, is_call: bool) -> f64 {
    let iv = implied_volatility(market_price, s, k, t, r, is_call);
    black_scholes_delta(s, k, t, r, iv, is_call)
}

/// Calculate time to expiration T (in years) based on Chicago market close (3:00 PM CST).
pub fn calculate_t_to_expiration() -> f64 {
    if std::env::var("TERMINATOR_TEST_ENV").is_ok() {
        if let Ok(t_str) = std::env::var("TERMINATOR_TEST_T") {
            if let Ok(t_val) = t_str.parse::<f64>() {
                return t_val;
            }
        }
        // During unit or integration tests, return a deterministic T corresponding to 8:30 AM CT (6.5 hours / 23400 seconds remaining)
        return 23400.0 / (365.0 * 24.0 * 3600.0);
    }

    use chrono::{TimeZone, Utc};
    use chrono_tz::America::Chicago;

    let now_ct = Chicago.from_utc_datetime(&Utc::now().naive_utc());
    let close_naive = now_ct.date_naive().and_hms_opt(15, 0, 0).unwrap();
    let close_ct = match Chicago.from_local_datetime(&close_naive) {
        chrono::LocalResult::Single(dt) => dt,
        _ => now_ct,
    };

    let seconds_left = (close_ct - now_ct).num_seconds() as f64;
    if seconds_left <= 0.0 {
        return 1.0 / (365.0 * 24.0 * 3600.0);
    }
    seconds_left / (365.0 * 24.0 * 3600.0)
}
