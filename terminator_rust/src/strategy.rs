use std::time::Duration;
use std::sync::Arc;
use std::collections::HashMap;
use ordered_float::OrderedFloat;
use tokio::sync::Mutex;
use anyhow::Result;
use chrono::{TimeZone, NaiveTime};
use chrono_tz::America::Chicago;
use chrono_tz::Tz;
use serde::{Deserialize, Serialize};
use serde_json::json;
use tracing::{error, info, warn};

use crate::grid::{OptionsGrid, OptionLegQuote};
use crate::execution::ExecutionClient;
use crate::parser::parse_occ_symbol;

#[derive(Clone, Debug)]
pub struct LegOverride { pub idx: usize, pub price_ea: f64 }
#[derive(Clone, Debug)]
pub struct PendingTrade { pub strat_id: String, pub trade: Trade, pub ui_legs: Vec<OptionLeg>, pub to_cancel: Vec<String> }
#[derive(Clone, Debug)]
pub struct HistoryPoint { pub ts: String, pub spx: f64, pub live_pnl: f64, pub sim_pnl: f64 }

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum StrategyState {
    Idle,
    EnteringSpread,
    Working,
    Exiting,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OptionLeg {
    pub symbol: String,
    pub strike: f64,
    pub side: String, // "CALL" or "PUT"
    pub quantity: i32, // Positive = long, Negative = short
    pub delta: f64,
    pub theta: f64,
    pub price: f64,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub instruction: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Trade {
    pub timestamp: String,
    pub legs: Vec<OptionLeg>,
    pub credit: f64,
    pub commission: f64,
    pub purpose: String, // "IRON_CONDOR", "EXIT", etc.
    pub strategy_id: String,
}

pub struct SubStrategy {
    pub sid: String,
    pub trade_start_time: NaiveTime,
    pub has_traded_today: bool,
    pub state: StrategyState,
    pub unit_size: i32,
    pub init_s_delta: f64,
    pub init_l_delta: f64,
    pub portfolio: Arc<Mutex<crate::portfolio::Portfolio>>,
    pub previous_portfolio: Option<crate::portfolio::Portfolio>,
    pub last_update_ts: Option<chrono::DateTime<chrono_tz::Tz>>,
    pub snapshot_trade_count: Option<usize>,
    pub cancelled_at: Option<std::time::Instant>,
}

impl SubStrategy {
    pub fn new(sid: String, trade_start_time: NaiveTime, init_s_delta: f64, init_l_delta: f64, unit_size: i32) -> Self {
        Self {
            sid,
            trade_start_time,
            has_traded_today: false,
            state: StrategyState::Idle,
            unit_size,
            init_s_delta,
            init_l_delta,
            portfolio: Arc::new(Mutex::new(crate::portfolio::Portfolio::new())),
            previous_portfolio: None,
            last_update_ts: None,
            snapshot_trade_count: None,
            cancelled_at: None,
        }
    }

    pub async fn check_and_finalize_fill(&mut self) -> bool {
        let prev = match &self.previous_portfolio {
            Some(p) => p,
            None => return true,
        };

        if let Some(snap_count) = self.snapshot_trade_count {
            if prev.trades.len() <= snap_count {
                return false;
            }
        }
        
        let fully_filled = {
            let port = self.portfolio.lock().await;
            let mut fully_filled = true;
            for pos in &port.positions {
                let prev_qty = prev.position_qty_for(&pos.symbol);
                if pos.quantity != prev_qty {
                    fully_filled = false;
                    break;
                }
            }
            if fully_filled {
                for pos in &prev.positions {
                    let port_qty = port.position_qty_for(&pos.symbol);
                    if pos.quantity != port_qty {
                        fully_filled = false;
                        break;
                    }
                }
            }
            fully_filled
        };
        
        if fully_filled {
            let prev_port = self.previous_portfolio.take().unwrap();
            {
                let mut port = self.portfolio.lock().await;
                port.positions = prev_port.positions;
                port.cash = prev_port.cash;
                port.trades = prev_port.trades;
            }
            self.last_update_ts = None;
            self.snapshot_trade_count = None;
            self.cancelled_at = None;
            
            let has_positions = !self.portfolio.lock().await.positions.is_empty();
            if has_positions {
                self.state = StrategyState::Working;
                self.has_traded_today = true;
            } else {
                self.state = StrategyState::Idle;
            }
            true
        } else {
            false
        }
    }

    pub async fn revert_optimistic_update(&mut self) {
        if let Some(prev) = self.previous_portfolio.take() {
            *self.portfolio.lock().await = prev;
            self.last_update_ts = None;
            self.snapshot_trade_count = None;
            self.cancelled_at = None;
            let has_positions = !self.portfolio.lock().await.positions.is_empty();
            if has_positions {
                self.state = StrategyState::Working;
            } else {
                self.state = StrategyState::Idle;
            }
        }
    }

    pub async fn finalize_partial_fill(&mut self) {
        // Identical to revert_optimistic_update: commits whatever fills landed in
        // previous_portfolio and transitions state accordingly.
        self.revert_optimistic_update().await;
    }
}

/// Calculate delta target with linear decay based on the time fraction of the trading day.
pub fn calculate_delta_decay(
    now: chrono::DateTime<Tz>,
    init_leg_delta: f64,
    start_time: NaiveTime,
    end_time: NaiveTime,
) -> f64 {
    let today = now.date_naive();
    let start_dt = today.and_time(start_time);
    let end_dt = today.and_time(end_time);

    let total_secs = (end_dt - start_dt).num_seconds() as f64;
    let elapsed_secs = (now.naive_local() - start_dt).num_seconds() as f64;

    let time_fraction = (elapsed_secs / total_secs).clamp(0.0, 1.0);

    // Linear decay
    let current_delta = init_leg_delta * (1.0 - time_fraction);
    current_delta.abs()
}

/// Find the option contract whose delta is closest to the target_delta.
/// If short_strike and max_diff are supplied, verifies strike limits for wings.
pub fn find_closest_option(
    grid: &OptionsGrid,
    target_delta: f64,
    is_call: bool,
    max_diff: Option<f64>,
    short_strike: Option<f64>,
) -> Option<OptionLegQuote> {
    let mut best_leg: Option<OptionLegQuote> = None;
    let mut min_diff = f64::MAX;
    let mut best_leg_fallback: Option<OptionLegQuote> = None;
    let mut min_diff_fallback = f64::MAX;
    
    let abs_target = target_delta.abs();

    for entry in grid.quotes.iter() {
        let strike = entry.key().0;
        let quote = entry.value();

        // Stale quote guard: skip strikes that haven't received a WS tick in 5s.
        // 500ms was too aggressive — WS ticks arrive at ~1Hz so half the time a
        // strike would be falsely excluded.
        if quote.last_updated.elapsed() > Duration::from_millis(5000) {
            continue;
        }

        if let Some(short) = short_strike {
            // Strict directional guard: long leg must be strictly further OTM than short leg.
            // Prevents zero-width spreads where long and short land on the same strike.
            if is_call && strike <= short {
                continue;
            }
            if !is_call && strike >= short {
                continue;
            }
            if let Some(diff) = max_diff {
                if is_call && (strike - short) > diff {
                    continue;
                }
                if !is_call && (short - strike) > diff {
                    continue;
                }
            }
        }

        let leg_quote_opt = if is_call { &quote.call } else { &quote.put };
        if let Some(leg) = leg_quote_opt {
            if leg.mid <= 0.0 || leg.last_update.elapsed() > Duration::from_millis(5000) {
                continue;
            }
            let abs_delta = leg.delta.abs();
            let diff = (abs_delta - abs_target).abs();
            
            if diff < min_diff_fallback {
                min_diff_fallback = diff;
                best_leg_fallback = Some(leg.clone());
            }

            if abs_delta >= abs_target {
                if diff < min_diff {
                    min_diff = diff;
                    best_leg = Some(leg.clone());
                }
            }
        }
    }

    best_leg.or(best_leg_fallback)
}

/// Core Iron Condor check entry logic
pub fn check_entry(
    grid: &OptionsGrid,
    s: &SubStrategy,
    now: chrono::DateTime<Tz>,
    max_diff: f64,
    commission_per_contract: f64,
) -> Option<Trade> {
    let start_time = NaiveTime::from_hms_opt(8, 30, 0)?;
    let end_time = NaiveTime::from_hms_opt(15, 0, 0)?;

    let target_sc_delta = calculate_delta_decay(now, s.init_s_delta, start_time, end_time);
    let target_sp_delta = calculate_delta_decay(now, s.init_s_delta, start_time, end_time);
    let target_lc_delta = calculate_delta_decay(now, s.init_l_delta, start_time, end_time);
    let target_lp_delta = calculate_delta_decay(now, s.init_l_delta, start_time, end_time);

    let sc = find_closest_option(grid, target_sc_delta, true, None, None)?;
    let sp = find_closest_option(grid, target_sp_delta, false, None, None)?;

    let parsed_sc = parse_occ_symbol(&sc.symbol)?.strike;
    let parsed_sp = parse_occ_symbol(&sp.symbol)?.strike;

    let lc = find_closest_option(grid, target_lc_delta, true, Some(max_diff), Some(parsed_sc))?;
    let lp = find_closest_option(grid, target_lp_delta, false, Some(max_diff), Some(parsed_sp))?;

    let parsed_lc = parse_occ_symbol(&lc.symbol)?.strike;
    let parsed_lp = parse_occ_symbol(&lp.symbol)?.strike;

    // Spread Verification checks (e.g. call and put vertical spread integrity)
    if (parsed_lc - parsed_sc).abs() > max_diff || (parsed_sp - parsed_lp).abs() > max_diff {
        warn!("Iron Condor spread verification failed: wing strike differences exceed max limit ({})", max_diff);
        return None;
    }

    let legs = vec![
        OptionLeg {
            symbol: sc.symbol.clone(),
            strike: parsed_sc,
            side: "CALL".to_string(),
            quantity: -s.unit_size,
            delta: sc.delta,
            theta: sc.theta,
            price: sc.mid,
            instruction: None,
        },
        OptionLeg {
            symbol: lc.symbol.clone(),
            strike: parsed_lc,
            side: "CALL".to_string(),
            quantity: s.unit_size,
            delta: lc.delta,
            theta: lc.theta,
            price: lc.mid,
            instruction: None,
        },
        OptionLeg {
            symbol: sp.symbol.clone(),
            strike: parsed_sp,
            side: "PUT".to_string(),
            quantity: -s.unit_size,
            delta: sp.delta,
            theta: sp.theta,
            price: sp.mid,
            instruction: None,
        },
        OptionLeg {
            symbol: lp.symbol.clone(),
            strike: parsed_lp,
            side: "PUT".to_string(),
            quantity: s.unit_size,
            delta: lp.delta,
            theta: lp.theta,
            price: lp.mid,
            instruction: None,
        },
    ];

    let credit = (sc.mid - lc.mid + sp.mid - lp.mid) * s.unit_size as f64 * 100.0;
    let commission = commission_per_contract * legs.len() as f64 * s.unit_size as f64;

    Some(Trade {
        timestamp: now.to_rfc3339(),
        legs,
        credit,
        commission,
        purpose: "IRON_CONDOR".to_string(),
        strategy_id: s.sid.clone(),
    })
}

pub struct CondorDeltas {
    pub abs_short_call: f64,
    pub abs_long_call:  f64,
    pub abs_short_put:  f64,
    pub abs_long_put:   f64,
    pub short_call_strike: Option<f64>,
    pub long_call_strike:  Option<f64>,
    pub short_put_strike:  Option<f64>,
    pub long_put_strike:   Option<f64>,
}

pub fn get_condor_deltas(portfolio: &crate::portfolio::Portfolio) -> CondorDeltas {
    let mut d = CondorDeltas { abs_short_call: 0.0, abs_long_call: 0.0,
                               abs_short_put: 0.0,  abs_long_put: 0.0,
                               short_call_strike: None, long_call_strike: None,
                               short_put_strike: None,  long_put_strike: None };
    for p in &portfolio.positions {
        let signed = p.delta * p.quantity as f64;
        match (p.side.as_str(), p.quantity < 0) {
            ("CALL", true)  => { d.abs_short_call = signed.abs(); d.short_call_strike = Some(p.strike); }
            ("CALL", false) => { d.abs_long_call  = signed.abs(); d.long_call_strike  = Some(p.strike); }
            ("PUT",  true)  => { d.abs_short_put  = signed.abs(); d.short_put_strike  = Some(p.strike); }
            ("PUT",  false) => { d.abs_long_put   = signed.abs(); d.long_put_strike   = Some(p.strike); }
            _ => {}
        }
    }
    d
}

fn create_new_spread_trade(
    grid: &OptionsGrid,
    s: &SubStrategy,
    t_short: f64,
    t_long: f64,
    is_call: bool,
    now: chrono::DateTime<Tz>,
    config: &crate::config::AppConfig,
) -> Option<Trade> {
    let opt_s = find_closest_option(grid, t_short, is_call, None, None)?;
    let parsed_s = parse_occ_symbol(&opt_s.symbol)?.strike;

    let opt_l = find_closest_option(grid, t_long, is_call, Some(config.max_spread_diff), Some(parsed_s))?;
    let parsed_l = parse_occ_symbol(&opt_l.symbol)?.strike;

    let side_str = if is_call { "CALL" } else { "PUT" };

    let legs = vec![
        OptionLeg {
            symbol: opt_s.symbol.clone(),
            strike: parsed_s,
            side: side_str.to_string(),
            quantity: -s.unit_size,
            delta: opt_s.delta,
            theta: opt_s.theta,
            price: opt_s.mid,
            instruction: None,
        },
        OptionLeg {
            symbol: opt_l.symbol.clone(),
            strike: parsed_l,
            side: side_str.to_string(),
            quantity: s.unit_size,
            delta: opt_l.delta,
            theta: opt_l.theta,
            price: opt_l.mid,
            instruction: None,
        },
    ];

    let credit = legs.iter().map(|l| -(l.quantity as f64) * l.price).sum::<f64>() * 100.0;
    let commission = 0.0; // Python sets it to 0 for REBALANCE_NEW

    Some(Trade {
        timestamp: now.to_rfc3339(),
        legs,
        credit,
        commission,
        purpose: "REBALANCE_NEW".to_string(),
        strategy_id: s.sid.clone(),
    })
}

fn create_rebalance_short(
    grid: &OptionsGrid,
    s: &SubStrategy,
    portfolio: &crate::portfolio::Portfolio,
    t_short: f64,
    t_long: f64,
    is_call: bool,
    now: chrono::DateTime<Tz>,
    config: &crate::config::AppConfig,
) -> Option<Trade> {
    let side_str = if is_call { "CALL" } else { "PUT" };
    let old_short = portfolio.positions.iter().find(|p| p.side == side_str && p.quantity < 0)?;

    let new_short = find_closest_option(grid, t_short, is_call, None, None)?;
    let parsed_new_short = parse_occ_symbol(&new_short.symbol)?.strike;

    let mut legs = vec![
        OptionLeg {
            symbol: old_short.symbol.clone(),
            strike: old_short.strike,
            side: old_short.side.clone(),
            quantity: -old_short.quantity,
            delta: old_short.delta,
            theta: old_short.theta,
            price: old_short.price,
            instruction: None,
        },
        OptionLeg {
            symbol: new_short.symbol.clone(),
            strike: parsed_new_short,
            side: side_str.to_string(),
            quantity: old_short.quantity,
            delta: new_short.delta,
            theta: new_short.theta,
            price: new_short.mid,
            instruction: None,
        },
    ];

    let old_long = portfolio.positions.iter().find(|p| p.side == side_str && p.quantity > 0);

    if let Some(ol) = old_long {
        let width = if is_call { ol.strike - parsed_new_short } else { parsed_new_short - ol.strike };
        if width > config.max_spread_diff {
            let new_long = find_closest_option(grid, t_long, is_call, Some(config.max_spread_diff), Some(parsed_new_short))?;
            let parsed_new_long = parse_occ_symbol(&new_long.symbol)?.strike;

            legs.push(OptionLeg {
                symbol: ol.symbol.clone(),
                strike: ol.strike,
                side: ol.side.clone(),
                quantity: -ol.quantity,
                delta: ol.delta,
                theta: ol.theta,
                price: ol.price,
                instruction: None,
            });
            legs.push(OptionLeg {
                symbol: new_long.symbol.clone(),
                strike: parsed_new_long,
                side: side_str.to_string(),
                quantity: ol.quantity,
                delta: new_long.delta,
                theta: new_long.theta,
                price: new_long.mid,
                instruction: None,
            });
        }
    }

    let credit = legs.iter().map(|l| -(l.quantity as f64) * l.price).sum::<f64>() * 100.0;
    let base_unit = old_short.quantity.abs() as f64;
    let commission = config.commission_per_contract * legs.len() as f64 * base_unit;

    if credit.abs() <= config.min_credit * 100.0 * base_unit {
        return None;
    }

    Some(Trade {
        timestamp: now.to_rfc3339(),
        legs,
        credit,
        commission,
        purpose: "REBALANCE_SHORT".to_string(),
        strategy_id: s.sid.clone(),
    })
}

fn create_rebalance_long(
    grid: &OptionsGrid,
    s: &SubStrategy,
    portfolio: &crate::portfolio::Portfolio,
    t_long: f64,
    is_call: bool,
    now: chrono::DateTime<Tz>,
    config: &crate::config::AppConfig,
    short_call_strike: Option<f64>,
    short_put_strike: Option<f64>,
) -> Option<Trade> {
    let side_str = if is_call { "CALL" } else { "PUT" };
    let old_long = portfolio.positions.iter().find(|p| p.side == side_str && p.quantity > 0)?;
    let short_strike = if is_call { short_call_strike } else { short_put_strike };

    let new_long = find_closest_option(grid, t_long, is_call, Some(config.max_spread_diff), short_strike)?;
    let parsed_new_long = parse_occ_symbol(&new_long.symbol)?.strike;

    let legs = vec![
        OptionLeg {
            symbol: old_long.symbol.clone(),
            strike: old_long.strike,
            side: old_long.side.clone(),
            quantity: -old_long.quantity,
            delta: old_long.delta,
            theta: old_long.theta,
            price: old_long.price,
            instruction: None,
        },
        OptionLeg {
            symbol: new_long.symbol.clone(),
            strike: parsed_new_long,
            side: side_str.to_string(),
            quantity: old_long.quantity,
            delta: new_long.delta,
            theta: new_long.theta,
            price: new_long.mid,
            instruction: None,
        },
    ];

    let credit = legs.iter().map(|l| -(l.quantity as f64) * l.price).sum::<f64>() * 100.0;
    let base_unit = old_long.quantity.abs() as f64;
    let commission = config.commission_per_contract * legs.len() as f64 * base_unit;

    Some(Trade {
        timestamp: now.to_rfc3339(),
        legs,
        credit,
        commission,
        purpose: "REBALANCE_LONG".to_string(),
        strategy_id: s.sid.clone(),
    })
}

pub fn check_rebalance(
    grid: &OptionsGrid,
    s: &SubStrategy,
    portfolio: &crate::portfolio::Portfolio,
    now: chrono::DateTime<Tz>,
    start_time: NaiveTime,
    end_time: NaiveTime,
    config: &crate::config::AppConfig,
) -> Vec<Trade> {
    let mut trades = Vec::new();
    let t_short = calculate_delta_decay(now, s.init_s_delta, start_time, end_time);
    let t_long  = calculate_delta_decay(now, s.init_l_delta, start_time, end_time);
    let d = get_condor_deltas(portfolio);

    for side in &["CALL", "PUT"] {
        let is_call = *side == "CALL";
        let abs_short_delta = if is_call { d.abs_short_call } else { d.abs_short_put };
        let abs_long_delta  = if is_call { d.abs_long_call } else { d.abs_long_put };

        let sn_needs = (abs_short_delta - t_short).abs() > config.rebalance_threshold;
        let mut ln_needs = (abs_long_delta - t_long).abs() > config.long_leg_rebalance_delta_threshold;

        if !ln_needs {
            let (short_strike, long_strike) = if is_call {
                (d.short_call_strike, d.long_call_strike)
            } else {
                (d.short_put_strike, d.long_put_strike)
            };
            if let (Some(ss), Some(ls)) = (short_strike, long_strike) {
                let width = if is_call { ls - ss } else { ss - ls };
                if width > config.max_spread_diff {
                    ln_needs = true;
                }
            }
        }

        let on_side: Vec<&crate::portfolio::PositionLeg> = portfolio.positions.iter().filter(|p| p.side == *side).collect();

        if on_side.is_empty() && (sn_needs || ln_needs) {
            if let Some(t) = create_new_spread_trade(grid, s, t_short, t_long, is_call, now, config) {
                trades.push(t);
            }
        } else if sn_needs {
            if let Some(t) = create_rebalance_short(grid, s, portfolio, t_short, t_long, is_call, now, config) {
                trades.push(t);
            }
        } else if ln_needs {
            if let Some(t) = create_rebalance_long(grid, s, portfolio, t_long, is_call, now, config, d.short_call_strike, d.short_put_strike) {
                trades.push(t);
            }
        }
    }

    trades
}

pub fn check_exit(
    grid: &OptionsGrid,
    portfolio: &crate::portfolio::Portfolio,
    now: chrono::DateTime<Tz>,
    strategy_id: &str,
    commission_per_contract: f64,
    _spx_price: f64,
) -> Option<Trade> {
    if portfolio.positions.is_empty() {
        return None;
    }

    let mut legs = Vec::new();
    for p in &portfolio.positions {
        let mut exit_price = 0.05;
        if let Some(quote) = grid.quotes.get(&ordered_float::OrderedFloat(p.strike)) {
            let lq = if p.side == "CALL" { &quote.call } else { &quote.put };
            if let Some(leg_quote) = lq {
                exit_price = leg_quote.mid.max(0.05);
            }
        }

        legs.push(OptionLeg {
            symbol: p.symbol.clone(),
            strike: p.strike,
            side: p.side.clone(),
            quantity: -p.quantity, // Reverse quantity to close
            delta: p.delta,
            theta: p.theta,
            price: exit_price,
            instruction: None,
        });
    }

    let credit = legs.iter().map(|l| -(l.quantity as f64) * l.price).sum::<f64>() * 100.0;
    let total_contracts: i32 = portfolio.positions.iter().map(|p| p.quantity.abs()).sum();
    let commission = commission_per_contract * total_contracts as f64;

    Some(Trade {
        timestamp: now.to_rfc3339(),
        legs,
        credit,
        commission,
        purpose: "EXIT".to_string(),
        strategy_id: strategy_id.to_string(),
    })
}

pub fn gcd(a: i32, b: i32) -> i32 {
    if b == 0 { a } else { gcd(b, a % b) }
}

pub fn unroll_legs(legs: &[OptionLeg]) -> Vec<OptionLeg> {
    let mut sorted_input = legs.to_vec();
    sorted_input.sort_by(|a, b| {
        let side_a = if a.side == "PUT" { 0 } else { 1 };
        let side_b = if b.side == "PUT" { 0 } else { 1 };
        side_a.cmp(&side_b).then_with(|| a.strike.partial_cmp(&b.strike).unwrap_or(std::cmp::Ordering::Equal))
    });

    let mut unrolled = Vec::new();
    for leg in sorted_input {
        let qty = leg.quantity.abs();
        let direction = if leg.quantity > 0 { 1 } else { -1 };
        for _ in 0..qty {
            let mut unit_leg = leg.clone();
            unit_leg.quantity = direction;
            unrolled.push(unit_leg);
        }
    }
    unrolled
}

pub fn roll_legs(legs: &[OptionLeg]) -> Vec<OptionLeg> {
    let mut agg: std::collections::HashMap<String, OptionLeg> = std::collections::HashMap::new();
    for l in legs {
        if let Some(existing) = agg.get_mut(&l.symbol) {
            existing.quantity += l.quantity;
        } else {
            agg.insert(l.symbol.clone(), l.clone());
        }
    }
    let mut rolled: Vec<OptionLeg> = agg.into_values().filter(|l| l.quantity != 0).collect();
    rolled.sort_by(|a, b| a.symbol.cmp(&b.symbol));
    rolled
}

pub fn classify_order_type(legs: &[OptionLeg]) -> (&'static str, Option<bool>) {
    let n = legs.len();
    if n == 0 {
        return ("unknown", None);
    }
    if n == 1 {
        let is_credit = legs[0].quantity < 0;
        return ("single", Some(is_credit));
    }
    if n == 2 {
        let l1 = &legs[0];
        let l2 = &legs[1];
        if l1.side == l2.side && l1.quantity * l2.quantity < 0 && l1.quantity.abs() == l2.quantity.abs() {
            let (long_leg, short_leg) = if l1.quantity > 0 { (l1, l2) } else { (l2, l1) };
            let is_credit = if l1.side == "CALL" {
                short_leg.strike < long_leg.strike
            } else {
                short_leg.strike > long_leg.strike
            };
            return ("vertical", Some(is_credit));
        }
    }

    let mut sorted_legs = legs.to_vec();
    sorted_legs.sort_by(|a, b| a.strike.partial_cmp(&b.strike).unwrap_or(std::cmp::Ordering::Equal));
    let quantities: Vec<i32> = sorted_legs.iter().map(|l| l.quantity).collect();

    let mut unit = quantities[0].abs();
    for q in quantities.iter().skip(1) {
        unit = gcd(unit, q.abs());
    }

    let ratios: Vec<i32> = if unit > 0 {
        quantities.iter().map(|q| q / unit).collect()
    } else {
        Vec::new()
    };

    if n == 3 {
        let all_same_side = sorted_legs.iter().all(|l| l.side == sorted_legs[0].side);
        if all_same_side {
            if ratios == vec![1, -2, 1] {
                return ("butterfly", Some(false));
            }
            if ratios == vec![-1, 2, -1] {
                return ("butterfly", Some(true));
            }
        }
    }

    if n == 4 {
        let all_same_side = sorted_legs.iter().all(|l| l.side == sorted_legs[0].side);
        if all_same_side {
            if ratios == vec![1, -1, -1, 1] {
                return ("condor", Some(false));
            }
            if ratios == vec![-1, 1, 1, -1] {
                return ("condor", Some(true));
            }
        }

        let p_legs: Vec<&OptionLeg> = sorted_legs.iter().filter(|l| l.side == "PUT").collect();
        let c_legs: Vec<&OptionLeg> = sorted_legs.iter().filter(|l| l.side == "CALL").collect();
        if p_legs.len() == 2 && c_legs.len() == 2 {
            let lp = p_legs.iter().find(|l| l.quantity > 0);
            let sp = p_legs.iter().find(|l| l.quantity < 0);
            let lc = c_legs.iter().find(|l| l.quantity > 0);
            let sc = c_legs.iter().find(|l| l.quantity < 0);

            if let (Some(lp), Some(sp), Some(lc), Some(sc)) = (lp, sp, lc, sc) {
                if lp.quantity.abs() == sp.quantity.abs()
                    && sp.quantity.abs() == lc.quantity.abs()
                    && lc.quantity.abs() == sc.quantity.abs()
                {
                    let is_credit = sp.strike > lp.strike && sc.strike < lc.strike;
                    let t_str = if sp.strike == sc.strike || lp.strike == lc.strike {
                        "iron_fly"
                    } else {
                        "iron_condor"
                    };
                    return (t_str, Some(is_credit));
                }
            }
        }
    }

    ("unknown", None)
}

fn extract_chunk<F>(remaining: &mut Vec<OptionLeg>, num_legs: usize, constraint_func: F) -> Option<Vec<OptionLeg>>
where
    F: Fn(&[OptionLeg]) -> bool,
{
    let len = remaining.len();
    if len < num_legs {
        return None;
    }

    if num_legs == 4 {
        for i in 0..len {
            for j in (i + 1)..len {
                for k in (j + 1)..len {
                    for l in (k + 1)..len {
                        let combo = vec![
                            remaining[i].clone(),
                            remaining[j].clone(),
                            remaining[k].clone(),
                            remaining[l].clone(),
                        ];
                        let mut unique = true;
                        for x in 0..4 {
                            for y in (x + 1)..4 {
                                if combo[x].side == combo[y].side && (combo[x].strike - combo[y].strike).abs() < 1e-5 {
                                    unique = false;
                                    break;
                                }
                            }
                            if !unique { break; }
                        }
                        if !unique { continue; }

                        if constraint_func(&combo) {
                            remaining.remove(l);
                            remaining.remove(k);
                            remaining.remove(j);
                            remaining.remove(i);
                            return Some(combo);
                        }
                    }
                }
            }
        }
    } else if num_legs == 2 {
        for i in 0..len {
            for j in (i + 1)..len {
                let combo = vec![
                    remaining[i].clone(),
                    remaining[j].clone(),
                ];
                if combo[0].side == combo[1].side && (combo[0].strike - combo[1].strike).abs() < 1e-5 {
                    continue;
                }

                if constraint_func(&combo) {
                    remaining.remove(j);
                    remaining.remove(i);
                    return Some(combo);
                }
            }
        }
    }
    None
}

pub fn get_smart_chunks(legs: &[OptionLeg]) -> Vec<Vec<OptionLeg>> {
    let mut remaining = unroll_legs(legs);
    if remaining.is_empty() {
        return Vec::new();
    }

    let mut found_combos = Vec::new();

    // Priority 1: Iron Condors (4 legs: 1xLC, 1xSC, 1xLP, 1xSP).
    // Both the call spread and the put spread must be the same type — either both credit
    // (standard short IC: sell nearer strikes) or both debit (long IC: buy nearer strikes).
    // A combo where one side is credit and the other is debit is NOT a valid iron condor
    // and is left for lower-priority grouping.
    while remaining.len() >= 4 {
        let ic = extract_chunk(&mut remaining, 4, |c| {
            if c.iter().filter(|l| l.side == "CALL" && l.quantity > 0).count() != 1 { return false; }
            if c.iter().filter(|l| l.side == "CALL" && l.quantity < 0).count() != 1 { return false; }
            if c.iter().filter(|l| l.side == "PUT"  && l.quantity > 0).count() != 1 { return false; }
            if c.iter().filter(|l| l.side == "PUT"  && l.quantity < 0).count() != 1 { return false; }
            let lc = c.iter().find(|l| l.side == "CALL" && l.quantity > 0).unwrap();
            let sc = c.iter().find(|l| l.side == "CALL" && l.quantity < 0).unwrap();
            let lp = c.iter().find(|l| l.side == "PUT"  && l.quantity > 0).unwrap();
            let sp = c.iter().find(|l| l.side == "PUT"  && l.quantity < 0).unwrap();
            let call_credit = sc.strike < lc.strike; // sell lower call = credit call spread
            let put_credit  = sp.strike > lp.strike; // sell higher put = credit put spread
            call_credit == put_credit                 // both sides must be same type
        });
        match ic {
            Some(combo) => found_combos.push(combo),
            None => break,
        }
    }

    // Priority 2: Side-Specific Condors / Rolls (4 legs: 2L, 2S on same side)
    while remaining.len() >= 4 {
        let roll = extract_chunk(&mut remaining, 4, |c| {
            c.iter().all(|l| l.side == c[0].side) &&
            c.iter().filter(|l| l.quantity > 0).count() == 2 &&
            c.iter().filter(|l| l.quantity < 0).count() == 2
        });
        match roll {
            Some(combo) => found_combos.push(combo),
            None => break,
        }
    }

    // Priority 3: Vertical Spreads (2 legs: 1L, 1S on same side)
    while remaining.len() >= 2 {
        let vs = extract_chunk(&mut remaining, 2, |c| {
            c.iter().all(|l| l.side == c[0].side) &&
            c.iter().filter(|l| l.quantity > 0).count() == 1 &&
            c.iter().filter(|l| l.quantity < 0).count() == 1
        });
        match vs {
            Some(combo) => found_combos.push(combo),
            None => break,
        }
    }

    // Residuals: Aggregate whatever is left
    let leftover_rolled = roll_legs(&remaining);

    // Group identical combo units (consolidation)
    let mut grouped: std::collections::HashMap<Vec<(String, Option<String>)>, Vec<Vec<OptionLeg>>> = std::collections::HashMap::new();
    for combo in found_combos {
        let mut sig: Vec<(String, Option<String>)> = combo.iter().map(|l| (l.symbol.clone(), l.instruction.clone())).collect();
        sig.sort();
        grouped.entry(sig).or_default().push(combo);
    }

    let mut final_chunks = Vec::new();
    let mut entries: Vec<(Vec<(String, Option<String>)>, Vec<Vec<OptionLeg>>)> = grouped.into_iter().collect();
    entries.sort_by(|a, b| a.0.cmp(&b.0));

    for (_, combos) in entries {
        let mut all_unit_legs = Vec::new();
        for combo in combos {
            all_unit_legs.extend(combo);
        }
        final_chunks.push(roll_legs(&all_unit_legs));
    }

    if !leftover_rolled.is_empty() {
        for chunk in leftover_rolled.chunks(4) {
            final_chunks.push(chunk.to_vec());
        }
    }

    final_chunks
}

#[derive(Debug, Clone)]
pub struct ExecutedChunk {
    pub order_id: Option<String>,
    pub quantity: f64,
    pub legs: Vec<serde_json::Value>,
}

#[derive(Debug, Clone)]
pub struct DeferredChunk {
    pub chunk: Vec<OptionLeg>,
    pub awaiting_order_id: String,
}

pub async fn execute_trade(
    client: &ExecutionClient,
    account_hash: &str,
    trade: &Trade,
    dry_run: bool,
    order_offset: f64,
    positions: &[crate::portfolio::PositionLeg],
    overrides: &[LegOverride],
) -> Result<(Vec<ExecutedChunk>, Vec<DeferredChunk>), (Vec<ExecutedChunk>, anyhow::Error)> {
    let mut order_ids = Vec::new();
    let mut deferred_chunks = Vec::new();

    let mut closing_remaining: std::collections::HashMap<String, i32> = std::collections::HashMap::new();
    for p in positions {
        closing_remaining.insert(p.symbol.clone(), p.quantity.abs());
    }

    // Group all legs together so they can be structured as valid multi-leg complex orders
    let chunks = get_smart_chunks(&trade.legs);

    // All symbols being closed — used to defer opening chunks that would conflict.
    let mut actively_closed_symbols = std::collections::HashSet::new();
    {
        let mut temp_remaining = closing_remaining.clone();
        for leg in &trade.legs {
            let remaining = temp_remaining.get(&leg.symbol).copied().unwrap_or(0);
            let broker_opposing = positions.iter().any(|p| {
                p.symbol == leg.symbol && (p.quantity as f64).signum() != (leg.quantity as f64).signum()
            });
            if broker_opposing && remaining > 0 {
                actively_closed_symbols.insert(leg.symbol.clone());
                let qty_deduct = leg.quantity.abs();
                let entry = temp_remaining.entry(leg.symbol.clone()).or_insert(0);
                *entry = entry.saturating_sub(qty_deduct);
            }
        }
    }

    // Classify chunks: a chunk is a close chunk (must be executed immediately and not deferred)
    // if it contains any leg that is closing an existing position at the broker.
    let chunk_classifications: Vec<bool> = chunks.iter().map(|chunk| {
        let mut temp_remaining = closing_remaining.clone();
        chunk.iter().any(|leg| {
            let remaining = temp_remaining.get(&leg.symbol).copied().unwrap_or(0);
            let broker_opposing = positions.iter().any(|p| {
                p.symbol == leg.symbol && (p.quantity as f64).signum() != (leg.quantity as f64).signum()
            });
            let is_close = broker_opposing && remaining > 0;
            if is_close {
                let qty_deduct = leg.quantity.abs();
                let entry = temp_remaining.entry(leg.symbol.clone()).or_insert(0);
                *entry = entry.saturating_sub(qty_deduct);
            }
            is_close
        })
    }).collect();

    let mut symbol_to_order_id: std::collections::HashMap<String, String> = std::collections::HashMap::new();
    let mut temp_closing_remaining = closing_remaining.clone();

    for (i, chunk) in chunks.iter().enumerate() {
        let is_deferred = !dry_run && !chunk_classifications[i] && chunk.iter().any(|leg| actively_closed_symbols.contains(&leg.symbol));
        if is_deferred {
            info!("Deferring chunk {} (flip opening) until sibling position conflicts are resolved", i);
            continue;
        }

        let total_chunk_credit = chunk.iter().map(|l| -(l.quantity as f64) * l.price).sum::<f64>() * 100.0;

        let mut num_units = chunk[0].quantity.abs();
        for leg in chunk.iter().skip(1) {
            num_units = gcd(num_units, leg.quantity.abs());
        }
        if num_units == 0 {
            num_units = 1;
        }

        let signed_mid = total_chunk_credit / (100.0 * num_units as f64);

        let target = if let Some(over) = overrides.iter().find(|o| o.idx == i) {
            over.price_ea
        } else {
            signed_mid + order_offset
        };

        let (struct_type, is_credit_structural) = classify_order_type(chunk);
        let lock_floor = struct_type != "unknown";

        let (is_final_credit, price) = if lock_floor {
            let is_credit = is_credit_structural.unwrap_or(true);
            let raw_price = if is_credit { target } else { -target };
            (is_credit, raw_price.max(0.0))
        } else {
            let is_credit = target >= 0.0;
            (is_credit, target.abs())
        };

        let price_ticked = (price / 0.05).round() * 0.05;
        let price_str = format!("{:.2}", price_ticked);
        let order_type_str = if is_final_credit { "NET_CREDIT" } else { "NET_DEBIT" };

        let complex_type = match (struct_type, chunk.len()) {
            ("iron_condor" | "iron_fly", _) => "IRON_CONDOR",
            ("vertical", _) => "VERTICAL",
            (_, 4) => "IRON_CONDOR",
            _ => "CUSTOM",
        };

        let mut legs_collection = Vec::new();
        for leg in chunk {
            let is_closing = {
                let remaining = closing_remaining.get(&leg.symbol).copied().unwrap_or(0);
                let broker_opposing = positions.iter().any(|p| {
                    p.symbol == leg.symbol && (p.quantity as f64).signum() != (leg.quantity as f64).signum()
                });
                broker_opposing && remaining > 0
            };

            if is_closing {
                let qty_deduct = leg.quantity.abs();
                let entry = closing_remaining.entry(leg.symbol.clone()).or_insert(0);
                *entry = entry.saturating_sub(qty_deduct);
            }

            let inst = if let Some(ref inst_str) = leg.instruction {
                inst_str.as_str()
            } else {
                match (leg.quantity > 0, is_closing) {
                    (true,  true)  => "BUY_TO_CLOSE",
                    (true,  false) => "BUY_TO_OPEN",
                    (false, true)  => "SELL_TO_CLOSE",
                    (false, false) => "SELL_TO_OPEN",
                }
            };

            legs_collection.push(json!({
                "instruction": inst,
                "quantity": leg.quantity.abs(),  // total contracts; Schwab uses leg qty for complex orders
                "instrument": {
                    "symbol": leg.symbol,
                    "assetType": "OPTION"
                }
            }));
        }

        if legs_collection.iter().any(|l| l["quantity"].as_i64().unwrap_or(0) == 0) {
            return Err((order_ids, anyhow::anyhow!("zero-quantity leg detected — order aborted")));
        }

        if dry_run {
            info!(
                "[DRY RUN] Bypassing REST endpoint. Chunk {} ({}) would trade (qty: {}, type: {}, price: {})",
                i, struct_type, num_units, order_type_str, price_str
            );
            order_ids.push(ExecutedChunk { order_id: None, quantity: num_units as f64, legs: legs_collection });
            continue;
        }

        let order_body = json!({
            "orderType": order_type_str,
            "session": "NORMAL",
            "duration": "DAY",
            "price": price_str,
            "orderStrategyType": "SINGLE",
            "complexOrderStrategyType": complex_type,
            "quantity": 1,  // Schwab ignores top-level quantity for complex orders; leg qty drives contract count
            "orderLegCollection": legs_collection
        });

        match client.place_order(account_hash, order_body).await {
            Ok(order_id) => {
                info!("Chunk {} placed successfully. Order ID: {:?}", i, order_id);
                if let Some(ref oid) = order_id {
                    for leg in chunk {
                        let remaining = temp_closing_remaining.get(&leg.symbol).copied().unwrap_or(0);
                        let broker_opposing = positions.iter().any(|p| {
                            p.symbol == leg.symbol && (p.quantity as f64).signum() != (leg.quantity as f64).signum()
                        });
                        if broker_opposing && remaining > 0 {
                            symbol_to_order_id.insert(leg.symbol.clone(), oid.clone());
                            let qty_deduct = leg.quantity.abs();
                            let entry = temp_closing_remaining.entry(leg.symbol.clone()).or_insert(0);
                            *entry = entry.saturating_sub(qty_deduct);
                        }
                    }
                }
                order_ids.push(ExecutedChunk { order_id, quantity: num_units as f64, legs: legs_collection });
            }
            Err(e) => {
                error!("Failed to place order for chunk {}: {:?}", i, e);
                return Err((order_ids, e));
            }
        }
    }

    // Construct DeferredChunks for deferred chunks
    for (i, chunk) in chunks.iter().enumerate() {
        let is_deferred = !dry_run && !chunk_classifications[i] && chunk.iter().any(|leg| actively_closed_symbols.contains(&leg.symbol));
        if is_deferred {
            let mut awaiting_order_id = String::new();
            for leg in chunk {
                if let Some(oid) = symbol_to_order_id.get(&leg.symbol) {
                    awaiting_order_id = oid.clone();
                    break;
                }
            }
            if !awaiting_order_id.is_empty() {
                deferred_chunks.push(DeferredChunk {
                    chunk: chunk.clone(),
                    awaiting_order_id,
                });
            } else {
                warn!("Deferred chunk {} could not find prerequisite order ID from sibling closing chunks. Dropping.", i);
            }
        }
    }

    Ok((order_ids, deferred_chunks))
}


// ============================================================================
// Strategy Supervisor & Execution State Machine
// ============================================================================

pub struct StrategySupervisor {
    pub config: crate::config::AppConfig,
    pub execution_client: Arc<ExecutionClient>,
    pub grid: Arc<OptionsGrid>,
    pub sub_strategies: Mutex<HashMap<String, SubStrategy>>,
    pub account_hash: Mutex<Option<String>>,
    pub live_portfolio: Arc<Mutex<crate::portfolio::Portfolio>>,
    /// Tracks only trades that were physically sent to the broker (dry_run=false).
    /// Used to populate the Live panel in the UI.
    pub broker_portfolio: Arc<Mutex<crate::portfolio::Portfolio>>,
    pub working_orders: tokio::sync::Mutex<Vec<serde_json::Value>>,
    pub pending_trade: tokio::sync::Mutex<Option<PendingTrade>>,
    pub server_name: String,
    pub status: tokio::sync::Mutex<String>,
    pub broker_connected: std::sync::atomic::AtomicBool,
    pub trading_enabled: std::sync::atomic::AtomicBool,
    pub heartbeat_failures: std::sync::atomic::AtomicUsize,
    pub timer_paused: std::sync::atomic::AtomicBool,
    pub session_history: tokio::sync::Mutex<std::collections::VecDeque<HistoryPoint>>,
    pub last_history_ct: tokio::sync::Mutex<Option<chrono::DateTime<chrono_tz::Tz>>>,
    pub execution_queue: Arc<tokio::sync::Mutex<Vec<DeferredChunk>>>,
    pub fast_sync_tx: tokio::sync::mpsc::Sender<bool>,
    pub fast_sync_rx: Mutex<Option<tokio::sync::mpsc::Receiver<bool>>>,
}

/// Split gap legs into close (reducing/exiting existing broker positions) and open (new positions).
/// For a position flip (sign crosses zero), the exit portion goes to `close` and the new
/// target size goes to `open`. This keeps `get_smart_chunks` from seeing duplicate-symbol legs.
///
/// Paired vertical spread detection: if a broker LONG position is being closed on a given
/// side (CALL or PUT), any broker SHORT positions on the *same side* are also promoted to
/// `close_adjustments` (buy them back to go flat), with their full sim target added to
/// `open_adjustments` for the subsequent wave.  This prevents Schwab position-conflict
/// rejections when closing a debit vertical spread while simultaneously wanting to reopen
/// new shorts on the same symbol.
pub fn separate_recon_adjustments(
    sim_map: &std::collections::HashMap<(OrderedFloat<f64>, String), i32>,
    live_map: &std::collections::HashMap<(OrderedFloat<f64>, String), i32>,
) -> (Vec<(f64, String, i32)>, Vec<(f64, String, i32)>) {
    let mut all_keys = std::collections::HashSet::new();
    for k in sim_map.keys() { all_keys.insert(k.clone()); }
    for k in live_map.keys() { all_keys.insert(k.clone()); }

    let mut close_adjustments: Vec<(f64, String, i32)> = Vec::new();
    let mut open_adjustments: Vec<(f64, String, i32)> = Vec::new();
    // Tracks which option sides (CALL/PUT) have a broker LONG position being closed.
    let mut closing_long_sides: std::collections::HashSet<String> = std::collections::HashSet::new();

    for (strike, side) in &all_keys {
        let key = (strike.clone(), side.clone());
        let sq = *sim_map.get(&key).unwrap_or(&0);
        let lq = *live_map.get(&key).unwrap_or(&0);
        let diff = sq - lq;
        if diff != 0 {
            if (lq < 0 && sq > 0) || (lq > 0 && sq < 0) {
                // Position flip: exit to zero first, then new target as a separate open leg
                close_adjustments.push((strike.into_inner(), side.clone(), -lq));
                open_adjustments.push((strike.into_inner(), side.clone(), sq));
                if lq > 0 {
                    closing_long_sides.insert(side.clone());
                }
            } else {
                let is_closing = lq != 0 && (lq as f64).signum() == -(diff as f64).signum();
                if is_closing {
                    close_adjustments.push((strike.into_inner(), side.clone(), diff));
                    if lq > 0 {
                        closing_long_sides.insert(side.clone());
                    }
                } else {
                    open_adjustments.push((strike.into_inner(), side.clone(), diff));
                }
            }
        }
    }

    // Paired vertical spread promotion: for each broker SHORT position that ended up in
    // open_adjustments on a side where a LONG is being closed, include it in the flatten
    // wave instead so the whole vertical is unwound before new shorts are opened.
    if !closing_long_sides.is_empty() {
        let to_promote: Vec<(f64, String)> = open_adjustments.iter()
            .filter(|(s, side, _)| {
                let key = (OrderedFloat(*s), side.clone());
                let lq = *live_map.get(&key).unwrap_or(&0);
                // Promote only if: broker is SHORT on a side where a LONG is being closed,
                // AND not already in close_adjustments (flips are already handled there).
                lq < 0
                    && closing_long_sides.contains(side)
                    && !close_adjustments.iter().any(|(cs, csd, _)| {
                        (*cs - s).abs() < 0.01 && csd == side
                    })
            })
            .map(|(s, side, _)| (*s, side.clone()))
            .collect();

        for (strike, side) in to_promote {
            let key = (OrderedFloat(strike), side.clone());
            let lq = *live_map.get(&key).unwrap_or(&0); // < 0 (short)
            let sq = *sim_map.get(&key).unwrap_or(&0);
            // Remove the diff-based entry added during the main loop
            open_adjustments.retain(|(s, sd, _)| !((*s - strike).abs() < 0.01 && sd == &side));
            // Buy back the short to go flat
            close_adjustments.push((strike, side.clone(), -lq));
            // Add the full sim target as a fresh open (from flat, not from diff)
            if sq != 0 {
                open_adjustments.push((strike, side, sq));
            }
        }
    }

    (close_adjustments, open_adjustments)
}

impl StrategySupervisor {
    pub fn new(
        config: crate::config::AppConfig,
        execution_client: Arc<ExecutionClient>,
        grid: Arc<OptionsGrid>,
    ) -> Self {
        let mut sub_strategies = HashMap::new();
        let mut t = config.portfolio_start_time;

        while t <= config.portfolio_end_time {
            let sid = format!("strat_{}", t.format("%H%M"));
            let init_s = config.initial_sum_delta / 2.0;
            let init_l = (init_s - config.init_wing_delta).max(0.025);
            let s = SubStrategy::new(sid.clone(), t, init_s, init_l, config.default_unit_size);
            sub_strategies.insert(sid, s);

            let (next_t, overflow_days) = t.overflowing_add_signed(chrono::Duration::minutes(config.portfolio_interval_minutes as i64));
            if overflow_days > 0 {
                break;
            }
            t = next_t;
        }

        let (fast_sync_tx, fast_sync_rx) = tokio::sync::mpsc::channel::<bool>(100);

        Self {
            config: config.clone(),
            execution_client,
            grid,
            sub_strategies: Mutex::new(sub_strategies),
            account_hash: Mutex::new(None),
            live_portfolio: Arc::new(Mutex::new(crate::portfolio::Portfolio::new())),
            broker_portfolio: Arc::new(Mutex::new(crate::portfolio::Portfolio::new())),
            working_orders: Mutex::new(Vec::new()),
            pending_trade: Mutex::new(None),
            server_name: config.server_name.clone(),
            status: Mutex::new("Initializing".to_string()),
            broker_connected: std::sync::atomic::AtomicBool::new(false),
            trading_enabled: std::sync::atomic::AtomicBool::new(false),
            heartbeat_failures: std::sync::atomic::AtomicUsize::new(0),
            timer_paused: std::sync::atomic::AtomicBool::new(false),
            session_history: Mutex::new(std::collections::VecDeque::new()),
            last_history_ct: Mutex::new(None),
            execution_queue: Arc::new(tokio::sync::Mutex::new(Vec::new())),
            fast_sync_tx,
            fast_sync_rx: Mutex::new(Some(fast_sync_rx)),
        }
    }

    pub fn create_sync_entry(live_trade: &Trade, now: chrono::DateTime<chrono_tz::Tz>, sid: &str) -> Trade {
        let mut sync_trade = live_trade.clone();
        sync_trade.timestamp = now.to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        sync_trade.purpose = "IRON_CONDOR".to_string();
        sync_trade.commission = 0.0;
        sync_trade.strategy_id = sid.to_string();
        // The leg prices and strikes were already mapped directly from Schwab fill data.
        // So the sync_trade perfectly reflects reality.
        sync_trade
    }

    pub async fn get_simulated_combined_portfolio(&self) -> crate::portfolio::Portfolio {
        let mut combined = crate::portfolio::Portfolio::new();
        let strats = self.sub_strategies.lock().await;
        for s in strats.values() {
            let port = s.portfolio.lock().await;
            combined.cash += port.cash;
            for t in &port.trades {
                combined.trades.push(t.clone());
            }
            for pos in &port.positions {
                if let Some(existing) = combined.positions.iter_mut().find(|p| p.symbol == pos.symbol) {
                    let old_qty = existing.quantity;
                    existing.quantity += pos.quantity;
                    if existing.quantity == 0 {
                        combined.positions.retain(|p| p.symbol != pos.symbol);
                    } else {
                        if (old_qty > 0 && pos.quantity > 0) || (old_qty < 0 && pos.quantity < 0) {
                            let total_qty = old_qty.abs() + pos.quantity.abs();
                            existing.entry_price = ((existing.entry_price * (old_qty.abs() as f64)) + (pos.entry_price * (pos.quantity.abs() as f64))) / (total_qty as f64);
                        }
                        existing.price = pos.price;
                        existing.delta = pos.delta;
                        existing.theta = pos.theta;
                        existing.bid = pos.bid;
                        existing.ask = pos.ask;
                    }
                } else {
                    combined.positions.push(pos.clone());
                }
            }
        }
        combined
    }

    /// Spawns a background task running the supervisor check loop every 5 seconds.
    pub async fn run_supervisor_loop(self: Arc<Self>) {
        info!("🤖 Strategy Supervisor task started. Checking status every 5 seconds.");

        // Resolve account hash at startup
        let resolved_hash = loop {
            match self.execution_client.get_account_hash().await {
                Ok(hash) => {
                    info!("🤖 Resolved account hash: {}", hash);
                    let mut h = self.account_hash.lock().await;
                    *h = Some(hash.clone());
                    self.broker_connected.store(true, std::sync::atomic::Ordering::Relaxed);
                    *self.status.lock().await = "Running".to_string();
                    break hash;
                }
                Err(e) => {
                    error!("Strategy Supervisor failed to resolve account hash: {:?}. Retrying in 10s...", e);
                    tokio::time::sleep(Duration::from_secs(10)).await;
                }
            }
        };

        // Startup Position Reconciliation Guard
        if !resolved_hash.is_empty() {
            match self.execution_client.get_live_positions(&resolved_hash).await {
                Ok(positions) => {
                    self.reconcile_startup_positions(&positions).await;
                }
                Err(e) => {
                    warn!("Could not verify open positions at startup: {:?}. Proceeding with fresh state.", e);
                }
            }
        }

        self.bootstrap_from_history(&resolved_hash).await;

        loop {
            if let Err(e) = self.tick().await {
                error!("Error in Strategy Supervisor tick: {:?}", e);
            }
            tokio::time::sleep(Duration::from_secs(5)).await;
        }
    }

    pub async fn bootstrap_from_history(&self, account_hash: &str) {
        if self.config.db_path.is_empty() {
            return;
        }
        let mode = self.config.bootstrap_mode.to_lowercase();
        if mode == "none" { return; }

        info!("Starting historical bootstrap replay (Mode: {})", mode);

        let now_utc = chrono::Utc::now();
        let tz: chrono_tz::Tz = "America/Chicago".parse().unwrap();
        let now_ct = now_utc.with_timezone(&tz);
        let start_time = now_ct.date_naive().and_hms_opt(8, 30, 0).unwrap().and_local_timezone(tz).unwrap();

        // Expand {date} placeholder in db_path to today's date (YYYYMMDD in CT)
        let date_str = now_ct.format("%Y%m%d").to_string();
        let resolved_db_path = self.config.db_path.replace("{date}", &date_str);

        if now_ct < start_time {
            info!("Skipping bootstrap (before 8:30 AM)");
            return;
        }

        let mut live_trades = Vec::new();
        match self.execution_client.get_today_filled_orders(account_hash, self.config.commission_per_contract).await {
            Ok(trades) => {
                live_trades = trades;
                info!("Fetched {} live filled trades for today", live_trades.len());
            }
            Err(e) => {
                warn!("Failed to fetch live trades for today: {:?}", e);
            }
        }

        match self.execution_client.get_working_orders(account_hash).await {
            Ok(orders) => {
                let mut wo = self.working_orders.lock().await;
                *wo = orders;
                info!("Fetched {} working orders", wo.len());
            }
            Err(e) => {
                warn!("Failed to fetch working orders: {:?}", e);
            }
        }

        // Reset all strategies (clearing out any false state placed by startup reconciliation)
        {
            let mut strats = self.sub_strategies.lock().await;
            for s in strats.values_mut() {
                *s.portfolio.lock().await = crate::portfolio::Portfolio::new();
                s.has_traded_today = false;
                s.state = StrategyState::Idle;
            }
            *self.live_portfolio.lock().await = crate::portfolio::Portfolio::new();
            *self.session_history.lock().await = std::collections::VecDeque::new();
            
            // Populate broker_portfolio trades with all filled live trades we just fetched
            let mut broker_port = self.broker_portfolio.lock().await;
            for trade in &live_trades {
                broker_port.add_trade(trade, None);
            }
        }

        let mut historical_broker_port = crate::portfolio::Portfolio::new();

        // Mutual Clarity Matching (Soft Mode)
        let mut assigned_live_entry = std::collections::HashMap::new();
        if mode == "soft" {
            let mut strat_matches = std::collections::HashMap::new();
            let mut trade_strats = std::collections::HashMap::new();

            for lt in &live_trades {
                if let Ok(lt_ts) = chrono::DateTime::parse_from_rfc3339(&lt.timestamp) {
                    let lt_ct = lt_ts.with_timezone(&tz);
                    for (sid, s) in self.sub_strategies.lock().await.iter() {
                        let win_start = start_time.date_naive().and_time(s.trade_start_time).and_local_timezone(tz).unwrap();
                        let win_end = win_start + chrono::Duration::hours(1);
                        if lt_ct >= win_start && lt_ct <= win_end {
                            strat_matches.entry(sid.clone()).or_insert_with(Vec::new).push(lt.clone());
                            trade_strats.entry(lt.strategy_id.clone()).or_insert_with(Vec::new).push(sid.clone());
                        }
                    }
                }
            }

            for (sid, matches) in strat_matches {
                if matches.len() == 1 {
                    let lt = &matches[0];
                    if let Some(strats) = trade_strats.get(&lt.strategy_id) {
                        if strats.len() == 1 {
                            info!("Soft Bootstrap: Assigned live trade {} to {}", lt.strategy_id, sid);
                            assigned_live_entry.insert(sid.clone(), lt.clone());
                        } else {
                            info!("Soft Bootstrap: Match for {} is ambiguous (trade matches multiple strategies). Sim fallback.", sid);
                        }
                    }
                } else if matches.len() > 1 {
                    info!("Soft Bootstrap: Match for {} is ambiguous (multiple trades in window). Sim fallback.", sid);
                }
            }
        }

        let start_str = start_time.to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        let end_str = now_ct.to_rfc3339_opts(chrono::SecondsFormat::Millis, true);

        info!("Bootstrap DB path: {}", resolved_db_path);
        match crate::db::load_historical_snapshots(&resolved_db_path, &start_str, &end_str) {
            Ok(snapshots) => {
                info!("Loaded {} historical snapshots for replay", snapshots.len());
                let mut last_history_ts: Option<i64> = None;
                for snap in snapshots {
                    if let Ok(snap_ts) = chrono::DateTime::parse_from_rfc3339(&snap.datetime) {
                        let snap_ct = snap_ts.with_timezone(&tz);
                        
                        let spx = crate::db::estimate_spx_from_snapshot(&snap.quotes).unwrap_or_else(|| self.grid.get_spx());
                        self.grid.inject_snapshot(&snap.quotes, spx);

                        // Run sub-strategies
                        let mut strats = self.sub_strategies.lock().await;
                        for (sid, s) in strats.iter_mut() {
                            if snap_ct.time() < s.trade_start_time { continue; }

                            if !s.has_traded_today {
                                if mode == "soft" {
                                    if let Some(lt) = assigned_live_entry.get(sid) {
                                        if let Ok(lt_ts) = chrono::DateTime::parse_from_rfc3339(&lt.timestamp) {
                                            if lt_ts.with_timezone(&tz) <= snap_ct {
                                                info!("Soft Bootstrap: Seeding {} with live trade {} at {}", sid, lt.strategy_id, snap_ct);
                                                let sync_trade = Self::create_sync_entry(lt, snap_ct, sid);
                                                s.portfolio.lock().await.add_trade(&sync_trade, None);
                                                self.live_portfolio.lock().await.add_trade(&sync_trade, None);
                                                s.has_traded_today = true;
                                                s.state = StrategyState::Working;
                                                continue;
                                            }
                                        }
                                    }
                                }

                                // Hard simulation entry
                                if let Some(trade) = check_entry(&self.grid, s, snap_ct, self.config.max_spread_diff, self.config.commission_per_contract) {
                                    info!("Bootstrap [HARD]: Entry for {} via sim logic at {}", sid, snap_ct);
                                    s.portfolio.lock().await.add_trade(&trade, None);
                                    self.live_portfolio.lock().await.add_trade(&trade, None);
                                    s.has_traded_today = true;
                                    s.state = StrategyState::Working;
                                }
                            } else if s.state == StrategyState::Working {
                                let mut s_port = s.portfolio.lock().await;
                                s_port.update_pricing(&self.grid);
                                
                                let end_time = start_time.date_naive().and_time(self.config.end_time).and_local_timezone(tz).unwrap();
                                if snap_ct >= end_time {
                                    if !s_port.positions.is_empty() {
                                        if let Some(exit_trade) = check_exit(&self.grid, &*s_port, snap_ct, sid, self.config.commission_per_contract, spx) {
                                            s.state = StrategyState::Exiting;
                                            s_port.add_trade(&exit_trade, None);
                                            self.live_portfolio.lock().await.add_trade(&exit_trade, None);
                                        }
                                    }
                                } else {
                                    let rebal_trades = check_rebalance(&self.grid, s, &*s_port, snap_ct, start_time.time(), end_time.time(), &self.config);
                                    for trade in rebal_trades {
                                        s_port.add_trade(&trade, None);
                                        self.live_portfolio.lock().await.add_trade(&trade, None);
                                    }
                                }
                            }
                        }

                        // Add to session history every ~30s using elapsed-time check.
                        // Cannot use timestamp() % 30 == 0 because DB timestamps drift
                        // (e.g., 08:34:36, 08:35:37) and almost never land on 30s boundaries.
                        let snap_ts_secs = snap_ct.timestamp();
                        let should_add_hist = match last_history_ts {
                            None => true,
                            Some(last) => snap_ts_secs - last >= 30,
                        };
                        if should_add_hist {
                            last_history_ts = Some(snap_ts_secs);
                            // Reprice live_portfolio so its net_pnl reflects current market prices.
                            self.live_portfolio.lock().await.update_pricing(&self.grid);
                            let sim_pnl = {
                                let mut total = 0.0;
                                for s in strats.values() {
                                    let port = s.portfolio.lock().await;
                                    total += port.net_pnl();
                                }
                                total
                            };
                            // Evaluate historical_broker_port for actual live PnL replay
                            historical_broker_port.update_pricing(&self.grid);
                            
                            // Inject trades that happened BEFORE this snapshot into historical_broker_port
                            for t in &live_trades {
                                if let Ok(t_ts) = chrono::DateTime::parse_from_rfc3339(&t.timestamp) {
                                    if t_ts.with_timezone(&tz) <= snap_ct {
                                        // Ensure we don't add the same trade twice (by checking strategy_id/order_id or just relying on a robust check).
                                        // Wait, the easiest way is to re-evaluate what trades were filled before `snap_ct` and build the portfolio from scratch.
                                    }
                                }
                            }
                            // Actually, simpler to just rebuild historical_broker_port positions at each snapshot
                            historical_broker_port.positions.clear();
                            historical_broker_port.cash = 0.0;
                            historical_broker_port.trades.clear();
                            for t in &live_trades {
                                if let Ok(t_ts) = chrono::DateTime::parse_from_rfc3339(&t.timestamp) {
                                    if t_ts.with_timezone(&tz) <= snap_ct {
                                        historical_broker_port.add_trade(t, None);
                                    }
                                }
                            }
                            historical_broker_port.update_pricing(&self.grid);

                            // Mirror the live-tick dry_run guard: live PnL is always 0 in sim mode.
                            let live_pnl = if self.config.dry_run {
                                0.0
                            } else {
                                historical_broker_port.net_pnl()
                            };

                            let mut hist = self.session_history.lock().await;
                            hist.push_back(HistoryPoint {
                                ts: snap_ct.to_rfc3339(),
                                spx,
                                live_pnl,
                                sim_pnl
                            });
                            if hist.len() > 1000 {
                                hist.pop_front();
                            }
                        }
                    }
                }
            }
            Err(e) => {
                error!("Failed to load historical snapshots for bootstrap: {:?}", e);
            }
        }
        
        info!("Bootstrap complete. Transitioning to live ticking.");
    }

    /// Reconcile startup positions. If any open positions are found, conservatively
    /// treat all sub-strategies as already traded today to prevent duplicate entries.
    pub async fn reconcile_startup_positions(&self, positions: &[crate::execution::BrokerPosition]) {
        if !positions.is_empty() {
            warn!("Found {} existing open options positions at startup. Marking affected strategies as already traded.", positions.len());
            let mut strats = self.sub_strategies.lock().await;
            for (sid, s) in strats.iter_mut() {
                // Minimum viable fix: We mark ALL strategies as Working to prevent duplicate entries.
                // Full fix: store strategy_id in broker order metadata and match here.
                info!("Conservative Startup Guard: Marking strategy {} as Working / has_traded_today.", sid);
                s.has_traded_today = true;
                s.state = StrategyState::Working;
            }
        } else {
            info!("No open positions found at startup. Supervisor starting fresh.");
        }
    }

    pub async fn create_execution_plan(&self, trade: &Trade) -> (Vec<String>, Vec<OptionLeg>) {
        let mut to_cancel = Vec::new();
        let mut protected_ids = std::collections::HashSet::new();
        let mut working_qtys: std::collections::HashMap<(i64, String), f64> = std::collections::HashMap::new();


        let working = self.working_orders.lock().await;
        for wo in working.iter() {
            let wid = wo.get("orderId")
                .and_then(|v| {
                    if v.is_number() { Some(v.to_string()) } else { v.as_str().map(|s| s.to_string()) }
                })
                .unwrap_or_default();
            if wid.is_empty() { continue; }

            // Check if order is SPX
            let mut is_spx = false;
            if let Some(leg_array) = wo.get("orderLegCollection").and_then(|v| v.as_array()) {
                for leg in leg_array {
                    if let Some(instr_obj) = leg.get("instrument") {
                        if let Some(sym) = instr_obj.get("symbol").and_then(|v| v.as_str()) {
                            if sym.contains("SPX") {
                                is_spx = true;
                                break;
                            }
                        }
                    }
                }
            }
            if !is_spx { continue; }

            let order_strat_id = wo.get("strategy_id").and_then(|v| v.as_str()).unwrap_or("");
            let is_recon_purpose = trade.purpose == "RECONCILIATION" || trade.purpose == "FLATTEN";
            let belongs_here = order_strat_id == trade.strategy_id || is_recon_purpose;

            // If the order is a GAP_RECON order, and the current trade is NOT a RECONCILIATION/FLATTEN trade,
            // we should completely ignore it. Normal sub-strategies do not manage or cancel GAP_RECON orders.
            if order_strat_id == "GAP_RECON" && !is_recon_purpose {
                continue;
            }

            let mut is_stale = !belongs_here && !order_strat_id.is_empty();
            let mut order_legs_data = Vec::new();

            if let Some(leg_array) = wo.get("orderLegCollection").and_then(|v| v.as_array()) {
                for leg in leg_array {
                    let instr = leg.get("instruction").and_then(|v| v.as_str()).unwrap_or("");
                    let qty = leg.get("quantity").and_then(|v| v.as_f64()).unwrap_or(0.0);
                    let instr_obj = leg.get("instrument").cloned().unwrap_or(serde_json::Value::Null);
                    let symbol = instr_obj.get("symbol").and_then(|v| v.as_str()).unwrap_or("");

                    let (strike, side) = if let Some(parsed) = parse_occ_symbol(symbol) {
                        (parsed.strike, parsed.side)
                    } else {
                        let s = instr_obj.get("strikePrice").and_then(|v| v.as_f64()).unwrap_or(0.0);
                        let sd = instr_obj.get("putCall").and_then(|v| v.as_str()).unwrap_or("").to_string();
                        (s, sd)
                    };

                    if strike > 0.0 && !side.is_empty() {
                        let k = (strike.round() as i64, side);
                        let mult = if instr.contains("BUY") { 1.0 } else { -1.0 };
                        // Schwab stores leg.quantity as the total contract count,
                        // so order_qty (top-level quantity) would double-count.
                        let working_qty = qty * mult;

                        let target_leg = trade.legs.iter().find(|l| (l.strike.round() as i64) == k.0 && l.side == k.1);
                        match target_leg {
                            Some(t_leg) => {
                                // If the direction is opposite, the order is stale!
                                if (t_leg.quantity > 0 && working_qty < 0.0) || (t_leg.quantity < 0 && working_qty > 0.0) {
                                    is_stale = true;
                                }
                            }
                            None => {
                                is_stale = true;
                            }
                        }
                        order_legs_data.push((k, working_qty));
                    } else {
                        is_stale = true;
                    }
                }
            } else {
                is_stale = true;
            }

            if is_stale {
                to_cancel.push(wid);
            } else {
                protected_ids.insert(wid);
                for (k, val) in order_legs_data {
                    *working_qtys.entry(k).or_insert(0.0) += val;
                }
            }
        }

        // Subtract protected working quantities from target trade quantities
        let mut remaining_legs = Vec::new();
        for leg in trade.legs.iter() {
            let key = (leg.strike.round() as i64, leg.side.clone());
            let needed = leg.quantity as f64;
            let already_covered = *working_qtys.get(&key).unwrap_or(&0.0);

            let mut to_fill = needed;
            if (needed > 0.0 && already_covered > 0.0) || (needed < 0.0 && already_covered < 0.0) {
                if needed.abs() > already_covered.abs() {
                    to_fill = needed - already_covered;
                } else {
                    to_fill = 0.0;
                }
            }

            if to_fill.abs() > 0.01 {
                let mut new_leg = leg.clone();
                new_leg.quantity = to_fill as i32;
                remaining_legs.push(new_leg);
            }
        }

        (to_cancel, remaining_legs)
    }

    /// Compare simulated combined portfolio with live broker reality and suggest syncing trades
    pub async fn check_reconciliation(&self, account_hash: &str) -> Result<()> {
        // Only run if there is no pending trade currently
        if self.pending_trade.lock().await.is_some() {
            return Ok(());
        }
        // Don't re-reconcile while GAP_RECON orders are already working or pending placement.
        // This prevents the re-reconciliation loop where the next tick fires before broker
        // positions reflect the just-placed working orders.
        {
            let wo = self.working_orders.lock().await;
            if wo.iter().any(|o| o.get("strategy_id").and_then(|v| v.as_str()) == Some("GAP_RECON")) {
                return Ok(());
            }
        }

        let live_positions = match self.execution_client.get_live_positions(account_hash).await {
            Ok(pos) => pos,
            Err(e) => {
                warn!("Reconciliation: Failed to fetch live positions from Schwab: {:?}", e);
                return Ok(());
            }
        };

        // Sync the actual broker snapshot back into our Live Portfolio tracking for the UI
        {
            let mut port = self.broker_portfolio.lock().await;
            port.sync_from_broker(&live_positions);
            port.update_pricing(&self.grid);
        }

        let enabled = self.trading_enabled.load(std::sync::atomic::Ordering::Relaxed);
        if !enabled {
            return Ok(());
        }

        // 1. Internal Netting math
        let now_ct = Chicago.from_utc_datetime(&chrono::Utc::now().naive_utc());
        {
            let mut strats = self.sub_strategies.lock().await;

            // Gather all symbols that sub-strategies want to trade
            let mut desired_changes: HashMap<String, Vec<(String, i32, chrono::DateTime<chrono_tz::Tz>)>> = HashMap::new();

            for (sid, s) in strats.iter() {
                if let Some(prev) = &s.previous_portfolio {
                    let port = s.portfolio.lock().await;
                    let mut symbols = std::collections::HashSet::new();
                    for pos in &port.positions { symbols.insert(pos.symbol.clone()); }
                    for pos in &prev.positions { symbols.insert(pos.symbol.clone()); }

                    for sym in symbols {
                        let target_qty = port.position_qty_for(&sym);
                        let current_qty = prev.position_qty_for(&sym);
                        let diff = target_qty - current_qty;
                        if diff != 0 {
                            desired_changes.entry(sym)
                                .or_default()
                                .push((sid.clone(), diff, s.last_update_ts.unwrap_or(now_ct)));
                        }
                    }
                }
            }

            for (symbol, mut changes) in desired_changes {
                let mut buys: Vec<(String, i32, chrono::DateTime<chrono_tz::Tz>)> = changes.iter().filter(|&&(_, diff, _)| diff > 0).cloned().collect();
                let mut sells: Vec<(String, i32, chrono::DateTime<chrono_tz::Tz>)> = changes.iter().filter(|&&(_, diff, _)| diff < 0).cloned().collect();

                buys.sort_by_key(|&(_, _, ts)| ts);
                sells.sort_by_key(|&(_, _, ts)| ts);

                let mut buy_idx = 0;
                let mut sell_idx = 0;

                while buy_idx < buys.len() && sell_idx < sells.len() {
                    let (b_sid, mut b_diff, b_ts) = buys[buy_idx].clone();
                    let (s_sid, mut s_diff, s_ts) = sells[sell_idx].clone();
                    let s_diff_abs = -s_diff;

                    let match_qty = b_diff.min(s_diff_abs);

                    let mut mid_price = 0.0;
                    let mut strike = 0.0;
                    let mut side = "CALL".to_string();
                    if let Some(parsed) = parse_occ_symbol(&symbol) {
                        strike = parsed.strike;
                        side = parsed.side;
                        if let Some(quote) = self.grid.quotes.get(&OrderedFloat(strike)) {
                            let leg_quote = if side == "CALL" { &quote.call } else { &quote.put };
                            if let Some(lq) = leg_quote {
                                mid_price = lq.mid;
                            }
                        }
                    }

                    info!("🤝 Internal netting match: {} (wants +{}) and {} (wants {}) net {} contracts of {} at ${:.2}",
                          b_sid, b_diff, s_sid, s_diff, match_qty, symbol, mid_price);

                    if let Some(b_strat) = strats.get_mut(&b_sid) {
                        if let Some(ref mut prev_port) = b_strat.previous_portfolio {
                            let fill_trade = Trade {
                                timestamp: now_ct.to_rfc3339(),
                                legs: vec![OptionLeg {
                                    symbol: symbol.clone(),
                                    strike,
                                    side: side.clone(),
                                    quantity: match_qty,
                                    delta: 0.0,
                                    theta: 0.0,
                                    price: mid_price,
                                    instruction: None,
                                }],
                                credit: -(match_qty as f64) * mid_price * 100.0,
                                commission: 0.0,
                                purpose: "INTERNAL_NETTING".to_string(),
                                strategy_id: b_sid.clone(),
                            };
                            prev_port.add_trade(&fill_trade, Some(vec![mid_price]));
                        }
                    }

                    if let Some(s_strat) = strats.get_mut(&s_sid) {
                        if let Some(ref mut prev_port) = s_strat.previous_portfolio {
                            let fill_trade = Trade {
                                timestamp: now_ct.to_rfc3339(),
                                legs: vec![OptionLeg {
                                    symbol: symbol.clone(),
                                    strike,
                                    side: side.clone(),
                                    quantity: -match_qty,
                                    delta: 0.0,
                                    theta: 0.0,
                                    price: mid_price,
                                    instruction: None,
                                }],
                                credit: (match_qty as f64) * mid_price * 100.0,
                                commission: 0.0,
                                purpose: "INTERNAL_NETTING".to_string(),
                                strategy_id: s_sid.clone(),
                            };
                            prev_port.add_trade(&fill_trade, Some(vec![mid_price]));
                        }
                    }

                    buys[buy_idx].1 -= match_qty;
                    sells[sell_idx].1 += match_qty;

                    if buys[buy_idx].1 == 0 { buy_idx += 1; }
                    if sells[sell_idx].1 == 0 { sell_idx += 1; }
                }
            }

            for s in strats.values_mut() {
                if s.previous_portfolio.is_some() {
                    if let Some(cancelled_time) = s.cancelled_at {
                        if cancelled_time.elapsed() >= std::time::Duration::from_secs(5) {
                            s.cancelled_at = None;
                            s.finalize_partial_fill().await;
                            continue;
                        }
                    }
                    s.check_and_finalize_fill().await;
                }
            }
        }

        let combined = self.get_simulated_combined_portfolio().await;
        {
            let mut live_port = self.live_portfolio.lock().await;
            *live_port = combined;
        }

        let sim_positions = {
            let port = self.live_portfolio.lock().await;
            port.positions.clone()
        };

        // Create mapping of strike/side to quantity for sim and live
        use std::collections::HashMap;
        let mut sim_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
        for p in &sim_positions {
            sim_map.insert((OrderedFloat(p.strike), p.side.clone()), p.quantity);
        }

        let mut live_map: HashMap<(OrderedFloat<f64>, String), i32> = HashMap::new();
        for p in &live_positions {
            live_map.insert((OrderedFloat(p.strike), p.side.clone()), p.quantity);
        }

        let (close_adjustments, open_adjustments) =
            separate_recon_adjustments(&sim_map, &live_map);

        let has_close_adjustments = !close_adjustments.is_empty();
        let close_len = close_adjustments.len();
        let open_len = open_adjustments.len();

        // Route ALL adjustments together so the confirmation modal shows both the flatten
        // and the non-conflicting IC opens simultaneously. Inside execute_trade, close and
        // open legs are chunked separately so get_smart_chunks never mixes them.
        let mut adjustments_to_route = close_adjustments;
        adjustments_to_route.extend(open_adjustments);

        if adjustments_to_route.is_empty() {
            return Ok(());
        }

        info!("⚠️ Reconciliation Discrepancy: Found {} close / {} open adjustments. Routing {} legs total.",
             close_len, open_len, adjustments_to_route.len());

        let mut legs = Vec::new();
        let mut total_credit = 0.0;

        for (strike, side, qty) in adjustments_to_route {
            // Find option in grid
            if let Some(quote) = self.grid.quotes.get(&OrderedFloat(strike)) {
                let leg_quote_opt = if side == "CALL" {
                    quote.call.as_ref()
                } else {
                    quote.put.as_ref()
                };

                if let Some(leg_quote) = leg_quote_opt {
                    legs.push(OptionLeg {
                        symbol: leg_quote.symbol.clone(),
                        strike,
                        side: side.clone(),
                        quantity: qty,
                        delta: leg_quote.delta,
                        theta: leg_quote.theta,
                        price: leg_quote.mid,
                        instruction: None,
                    });
                    total_credit += -(qty as f64) * leg_quote.mid * 100.0;
                } else {
                    error!("Reconciliation: Strike {} {} has no quote in grid", strike, side);
                }
            } else {
                error!("Reconciliation: Strike {} not found in grid", strike);
            }
        }

        if !legs.is_empty() {
            let total_contracts: i32 = legs.iter().map(|l| l.quantity.abs()).sum();
            let commission = total_contracts as f64 * self.config.commission_per_contract;
            let now_ct = Chicago.from_utc_datetime(&chrono::Utc::now().naive_utc());
            let timestamp = now_ct.to_rfc3339();

            // Label purpose as FLATTEN if this batch contains close legs, otherwise RECONCILIATION
            let purpose = if has_close_adjustments {
                "FLATTEN".to_string()
            } else {
                "RECONCILIATION".to_string()
            };

            let trade = Trade {
                timestamp,
                legs,
                credit: total_credit,
                commission,
                purpose,
                strategy_id: "GAP_RECON".to_string(),
            };

            // Run execution plan check!
            let (to_cancel, remaining_legs) = self.create_execution_plan(&trade).await;
            if to_cancel.is_empty() && remaining_legs.is_empty() {
                info!("Reconciliation GAP_SYNC generated, but broker already has matching orders. Suppressing pop.");
                return Ok(());
            }

            info!("Routing reconciliation trade to pending_trade for confirmation: {:?}", trade);
            *self.pending_trade.lock().await = Some(PendingTrade {
                strat_id: "GAP_RECON".to_string(),
                trade,
                ui_legs: remaining_legs,
                to_cancel,
            });
        }

        Ok(())
    }

    /// Process sub-strategy optimistic updates and pending fill finalizations.
    pub async fn process_pending_finalizations(&self) {
        let mut strats = self.sub_strategies.lock().await;
        for s in strats.values_mut() {
            if s.previous_portfolio.is_some() {
                if let Some(cancelled_time) = s.cancelled_at {
                    if cancelled_time.elapsed() >= std::time::Duration::from_secs(5) {
                        s.cancelled_at = None;
                        s.finalize_partial_fill().await;
                        continue;
                    }
                }
                s.check_and_finalize_fill().await;
            }
        }
    }

    /// Execute a single strategy check tick.
    pub async fn tick(&self) -> Result<()> {
        let now_ct = if std::env::var("TERMINATOR_TEST_ENV").is_ok() {
            Chicago.with_ymd_and_hms(2026, 5, 22, 9, 0, 0).unwrap()
        } else {
            Chicago.from_utc_datetime(&chrono::Utc::now().naive_utc())
        };
        let current_time = now_ct.time();

        let start_time = self.config.start_time;
        let end_time = self.config.end_time;

        if current_time < start_time || current_time > end_time {
            return Ok(());
        }

        // Append session history at ~30s granularity inside trading hours
        {
            let should_record = {
                let mut last_hist = self.last_history_ct.lock().await;
                match *last_hist {
                    None => {
                        *last_hist = Some(now_ct);
                        true
                    }
                    Some(last) => {
                        if now_ct.signed_duration_since(last).num_seconds() >= 30 {
                            *last_hist = Some(now_ct);
                            true
                        } else {
                            false
                        }
                    }
                }
            };

            if should_record {
                let mut hist = self.session_history.lock().await;
                let spx = self.grid.get_underlying_price();
                let net_pnl = self.live_portfolio.lock().await.net_pnl();
                let (live_pnl, sim_pnl) = if self.config.dry_run {
                    (0.0, net_pnl)
                } else {
                    (net_pnl, 0.0)
                };
                hist.push_back(HistoryPoint {
                    ts: now_ct.to_rfc3339(),
                    spx,
                    live_pnl,
                    sim_pnl,
                });
                if hist.len() > 1000 {
                    hist.pop_front();
                }
            }
        }

        let account_hash = {
            let h = self.account_hash.lock().await;
            h.clone().unwrap_or_default()
        };
        let combined = self.get_simulated_combined_portfolio().await;
        {
            let mut live_port = self.live_portfolio.lock().await;
            *live_port = combined;
        }

        if account_hash.is_empty() {
            return Ok(());
        }
        if let Err(e) = self.check_reconciliation(&account_hash).await {
            error!("Failed to check reconciliation: {:?}", e);
        }

        let mut strats = self.sub_strategies.lock().await;
        for (sid, s) in strats.iter_mut() {
            // Do not evaluate entry/exit if there is already a working order for this strategy
            let has_working = {
                let wo = self.working_orders.lock().await;
                wo.iter().any(|o| o.get("strategy_id").and_then(|v| v.as_str()) == Some(sid))
            };
            if has_working {
                continue;
            }

            if s.state == StrategyState::Idle && current_time >= s.trade_start_time {
                // Swarm fix: skip missed tranches to prevent identical simultaneous entries on late start
                let elapsed_secs = (current_time - s.trade_start_time).num_seconds();
                if elapsed_secs > 600 {
                    info!("⏭️ Sub-strategy {} start time ({}) was missed by {}s. Skipping entry for today.", sid, s.trade_start_time, elapsed_secs);
                    s.state = StrategyState::Exiting;
                    s.has_traded_today = true;
                    continue;
                }

                info!("🔔 Sub-strategy {} start time reached ({}). Checking entry...", sid, s.trade_start_time);
                if let Some(trade) = check_entry(&self.grid, s, now_ct, self.config.max_spread_diff, self.config.commission_per_contract) {
                    info!("🎯 Entry signal triggered for {}! Net credit: ${:.2}. Executing...", sid, trade.credit);

                    if s.previous_portfolio.is_some() {
                        info!("Skipping entry re-firing for {} because a prior optimistic update is still unconfirmed.", sid);
                        continue;
                    }

                    let enabled = self.trading_enabled.load(std::sync::atomic::Ordering::Relaxed) || self.config.dry_run;
                    if enabled {
                        s.previous_portfolio = Some((*s.portfolio.lock().await).clone());
                        s.snapshot_trade_count = Some(s.previous_portfolio.as_ref().unwrap().trades.len());
                        s.last_update_ts = Some(now_ct);
                        s.state = StrategyState::EnteringSpread;
                    } else {
                        s.state = StrategyState::Working;
                    }

                    s.portfolio.lock().await.add_trade(&trade, None);
                    info!("Updated sim portfolio for entry of {} (Live trading enabled: {})", sid, enabled);
                    continue;
                }
            } else if s.state == StrategyState::Working {
                let mut s_port = s.portfolio.lock().await;
                s_port.update_pricing(&self.grid);

                if current_time >= end_time {
                    // --- EXIT ---
                    if !s_port.positions.is_empty() {
                        let spx = self.grid.get_underlying_price();

                        if let Some(exit_trade) = check_exit(&self.grid, &*s_port, now_ct, &s.sid, self.config.commission_per_contract, spx) {
                            if s.previous_portfolio.is_some() {
                                info!("Skipping exit re-firing for {} because a prior optimistic update is still unconfirmed.", sid);
                                continue;
                            }

                            let enabled = self.trading_enabled.load(std::sync::atomic::Ordering::Relaxed) || self.config.dry_run;
                            if enabled {
                                s.previous_portfolio = Some((*s_port).clone());
                                s.snapshot_trade_count = Some(s.previous_portfolio.as_ref().unwrap().trades.len());
                                s.last_update_ts = Some(now_ct);
                                s.state = StrategyState::Exiting;
                            } else {
                                s.state = StrategyState::Idle;
                                s.has_traded_today = true;
                            }

                            s_port.add_trade(&exit_trade, None);
                            info!("Updated sim portfolio for exit of {} (Live trading enabled: {})", sid, enabled);
                            continue;
                        }
                    }
                } else {
                    // --- REBALANCE ---
                    let rebal_trades = check_rebalance(&self.grid, s, &*s_port, now_ct, start_time, end_time, &self.config);

                    if !rebal_trades.is_empty() {
                        if s.previous_portfolio.is_some() {
                            info!("Skipping rebalance re-firing for {} because a prior optimistic update is still unconfirmed.", sid);
                            continue;
                        }

                        let enabled = self.trading_enabled.load(std::sync::atomic::Ordering::Relaxed) || self.config.dry_run;
                        if enabled {
                            s.previous_portfolio = Some((*s_port).clone());
                            s.snapshot_trade_count = Some(s.previous_portfolio.as_ref().unwrap().trades.len());
                            s.last_update_ts = Some(now_ct);
                        }

                        for trade in rebal_trades {
                            s_port.add_trade(&trade, None);
                        }
                        info!("Updated sim portfolio for rebalance of {} (Live trading enabled: {})", sid, enabled);
                    }
                }
            }
        }
        drop(strats);
        self.process_pending_finalizations().await;

        // Keep live_portfolio position prices in sync with sub-strategy portfolios.
        // Sub-strategies call update_pricing() above; without this, live_portfolio.gross_pnl()
        // would use stale entry prices, making the Sim Total PnL card wrong.
        self.live_portfolio.lock().await.update_pricing(&self.grid);

        Ok(())
    }

    /// Process parsed order events from the ACCT_ACTIVITY feed.
    pub async fn process_account_event(&self, event: crate::parser::OrderActivityEvent) {
        info!("💼 Strategy Supervisor processing event: Order ID = {}, Type = {}, Status = {}, Legs count = {}",
            event.order_id, event.message_type, event.status, event.legs.len());

        let mut strats = self.sub_strategies.lock().await;

        if event.status == "Cancelled" || event.status == "Rejected" {
            warn!("⚠️ Order {} was cancelled/rejected. Finalizing partial fills or reverting.", event.order_id);
            if event.legs.is_empty() {
                // No leg data (e.g. OrderUROutCompleted) — cannot determine which strategies
                // are affected. Skip to avoid reverting unrelated optimistic updates.
                // The paired ExecutionCreated event (which does carry legs) already handled any
                // needed revert for this order.
            } else {
                let cancelled_symbols: std::collections::HashSet<&str> =
                    event.legs.iter().map(|l| l.symbol.as_str()).collect();
                for s in strats.values_mut() {
                    if let Some(ref prev) = s.previous_portfolio {
                        let port = s.portfolio.lock().await;
                        let affects_this_strategy = cancelled_symbols.iter().any(|sym| {
                            port.position_qty_for(sym) != prev.position_qty_for(sym)
                        });
                        drop(port);
                        if affects_this_strategy {
                            s.cancelled_at = Some(std::time::Instant::now());
                        }
                    }
                }
            }
        } else if event.status == "Filled" || event.message_type == "ExecutionCreated" {
            let now_ct = Chicago.from_utc_datetime(&chrono::Utc::now().naive_utc());
            for leg in &event.legs {
                let symbol = &leg.symbol;
                let is_buy = leg.buy_sell.to_lowercase().contains("buy");
                let sign = if is_buy { 1 } else { -1 };
                let mut remaining_fill = leg.quantity as i32;

                // Find all strats waiting for fills in this direction
                let mut waiting = Vec::new();
                for (sid, s) in strats.iter() {
                    if let Some(prev) = &s.previous_portfolio {
                        let port = s.portfolio.lock().await;
                        let target_qty = port.position_qty_for(symbol);
                        let current_qty = prev.position_qty_for(symbol);
                        let diff = target_qty - current_qty;
                        if diff != 0 && (diff > 0) == is_buy {
                            waiting.push((sid.clone(), diff.abs(), s.last_update_ts.unwrap_or(now_ct)));
                        }
                    }
                }

                // Sort FIFO
                waiting.sort_by_key(|&(_, _, ts)| ts);

                for (sid, needed_qty, _) in waiting {
                    if remaining_fill <= 0 {
                        break;
                    }
                    let alloc = needed_qty.min(remaining_fill);
                    let alloc_qty = alloc * sign;

                    if let Some(s) = strats.get_mut(&sid) {
                        if let Some(ref mut prev_port) = s.previous_portfolio {
                            let mut strike = 0.0;
                            let mut side = "CALL".to_string();
                            let mut mid_price = 0.0;
                            if let Some(parsed) = parse_occ_symbol(symbol) {
                                strike = parsed.strike;
                                side = parsed.side;
                                if let Some(quote) = self.grid.quotes.get(&OrderedFloat(strike)) {
                                    let leg_quote = if side == "CALL" { &quote.call } else { &quote.put };
                                    if let Some(lq) = leg_quote {
                                        mid_price = lq.mid;
                                    }
                                }
                            }

                            let fill_trade = Trade {
                                timestamp: now_ct.to_rfc3339(),
                                legs: vec![OptionLeg {
                                    symbol: symbol.clone(),
                                    strike,
                                    side: side.clone(),
                                    quantity: alloc_qty,
                                    delta: 0.0,
                                    theta: 0.0,
                                    price: mid_price,
                                    instruction: None,
                                }],
                                credit: -(alloc_qty as f64) * mid_price * 100.0,
                                commission: 0.0,
                                purpose: "BROKER_FILL".to_string(),
                                strategy_id: sid.clone(),
                            };
                            prev_port.add_trade(&fill_trade, Some(vec![mid_price]));
                        }
                    }
                    remaining_fill -= alloc;
                }
            }

            for s in strats.values_mut() {
                if s.previous_portfolio.is_some() {
                    s.check_and_finalize_fill().await;
                }
            }
        }
        
        // Handle execution_queue updates
        let mut chunks_to_submit = Vec::new();
        if event.status == "Filled" {
            let mut queue = self.execution_queue.lock().await;
            let mut i = 0;
            while i < queue.len() {
                if queue[i].awaiting_order_id == event.order_id {
                    let dc = queue.remove(i);
                    chunks_to_submit.push(dc.chunk);
                } else {
                    i += 1;
                }
            }
        } else if event.status == "Cancelled" || event.status == "Rejected" {
            let mut queue = self.execution_queue.lock().await;
            let mut i = 0;
            while i < queue.len() {
                if queue[i].awaiting_order_id == event.order_id {
                    warn!("⚠️ Dropping deferred chunk from execution queue because prerequisite order {} was cancelled/rejected: {:?}", event.order_id, queue[i].chunk);
                    queue.remove(i);
                } else {
                    i += 1;
                }
            }
        }

        // Drop the strats lock before making async REST calls to prevent blocking
        drop(strats);

        // Submit any deferred chunks that have been freed
        if !chunks_to_submit.is_empty() {
            let hash_opt = self.account_hash.lock().await.clone();
            if let Some(hash) = hash_opt {
                let broker_positions = self.broker_portfolio.lock().await.positions.clone();
                for chunk in chunks_to_submit {
                    info!("Submitting deferred chunk following fill of prerequisite order {}: {:?}", event.order_id, chunk);
                    let reconstructed_trade = Trade {
                        timestamp: Chicago.from_utc_datetime(&chrono::Utc::now().naive_utc()).to_rfc3339(),
                        legs: chunk,
                        credit: 0.0,
                        commission: 0.0,
                        purpose: "GAP_RECON".to_string(),
                        strategy_id: "GAP_RECON".to_string(),
                    };
                    let is_dry_run = self.config.dry_run;
                    let execute_res = execute_trade(
                        &self.execution_client,
                        &hash,
                        &reconstructed_trade,
                        is_dry_run,
                        self.config.order_offset,
                        &broker_positions,
                        &[],
                    ).await;

                    if !is_dry_run {
                        match execute_res {
                            Ok((executed, deferred)) => {
                                for ex in executed {
                                    if let Some(oid) = ex.order_id {
                                        self.working_orders.lock().await.push(json!({
                                            "orderId": oid,
                                            "strategy_id": "GAP_RECON",
                                            "quantity": ex.quantity,
                                            "orderLegCollection": ex.legs,
                                            "local_stub_ts": chrono::Utc::now().timestamp()
                                        }));
                                    }
                                }
                                if !deferred.is_empty() {
                                    let mut queue = self.execution_queue.lock().await;
                                    for dc in deferred {
                                        info!("Queueing deferred flip chunk waiting for order ID: {}", dc.awaiting_order_id);
                                        queue.push(dc);
                                    }
                                }
                            }
                            Err((executed, e)) => {
                                for ex in executed {
                                    if let Some(oid) = ex.order_id {
                                        self.working_orders.lock().await.push(json!({
                                            "orderId": oid,
                                            "strategy_id": "GAP_RECON",
                                            "quantity": ex.quantity,
                                            "orderLegCollection": ex.legs,
                                            "local_stub_ts": chrono::Utc::now().timestamp()
                                        }));
                                    }
                                }
                                error!("Failed to submit deferred chunk: {:?}", e);
                            }
                        }
                    }
                }
            }
        }

        let hash_opt = self.account_hash.lock().await.clone();
        if let Some(_) = hash_opt {
            let need_full = event.status == "Filled" || event.message_type == "ExecutionCreated";
            let _ = self.fast_sync_tx.try_send(need_full);
        }
    }

    pub async fn confirm_trade(&self, strat_id: &str, overrides: Vec<LegOverride>) -> Result<()> {
        let pending = { self.pending_trade.lock().await.take() };
        if let Some(t) = pending {
            let account_hash = self.account_hash.lock().await.clone().unwrap_or_default();

            // 1. Cancel outdated/stale working orders first
            for oid in &t.to_cancel {
                info!("Cancelling outdated/opposite working order: {}", oid);
                if let Err(e) = self.execution_client.cancel_order(&account_hash, oid).await {
                    warn!("Failed to cancel working order {}: {:?}", oid, e);
                }
            }

            if t.ui_legs.is_empty() {
                info!("All legs covered by working orders after cancel phase. No new order to place.");
                return Ok(());
            }

            // Create temporary trade with remaining legs
            let mut adjusted_trade = t.trade.clone();
            adjusted_trade.legs = t.ui_legs.clone();

            let is_recon_or_flatten = t.trade.purpose == "RECONCILIATION" || t.trade.purpose == "FLATTEN";
            if strat_id == "GAP_RECON" || is_recon_or_flatten {
                let positions = {
                    let broker_port = self.broker_portfolio.lock().await;
                    broker_port.positions.clone()
                };
                let is_dry_run = self.config.dry_run;
                let execute_res = execute_trade(
                    &self.execution_client, &account_hash, &adjusted_trade,
                    is_dry_run, self.config.order_offset, &positions,
                    &overrides
                ).await;

                if is_dry_run {
                    let now_ct = Chicago.from_utc_datetime(&chrono::Utc::now().naive_utc());
                    let mut strats = self.sub_strategies.lock().await;
                    for leg in &adjusted_trade.legs {
                        let symbol = &leg.symbol;
                        let is_buy = leg.quantity > 0;
                        let sign = if is_buy { 1 } else { -1 };
                        let mut remaining_fill = leg.quantity.abs();

                        let mut waiting = Vec::new();
                        for (sid, s) in strats.iter() {
                            if let Some(prev) = &s.previous_portfolio {
                                let port = s.portfolio.lock().await;
                                let target_qty = port.position_qty_for(symbol);
                                let current_qty = prev.position_qty_for(symbol);
                                let diff = target_qty - current_qty;
                                if diff != 0 && (diff > 0) == is_buy {
                                    waiting.push((sid.clone(), diff.abs(), s.last_update_ts.unwrap_or(now_ct)));
                                }
                            }
                        }

                        waiting.sort_by_key(|&(_, _, ts)| ts);

                        for (sid, needed_qty, _) in waiting {
                            if remaining_fill <= 0 {
                                break;
                            }
                            let alloc = needed_qty.min(remaining_fill);
                            let alloc_qty = alloc * sign;

                            if let Some(s) = strats.get_mut(&sid) {
                                if let Some(ref mut prev_port) = s.previous_portfolio {
                                    let fill_trade = Trade {
                                        timestamp: now_ct.to_rfc3339(),
                                        legs: vec![OptionLeg {
                                            symbol: symbol.clone(),
                                            strike: leg.strike,
                                            side: leg.side.clone(),
                                            quantity: alloc_qty,
                                            delta: leg.delta,
                                            theta: leg.theta,
                                            price: leg.price,
                                            instruction: None,
                                        }],
                                        credit: -(alloc_qty as f64) * leg.price * 100.0,
                                        commission: 0.0,
                                        purpose: "DRY_RUN_FILL".to_string(),
                                        strategy_id: sid.clone(),
                                    };
                                    prev_port.add_trade(&fill_trade, Some(vec![leg.price]));
                                }
                            }
                            remaining_fill -= alloc;
                        }
                    }

                    for s in strats.values_mut() {
                        if s.previous_portfolio.is_some() {
                            s.check_and_finalize_fill().await;
                        }
                    }

                    drop(strats);
                    let combined = self.get_simulated_combined_portfolio().await;
                    {
                        let mut live_port = self.live_portfolio.lock().await;
                        *live_port = combined.clone();
                    }
                    // In dry_run, simulate the broker accepting the trade so reconciliation
                    // doesn't re-detect the same gap on the very next tick.
                    {
                        let mut broker_port = self.broker_portfolio.lock().await;
                        *broker_port = combined;
                    }
                } else {
                    match execute_res {
                        Ok((chunks, deferred)) => {
                            for chunk in chunks {
                                if let Some(oid) = chunk.order_id {
                                    self.working_orders.lock().await.push(json!({
                                        "orderId": oid,
                                        "strategy_id": "GAP_RECON",
                                        "quantity": chunk.quantity,
                                        "orderLegCollection": chunk.legs,
                                        "local_stub_ts": chrono::Utc::now().timestamp()
                                    }));
                                }
                            }
                            if !deferred.is_empty() {
                                let mut queue = self.execution_queue.lock().await;
                                for dc in deferred {
                                    info!("Queueing deferred flip chunk waiting for order ID: {}", dc.awaiting_order_id);
                                    queue.push(dc);
                                }
                            }
                        }
                        Err((chunks, e)) => {
                            for chunk in chunks {
                                if let Some(oid) = chunk.order_id {
                                    self.working_orders.lock().await.push(json!({
                                        "orderId": oid,
                                        "strategy_id": "GAP_RECON",
                                        "quantity": chunk.quantity,
                                        "orderLegCollection": chunk.legs,
                                        "local_stub_ts": chrono::Utc::now().timestamp()
                                    }));
                                }
                            }
                            return Err(e);
                        }
                    }
                }
            } else {
                // pending_trade is only ever set for RECONCILIATION/FLATTEN trades (see check_reconciliation).
                // A non-GAP_RECON confirm here would double-count the optimistic add_trade from tick().
                warn!("confirm_trade: unexpected non-GAP_RECON pending trade (purpose={}, strat_id={}). Ignoring.", t.trade.purpose, strat_id);
            }
        }
        Ok(())
    }

    pub async fn dismiss_trade(&self, strat_id: &str) {
        let _ = self.pending_trade.lock().await.take();
        let mut strats = self.sub_strategies.lock().await;
        if strat_id == "GAP_RECON" {
            for s in strats.values_mut() {
                if s.previous_portfolio.is_some() {
                    s.revert_optimistic_update().await;
                }
            }
        } else if let Some(s) = strats.get_mut(strat_id) {
            if s.previous_portfolio.is_some() {
                s.revert_optimistic_update().await;
            }
        }
    }

    pub fn set_timer_paused(&self, is_paused: bool) {
        self.timer_paused.store(is_paused, std::sync::atomic::Ordering::Relaxed);
    }

    pub fn toggle_trading_enabled(&self) -> bool {
        let current = self.trading_enabled.load(std::sync::atomic::Ordering::Relaxed);
        self.trading_enabled.store(!current, std::sync::atomic::Ordering::Relaxed);
        !current
    }

    pub async fn run_fast_sync_loop(self: Arc<Self>) {
        let mut rx = {
            let mut opt = self.fast_sync_rx.lock().await;
            opt.take().expect("fast_sync_rx already taken")
        };
        info!("🔄 Fast-sync background worker loop started.");
        while let Some(mut need_full) = rx.recv().await {
            // Coalesce multiple rapid fill signals into a single REST sync
            tokio::time::sleep(Duration::from_millis(50)).await;
            while let Ok(next_need_full) = rx.try_recv() {
                need_full = need_full || next_need_full;
            }

            let hash_opt = self.account_hash.lock().await.clone();
            if let Some(hash) = hash_opt {
                // Perform working orders sync
                match self.execution_client.get_working_orders(&hash).await {
                    Ok(orders) => {
                        let num_working = orders.len();
                        {
                            let mut wo = self.working_orders.lock().await;
                            let mut merged_orders = Vec::new();
                            let mut broker_oids = std::collections::HashSet::new();

                            for mut new_order in orders {
                                let new_id = new_order.get("orderId").or_else(|| new_order.get("id"));
                                let new_id_str = new_id.and_then(|id| if id.is_number() { Some(id.to_string()) } else { id.as_str().map(|s| s.to_string()) });

                                if let Some(ref id_str) = new_id_str {
                                    broker_oids.insert(id_str.clone());
                                }

                                let old_strat_id = if let Some(ref id_str) = new_id_str {
                                    wo.iter().find(|old_order| {
                                        let old_id = old_order.get("orderId").or_else(|| old_order.get("id"));
                                        let old_id_str = old_id.and_then(|id| if id.is_number() { Some(id.to_string()) } else { id.as_str().map(|s| s.to_string()) });
                                        old_id_str == Some(id_str.clone())
                                    }).and_then(|old_order| old_order.get("strategy_id").cloned())
                                } else {
                                    None
                                };

                                if let Some(strat_id) = old_strat_id {
                                    if let Some(obj) = new_order.as_object_mut() {
                                        obj.insert("strategy_id".to_string(), strat_id);
                                    }
                                }
                                merged_orders.push(new_order);
                            }

                            let now = chrono::Utc::now().timestamp();
                            for old_order in wo.iter() {
                                let old_id = old_order.get("orderId").or_else(|| old_order.get("id"));
                                let old_id_str = old_id.and_then(|id| if id.is_number() { Some(id.to_string()) } else { id.as_str().map(|s| s.to_string()) });
                                
                                if let Some(id_str) = old_id_str {
                                    if !broker_oids.contains(&id_str) {
                                        if let Some(stub_ts) = old_order.get("local_stub_ts").and_then(|v| v.as_i64()) {
                                            if now - stub_ts < 10 { // 10 seconds grace period
                                                merged_orders.push(old_order.clone());
                                            }
                                        }
                                    }
                                }
                            }

                            *wo = merged_orders;
                        }

                        info!("🔄 Fast-synced {} working orders from Schwab REST API", num_working);
                    }
                    Err(e) => warn!("Failed to fast-sync working orders: {:?}", e),
                }

                // Perform today's filled orders sync if requested
                if need_full {
                    match self.execution_client.get_today_filled_orders(&hash, self.config.commission_per_contract).await {
                        Ok(trades) => {
                            {
                                let mut bp = self.broker_portfolio.lock().await;
                                bp.trades.clear();
                                bp.cash = 0.0;
                                bp.positions.clear();
                                for trade in &trades {
                                    bp.add_trade(trade, None);
                                }
                            }
                            info!("🔄 Fast-synced filled trades into broker portfolio (cleared and rebuilt to avoid duplicates)");
                            self.apply_broker_fills_to_strategies(&trades).await;
                        }
                        Err(e) => warn!("Failed to fast-sync filled trades: {:?}", e),
                    }
                }
            }
        }
    }

    pub async fn apply_broker_fills_to_strategies(&self, trades: &[Trade]) {
        let mut strats = self.sub_strategies.lock().await;
        let now_ct = Chicago.from_utc_datetime(&chrono::Utc::now().naive_utc());

        for trade in trades {
            // Check if this trade (order ID) has already been applied to any sub-strategy
            let mut already_applied = false;
            for s in strats.values() {
                let port = s.portfolio.lock().await;
                if port.trades.iter().any(|t| t.strategy_id == trade.strategy_id) {
                    already_applied = true;
                    break;
                }
                if let Some(prev) = &s.previous_portfolio {
                    if prev.trades.iter().any(|t| t.strategy_id == trade.strategy_id) {
                        already_applied = true;
                        break;
                    }
                }
            }

            if already_applied {
                continue;
            }

            // Distribute this trade's legs to waiting strategies
            for leg in &trade.legs {
                let symbol = &leg.symbol;
                let is_buy = leg.quantity > 0;
                let sign = if is_buy { 1 } else { -1 };
                let mut remaining_fill = leg.quantity.abs();

                let mut mid_price = 0.0;
                let mut delta = 0.0;
                let mut theta = 0.0;
                if let Some(quote) = self.grid.quotes.get(&OrderedFloat(leg.strike)) {
                    let leg_quote = if leg.side == "CALL" { &quote.call } else { &quote.put };
                    if let Some(lq) = leg_quote {
                        mid_price = lq.mid;
                        delta = lq.delta;
                        theta = lq.theta;
                    }
                }
                if mid_price == 0.0 {
                    mid_price = 0.05;
                }

                let mut waiting = Vec::new();
                for (sid, s) in strats.iter() {
                    if let Some(prev) = &s.previous_portfolio {
                        let port = s.portfolio.lock().await;
                        let target_qty = port.position_qty_for(symbol);
                        let current_qty = prev.position_qty_for(symbol);
                        let diff = target_qty - current_qty;
                        if diff != 0 && (diff > 0) == is_buy {
                            waiting.push((sid.clone(), diff.abs(), s.last_update_ts.unwrap_or(now_ct)));
                        }
                    }
                }

                waiting.sort_by_key(|&(_, _, ts)| ts);

                for (sid, needed_qty, _) in waiting {
                    if remaining_fill <= 0 {
                        break;
                    }
                    let alloc = needed_qty.min(remaining_fill);
                    let alloc_qty = alloc * sign;

                    if let Some(s) = strats.get_mut(&sid) {
                        if let Some(ref mut prev_port) = s.previous_portfolio {
                            let fill_trade = Trade {
                                timestamp: trade.timestamp.clone(),
                                legs: vec![OptionLeg {
                                    symbol: symbol.clone(),
                                    strike: leg.strike,
                                    side: leg.side.clone(),
                                    quantity: alloc_qty,
                                    delta,
                                    theta,
                                    price: mid_price,
                                    instruction: leg.instruction.clone(),
                                }],
                                credit: -(alloc_qty as f64) * mid_price * 100.0,
                                commission: 0.0,
                                purpose: "BROKER_FILL".to_string(),
                                strategy_id: trade.strategy_id.clone(),
                            };
                            prev_port.add_trade(&fill_trade, Some(vec![mid_price]));
                        }
                    }
                    remaining_fill -= alloc;
                }
            }
        }

        // Finalize any sub-strategies that are now fully filled
        for s in strats.values_mut() {
            if s.previous_portfolio.is_some() {
                s.check_and_finalize_fill().await;
            }
        }
    }
}
