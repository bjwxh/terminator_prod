use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;
use dashmap::DashMap;
use ordered_float::OrderedFloat;

#[derive(Debug, Clone, Hash, Eq, PartialEq)]
pub struct StrikeAndSide {
    pub strike: OrderedFloat<f64>,
    pub is_call: bool,
}

#[derive(Debug, Clone)]
pub struct OptionLegQuote {
    pub symbol: String,
    pub bid: f64,
    pub ask: f64,
    pub mid: f64,
    pub delta: f64,
    pub theta: f64,
    pub last_update: Instant,
}

#[derive(Debug, Clone)]
pub struct OptionQuote {
    pub strike: f64,
    pub call: Option<OptionLegQuote>,
    pub put: Option<OptionLegQuote>,
    pub last_updated: Instant,
}

pub struct OptionsGrid {
    pub quotes: Arc<DashMap<OrderedFloat<f64>, OptionQuote>>,
    pub symbol_lookup: HashMap<String, StrikeAndSide>,
    pub underlying_price: std::sync::atomic::AtomicU64,
}

impl OptionsGrid {
    pub fn new(symbol_map: HashMap<StrikeAndSide, String>) -> Self {
        let mut symbol_lookup = HashMap::new();
        for (key, sym) in &symbol_map {
            symbol_lookup.insert(sym.clone(), key.clone());
        }
        Self {
            quotes: Arc::new(DashMap::new()),
            symbol_lookup,
            underlying_price: std::sync::atomic::AtomicU64::new(0.0f64.to_bits()),
        }
    }

    pub fn get_underlying_price(&self) -> f64 {
        f64::from_bits(self.underlying_price.load(std::sync::atomic::Ordering::Relaxed))
    }

    pub fn set_underlying_price(&self, price: f64) {
        self.underlying_price.store(price.to_bits(), std::sync::atomic::Ordering::Relaxed);
    }

    pub fn update_underlying(&self, spx_price: f64) {
        if spx_price <= 0.0 {
            return;
        }
        let t = calculate_t_to_expiration();
        let r = 0.0525;
        for mut entry in self.quotes.iter_mut() {
            let strike = entry.key().0;
            let quote = entry.value_mut();
            if let Some(ref mut call) = quote.call {
                if call.mid > 0.0 {
                    call.delta = calculate_delta(call.mid, spx_price, strike, t, r, true);
                    call.theta = call.mid;
                }
            }
            if let Some(ref mut put) = quote.put {
                if put.mid > 0.0 {
                    put.delta = calculate_delta(put.mid, spx_price, strike, t, r, false);
                    put.theta = put.mid;
                }
            }
            quote.last_updated = Instant::now();
        }
    }

    pub fn update_option(&self, symbol: &str, bid: Option<f64>, ask: Option<f64>, spx_price: f64) {
        if let Some(lookup) = self.symbol_lookup.get(symbol) {
            let strike = lookup.strike;
            let is_call = lookup.is_call;

            let mut entry = self.quotes.entry(strike).or_insert_with(|| OptionQuote {
                strike: strike.0,
                call: None,
                put: None,
                last_updated: Instant::now(),
            });

            let quote = entry.value_mut();
            let (mut current_bid, mut current_ask) = if is_call {
                if let Some(ref call) = quote.call {
                    (call.bid, call.ask)
                } else {
                    (0.0, 0.0)
                }
            } else {
                if let Some(ref put) = quote.put {
                    (put.bid, put.ask)
                } else {
                    (0.0, 0.0)
                }
            };

            if let Some(b) = bid {
                current_bid = b;
            }
            if let Some(a) = ask {
                current_ask = a;
            }

            let mid = (current_bid + current_ask) / 2.0;
            let t = calculate_t_to_expiration();
            let r = 0.0525;

            let delta = if spx_price > 0.0 && mid > 0.0 {
                calculate_delta(mid, spx_price, strike.0, t, r, is_call)
            } else {
                0.0
            };

            let leg = OptionLegQuote {
                symbol: symbol.to_string(),
                bid: current_bid,
                ask: current_ask,
                mid,
                delta,
                theta: mid,
                last_update: Instant::now(),
            };

            if is_call {
                quote.call = Some(leg);
            } else {
                quote.put = Some(leg);
            }
            quote.last_updated = Instant::now();
        }
    }
}

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

