use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;
use dashmap::DashMap;
use ordered_float::OrderedFloat;
use tracing::info;

use crate::options_chain::StrikeAndSide;

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
                    call.delta = crate::greeks::calculate_delta(call.mid, spx_price, strike, t, r, true);
                    call.theta = call.mid; // Schwab mid-price convention for 0DTE Theta
                }
            }

            if let Some(ref mut put) = quote.put {
                if put.mid > 0.0 {
                    put.delta = crate::greeks::calculate_delta(put.mid, spx_price, strike, t, r, false);
                    put.theta = put.mid; // Schwab mid-price convention for 0DTE Theta
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
