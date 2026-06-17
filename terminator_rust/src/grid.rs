use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;
use dashmap::DashMap;
use ordered_float::OrderedFloat;
use tracing::info;

use crate::options_chain::StrikeAndSide;
use crate::db::OptionQuoteRow;

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
    /// Concurrent striped map: Strike -> Call/Put OptionQuote
    pub quotes: Arc<DashMap<OrderedFloat<f64>, OptionQuote>>,
    /// Reverse symbol lookup mapping Option Symbol -> Strike price and is_call side
    pub symbol_lookup: HashMap<String, StrikeAndSide>,
    /// Thread-safe lock-free storage for the latest SPX underlying index price
    pub underlying_price: std::sync::atomic::AtomicU64,
    pub vix: std::sync::atomic::AtomicU64,
    pub exchange_ts_ms: std::sync::atomic::AtomicU64,
    pub subscribed_option_symbols: std::sync::RwLock<std::collections::HashSet<String>>,
}

impl OptionsGrid {
    pub fn new(symbol_map: HashMap<StrikeAndSide, String>) -> Self {
        // Build reverse symbol lookup map
        let mut symbol_lookup = HashMap::new();
        for (key, sym) in &symbol_map {
            symbol_lookup.insert(sym.clone(), key.clone());
        }

        info!("Options pricing grid initialized with {} symbols in dynamic registry.", symbol_lookup.len());

        Self {
            quotes: Arc::new(DashMap::new()),
            symbol_lookup,
            underlying_price: std::sync::atomic::AtomicU64::new(0.0f64.to_bits()),
            vix: std::sync::atomic::AtomicU64::new(0.0f64.to_bits()),
            exchange_ts_ms: std::sync::atomic::AtomicU64::new(0),
            subscribed_option_symbols: std::sync::RwLock::new(std::collections::HashSet::new()),
        }
    }

    /// Read the latest SPX underlying price thread-safely and lock-free
    pub fn get_underlying_price(&self) -> f64 {
        f64::from_bits(self.underlying_price.load(std::sync::atomic::Ordering::Relaxed))
    }

    /// Set the latest SPX underlying price thread-safely and lock-free
    pub fn set_underlying_price(&self, price: f64) {
        self.underlying_price.store(price.to_bits(), std::sync::atomic::Ordering::Relaxed);
    }

    pub fn get_spx(&self) -> f64 {
        self.get_underlying_price()
    }

    pub fn get_vix(&self) -> f64 {
        f64::from_bits(self.vix.load(std::sync::atomic::Ordering::Relaxed))
    }

    pub fn set_vix(&self, price: f64) {
        self.vix.store(price.to_bits(), std::sync::atomic::Ordering::Relaxed);
    }

    pub fn get_exchange_ts_ms(&self) -> u64 {
        self.exchange_ts_ms.load(std::sync::atomic::Ordering::Relaxed)
    }

    pub fn set_exchange_ts_ms(&self, ts: u64) {
        self.exchange_ts_ms.store(ts, std::sync::atomic::Ordering::Relaxed);
    }

    /// Recalculates Greeks for all active options in the grid when the underlying index ($SPX) moves.
    pub fn update_underlying(&self, spx_price: f64) {
        if spx_price <= 0.0 {
            return;
        }

        let t = crate::greeks::calculate_t_to_expiration();
        let r = 0.0525; // standard short term risk-free rate estimate

        for mut entry in self.quotes.iter_mut() {
            let strike = entry.key().0;
            let quote = entry.value_mut();

            if let Some(ref mut call) = quote.call {
                if call.mid > 0.0 {
                    // TODO: Document known divergence - Rust computes delta via BS over mid price; Python uses Schwab stream.
                    call.delta = crate::greeks::calculate_delta(call.mid, spx_price, strike, t, r, true);
                    call.theta = 0.0; // TODO: Compute actual BS theta or stream it from Schwab instead of using mid
                }
            }

            if let Some(ref mut put) = quote.put {
                if put.mid > 0.0 {
                    // TODO: Document known divergence - Rust computes delta via BS over mid price; Python uses Schwab stream.
                    put.delta = crate::greeks::calculate_delta(put.mid, spx_price, strike, t, r, false);
                    put.theta = 0.0; // TODO: Compute actual BS theta or stream it from Schwab instead of using mid
                }
            }
            quote.last_updated = Instant::now();
        }
    }

    /// Updates individual option bids and asks, preserving existing quotes for partial updates.
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

            // Extract existing leg quote values if available to support partial updates
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

            // Apply updates
            if let Some(b) = bid {
                current_bid = b;
            }
            if let Some(a) = ask {
                current_ask = a;
            }

            let mid = (current_bid + current_ask) / 2.0;
            let t = crate::greeks::calculate_t_to_expiration();
            let r = 0.0525;

            let delta = if spx_price > 0.0 && mid > 0.0 {
                crate::greeks::calculate_delta(mid, spx_price, strike.0, t, r, is_call)
            } else {
                0.0
            };

            let leg = OptionLegQuote {
                symbol: symbol.to_string(),
                bid: current_bid,
                ask: current_ask,
                mid,
                delta,
                theta: 0.0, // TODO: Compute actual BS theta or stream it from Schwab instead of using mid
                last_update: Instant::now(),
            };

            if is_call {
                quote.call = Some(leg);
            } else {
                quote.put = Some(leg);
            }
            quote.last_updated = Instant::now();
            self.subscribed_option_symbols.write().unwrap().insert(symbol.to_string());
        }
    }
    pub fn inject_snapshot(&self, quotes: &[OptionQuoteRow], spx: f64) {
        self.set_underlying_price(spx);
        for q in quotes {
            let strike = OrderedFloat(q.strike);
            let is_call = q.side == "CALL";
            let mid = (q.bid + q.ask) / 2.0;

            let leg = OptionLegQuote {
                symbol: q.symbol.clone(),
                bid: q.bid,
                ask: q.ask,
                mid,
                delta: q.delta,
                theta: q.theta,
                last_update: Instant::now(),
            };

            let mut entry = self.quotes.entry(strike).or_insert_with(|| OptionQuote {
                strike: q.strike,
                call: None,
                put: None,
                last_updated: Instant::now(),
            });

            if is_call {
                entry.value_mut().call = Some(leg);
            } else {
                entry.value_mut().put = Some(leg);
            }
            entry.value_mut().last_updated = Instant::now();
        }
    }
}