pub fn implied_volatility(market_price: f64, s: f64, k: f64, t: f64, r: f64, is_call: bool) -> f64 {
    let mut low = 1e-4;
    let mut high = 5.0;
    let intrinsic = if is_call { (s - k).max(0.0) } else { (k - s).max(0.0) };
    if market_price <= intrinsic {
        return low;
    }
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

pub fn calculate_delta(market_price: f64, s: f64, k: f64, t: f64, r: f64, is_call: bool) -> f64 {
    let iv = implied_volatility(market_price, s, k, t, r, is_call);
    black_scholes_delta(s, k, t, r, iv, is_call)
}

pub fn calculate_t_to_expiration() -> f64 {
    use chrono::{Local, TimeZone, NaiveTime};
    let now = Local::now();
    let close_time = NaiveTime::from_hms_opt(15, 0, 0).unwrap();
    let today_close = now.date_naive().and_time(close_time);
    let close_dt = match Local.from_local_datetime(&today_close) {
        chrono::LocalResult::Single(dt) => dt,
        _ => now,
    };
    let seconds_left = (close_dt - now).num_seconds() as f64;
    if seconds_left <= 0.0 {
        return 1.0 / (365.0 * 24.0 * 3600.0);
    }
    seconds_left / (365.0 * 24.0 * 3600.0)
}

pub fn parse_streaming_message(txt: &str, grid: &OptionsGrid) -> Option<f64> {
    let val: serde_json::Value = serde_json::from_str(txt).unwrap();
    let mut new_spx = None;
    if let Some(data_array) = val.get("data").and_then(|d| d.as_array()) {
        for msg in data_array {
            let service = msg.get("service").and_then(|s| s.as_str()).unwrap_or_default();
            let content_array = msg.get("content").and_then(|c| c.as_array()).unwrap();
            if service == "LEVELONE_EQUITIES" {
                for entry in content_array {
                    let key = entry.get("key").and_then(|k| k.as_str()).unwrap_or_default();
                    if key == "$SPX" {
                        let price_val = entry.get("3").or_else(|| entry.get("LAST_PRICE"));
                        if let Some(p_val) = price_val {
                            if let Some(price) = p_val.as_f64() {
                                grid.set_underlying_price(price);
                                grid.update_underlying(price);
                                new_spx = Some(price);
                            }
                        }
                    }
                }
            } else if service == "LEVELONE_OPTIONS" {
                let spx_price = grid.get_underlying_price();
                for entry in content_array {
                    let key = entry.get("key").and_then(|k| k.as_str()).unwrap_or_default();
                    let bid_val = entry.get("2").or_else(|| entry.get("BID_PRICE"));
                    let ask_val = entry.get("3").or_else(|| entry.get("ASK_PRICE"));
                    let bid = bid_val.and_then(|b| b.as_f64());
                    let ask = ask_val.and_then(|a| a.as_f64());
                    if bid.is_some() || ask.is_some() {
                        grid.update_option(key, bid, ask, spx_price);
                    }
                }
            }
        }
    }
    new_spx
}

fn main() {
    println!("Step 1: Setup OptionsGrid...");
    let mut symbol_map = HashMap::new();
    symbol_map.insert(
        StrikeAndSide {
            strike: OrderedFloat(5300.0),
            is_call: true,
        },
        "SPXW  260522C05300000".to_string(),
    );
    symbol_map.insert(
        StrikeAndSide {
            strike: OrderedFloat(5300.0),
            is_call: false,
        },
        "SPXW  260522P05300000".to_string(),
    );
    let grid = OptionsGrid::new(symbol_map);

    println!("Step 2: Parse LEVELONE_EQUITIES...");
    let eq_update = r#"{
        "data": [
            {
                "service": "LEVELONE_EQUITIES",
                "content": [
                    {
                        "key": "$SPX",
                        "3": 5302.50
                    }
                  ]
            }
        ]
    }"#;
    let new_spx = parse_streaming_message(eq_update, &grid);
    println!("New SPX: {:?}", new_spx);

    println!("Step 3: Parse LEVELONE_OPTIONS...");
    let opt_update = r#"{
        "data": [
            {
                "service": "LEVELONE_OPTIONS",
                "content": [
                    {
                        "key": "SPXW  260522C05300000",
                        "2": 10.00,
                        "3": 11.00
                    }
                ]
            }
        ]
    }"#;
    parse_streaming_message(opt_update, &grid);
    println!("Successfully parsed option updates!");

    println!("Step 4: Retrieve options quotes...");
    let strike_key = OrderedFloat(5300.0);
    let quote = grid.quotes.get(&strike_key).unwrap();
    let call = quote.call.as_ref().unwrap();
    println!("Call delta calculated: {}", call.delta);
    println!("Diagnostics complete!");
}
