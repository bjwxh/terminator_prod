use std::time::Duration;
use std::sync::Arc;
use std::collections::HashMap;
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
pub struct PendingTrade { pub strat_id: String, pub trade: Trade }
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
    pub active_order_id: Option<String>,
    pub portfolio: Arc<Mutex<crate::portfolio::Portfolio>>,
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
            active_order_id: None,
            portfolio: Arc::new(Mutex::new(crate::portfolio::Portfolio::new())),
        }
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

        // Safety stale quote check (Stale Quote Guard: 500ms limit)
        if quote.last_updated.elapsed() > Duration::from_millis(500) {
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
            if leg.mid <= 0.0 || leg.last_update.elapsed() > Duration::from_millis(500) {
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
        },
        OptionLeg {
            symbol: lc.symbol.clone(),
            strike: parsed_lc,
            side: "CALL".to_string(),
            quantity: s.unit_size,
            delta: lc.delta,
            theta: lc.theta,
            price: lc.mid,
        },
        OptionLeg {
            symbol: sp.symbol.clone(),
            strike: parsed_sp,
            side: "PUT".to_string(),
            quantity: -s.unit_size,
            delta: sp.delta,
            theta: sp.theta,
            price: sp.mid,
        },
        OptionLeg {
            symbol: lp.symbol.clone(),
            strike: parsed_lp,
            side: "PUT".to_string(),
            quantity: s.unit_size,
            delta: lp.delta,
            theta: lp.theta,
            price: lp.mid,
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
        },
        OptionLeg {
            symbol: opt_l.symbol.clone(),
            strike: parsed_l,
            side: side_str.to_string(),
            quantity: s.unit_size,
            delta: opt_l.delta,
            theta: opt_l.theta,
            price: opt_l.mid,
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
        },
        OptionLeg {
            symbol: new_short.symbol.clone(),
            strike: parsed_new_short,
            side: side_str.to_string(),
            quantity: old_short.quantity,
            delta: new_short.delta,
            theta: new_short.theta,
            price: new_short.mid,
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
            });
            legs.push(OptionLeg {
                symbol: new_long.symbol.clone(),
                strike: parsed_new_long,
                side: side_str.to_string(),
                quantity: ol.quantity,
                delta: new_long.delta,
                theta: new_long.theta,
                price: new_long.mid,
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
        },
        OptionLeg {
            symbol: new_long.symbol.clone(),
            strike: parsed_new_long,
            side: side_str.to_string(),
            quantity: old_long.quantity,
            delta: new_long.delta,
            theta: new_long.theta,
            price: new_long.mid,
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
    spx_price: f64,
) -> Option<Trade> {
    if portfolio.positions.is_empty() {
        return None;
    }

    let mut legs = Vec::new();
    for p in &portfolio.positions {
        let is_itm = (p.side == "CALL" && spx_price > p.strike)
                  || (p.side == "PUT"  && spx_price < p.strike);

        let mut exit_price = 0.0;
        if is_itm {
            if let Some(quote) = grid.quotes.get(&ordered_float::OrderedFloat(p.strike)) {
                let lq = if p.side == "CALL" { &quote.call } else { &quote.put };
                if let Some(leg_quote) = lq {
                    exit_price = leg_quote.mid;
                } else {
                    exit_price = p.price;
                }
            } else {
                exit_price = p.price;
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
        });
    }

    let credit = legs.iter().map(|l| -(l.quantity as f64) * l.price).sum::<f64>() * 100.0;
    let base_unit = portfolio.positions.first().map(|p| p.quantity.abs()).unwrap_or(1) as f64;
    let commission = commission_per_contract * legs.len() as f64 * base_unit;

    Some(Trade {
        timestamp: now.to_rfc3339(),
        legs,
        credit,
        commission,
        purpose: "EXIT".to_string(),
        strategy_id: strategy_id.to_string(),
    })
}

/// Place the generated trade to Schwab API
pub async fn execute_trade(
    client: &ExecutionClient,
    account_hash: &str,
    trade: &Trade,
    dry_run: bool,
    order_offset: f64,
    portfolio: &crate::portfolio::Portfolio,
) -> Result<Option<String>> {
    if dry_run {
        info!("[DRY RUN] Bypassing REST endpoint. Strategy {} would trade {} (credit: ${:.2})", trade.strategy_id, trade.purpose, trade.credit);
        return Ok(None);
    }

    let mut legs_collection = Vec::new();
    for leg in &trade.legs {
        // Check if this leg closes an existing position
        let is_closing = portfolio.positions.iter().any(|p| p.symbol == leg.symbol && (p.quantity as f64).signum() != (leg.quantity as f64).signum());

        let inst = match (leg.quantity > 0, is_closing) {
            (true,  true)  => "BUY_TO_CLOSE",
            (true,  false) => "BUY_TO_OPEN",
            (false, true)  => "SELL_TO_CLOSE",
            (false, false) => "SELL_TO_OPEN",
        };

        legs_collection.push(json!({
            "instruction": inst,
            "quantity": leg.quantity.abs(),
            "instrument": {
                "symbol": leg.symbol,
                "assetType": "OPTION"
            }
        }));
    }

    // Credit per unit with offset, rounded to $0.05 tick
    let unit_qty = trade.legs[0].quantity.abs() as f64;
    let raw_mid = (trade.credit / (unit_qty * 100.0)).abs();
    let with_offset = raw_mid + order_offset;
    let ticked = (with_offset / 0.05).round() * 0.05;
    let price_str = format!("{:.2}", ticked);

    let order_type = if trade.credit >= 0.0 { "NET_CREDIT" } else { "NET_DEBIT" };

    let complex_type = match (trade.purpose.as_str(), legs_collection.len()) {
        ("IRON_CONDOR", _) => "IRON_CONDOR",
        ("EXIT", 4)        => "IRON_CONDOR",
        _                  => "VERTICAL",
    };

    let order_body = json!({
        "orderType": order_type,
        "session": "NORMAL",
        "duration": "DAY",
        "price": price_str,
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": complex_type,
        "quantity": trade.legs[0].quantity.abs(),
        "orderLegCollection": legs_collection
    });

    client.place_order(account_hash, order_body).await
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
    pub working_orders: tokio::sync::Mutex<Vec<serde_json::Value>>,
    pub pending_trade: tokio::sync::Mutex<Option<PendingTrade>>,
    pub server_name: String,
    pub status: tokio::sync::Mutex<String>,
    pub broker_connected: std::sync::atomic::AtomicBool,
    pub trading_enabled: std::sync::atomic::AtomicBool,
    pub heartbeat_failures: std::sync::atomic::AtomicUsize,
    pub timer_paused: std::sync::atomic::AtomicBool,
    pub session_history: tokio::sync::Mutex<std::collections::VecDeque<HistoryPoint>>,
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

            t += chrono::Duration::minutes(config.portfolio_interval_minutes as i64);
        }

        Self {
            config: config.clone(),
            execution_client,
            grid,
            sub_strategies: Mutex::new(sub_strategies),
            account_hash: Mutex::new(None),
            live_portfolio: Arc::new(Mutex::new(crate::portfolio::Portfolio::new())),
            working_orders: Mutex::new(Vec::new()),
            pending_trade: Mutex::new(None),
            server_name: config.server_name.clone(),
            status: Mutex::new("Initializing".to_string()),
            broker_connected: std::sync::atomic::AtomicBool::new(false),
            trading_enabled: std::sync::atomic::AtomicBool::new(true),
            heartbeat_failures: std::sync::atomic::AtomicUsize::new(0),
            timer_paused: std::sync::atomic::AtomicBool::new(false),
            session_history: Mutex::new(std::collections::VecDeque::new()),
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

        if now_ct < start_time {
            info!("Skipping bootstrap (before 8:30 AM)");
            return;
        }

        let mut live_trades = Vec::new();
        if mode == "soft" {
            match self.execution_client.get_today_filled_orders(account_hash).await {
                Ok(trades) => {
                    live_trades = trades;
                    info!("Fetched {} live filled trades for soft bootstrap matching", live_trades.len());
                }
                Err(e) => {
                    warn!("Failed to fetch live trades for soft bootstrap: {:?}", e);
                }
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
        }

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

        match crate::db::load_historical_snapshots(&self.config.db_path, &start_str, &end_str) {
            Ok(snapshots) => {
                info!("Loaded {} historical snapshots for replay", snapshots.len());
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
                                            }
                                        }
                                        continue;
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

                        // Add to session history
                        if snap_ct.timestamp() % 30 == 0 {
                            let sim_pnl = {
                                let mut total = 0.0;
                                for s in strats.values() {
                                    let port = s.portfolio.lock().await;
                                    total += port.net_pnl();
                                }
                                total
                            };
                            let live_pnl = {
                                let port = self.live_portfolio.lock().await;
                                port.net_pnl()
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
        let now_secs = now_ct.timestamp();
        if now_secs % 30 == 0 {
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

        let account_hash = {
            let h = self.account_hash.lock().await;
            h.clone().unwrap_or_default()
        };
        if account_hash.is_empty() {
            return Ok(());
        }

        let mut strats = self.sub_strategies.lock().await;
        for (sid, s) in strats.iter_mut() {
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

                    let enabled = self.trading_enabled.load(std::sync::atomic::Ordering::Relaxed);
                    let paused = self.timer_paused.load(std::sync::atomic::Ordering::Relaxed);
                    if !enabled || paused {
                        info!("Trading disabled or paused. Routing to pending_trade for user confirmation.");
                        *self.pending_trade.lock().await = Some(PendingTrade {
                            strat_id: sid.clone(),
                            trade: trade.clone(),
                        });
                        continue;
                    }

                    s.state = StrategyState::EnteringSpread;
                    let mut port = s.portfolio.lock().await;
                    match execute_trade(&self.execution_client, &account_hash, &trade, self.config.dry_run, self.config.order_offset, &*port).await {
                        Ok(order_id) => {
                            info!("✅ Order placed successfully for strategy {}. Order ID: {:?}", sid, order_id);
                            s.state = StrategyState::Working;
                            s.has_traded_today = true;
                            s.active_order_id = order_id.clone();
                            if let Some(ref oid) = order_id {
                                self.working_orders.lock().await.push(json!({"orderId": oid, "strategy_id": sid.clone()}));
                            }
                            port.add_trade(&trade, None);
                            self.live_portfolio.lock().await.add_trade(&trade, None);
                        }
                        Err(e) => {
                            error!("❌ Failed to place order for strategy {}: {:?}", sid, e);
                            s.state = StrategyState::Idle; // Revert to retry
                        }
                    }
                }
            } else if s.state == StrategyState::Working {
                let mut s_port = s.portfolio.lock().await;
                s_port.update_pricing(&self.grid);

                if current_time >= end_time {
                    // --- EXIT ---
                    if !s_port.positions.is_empty() {
                        let spx = self.grid.get_underlying_price();

                        if let Some(exit_trade) = check_exit(&self.grid, &*s_port, now_ct, &s.sid, self.config.commission_per_contract, spx) {
                            let enabled = self.trading_enabled.load(std::sync::atomic::Ordering::Relaxed);
                            let paused = self.timer_paused.load(std::sync::atomic::Ordering::Relaxed);
                            if !enabled || paused {
                                info!("Trading disabled or paused. Routing to pending_trade for user confirmation.");
                                *self.pending_trade.lock().await = Some(PendingTrade {
                                    strat_id: sid.clone(),
                                    trade: exit_trade.clone(),
                                });
                                continue;
                            }
                            s.state = StrategyState::Exiting;
                            if let Ok(order_id) = execute_trade(&self.execution_client, &account_hash, &exit_trade, self.config.dry_run, self.config.order_offset, &*s_port).await {
                                if let Some(ref oid) = order_id {
                                    self.working_orders.lock().await.push(json!({"orderId": oid, "strategy_id": sid.clone()}));
                                }
                                s_port.add_trade(&exit_trade, None);
                                self.live_portfolio.lock().await.add_trade(&exit_trade, None);
                            } else {
                                s.state = StrategyState::Working; // Retry next tick
                            }
                        }
                    }
                } else {
                    // --- REBALANCE ---
                    let rebal_trades = check_rebalance(&self.grid, s, &*s_port, now_ct, start_time, end_time, &self.config);

                    for trade in rebal_trades {
                        let enabled = self.trading_enabled.load(std::sync::atomic::Ordering::Relaxed);
                        let paused = self.timer_paused.load(std::sync::atomic::Ordering::Relaxed);
                        if !enabled || paused {
                            info!("Trading disabled or paused. Routing to pending_trade for user confirmation.");
                            *self.pending_trade.lock().await = Some(PendingTrade {
                                strat_id: sid.clone(),
                                trade: trade.clone(),
                            });
                            continue;
                        }
                        if let Ok(order_id) = execute_trade(&self.execution_client, &account_hash, &trade, self.config.dry_run, self.config.order_offset, &*s_port).await {
                            if let Some(ref oid) = order_id {
                                self.working_orders.lock().await.push(json!({"orderId": oid, "strategy_id": sid.clone()}));
                            }
                            s_port.add_trade(&trade, None);
                            self.live_portfolio.lock().await.add_trade(&trade, None);
                        }
                    }
                }
            }
        }

        Ok(())
    }

    /// Process parsed order events from the ACCT_ACTIVITY feed.
    pub async fn process_account_event(&self, event: crate::parser::OrderActivityEvent) {
        info!("💼 Strategy Supervisor processing event: Order ID = {}, Type = {}, Status = {}, Legs count = {}",
            event.order_id, event.message_type, event.status, event.legs.len());

        let mut strats = self.sub_strategies.lock().await;
        for (sid, s) in strats.iter_mut() {
            let is_our_order = s.active_order_id.as_deref() == Some(&event.order_id);
            if !is_our_order {
                continue;
            }
            if event.status == "Filled" || event.status == "Cancelled" || event.status == "Rejected" || event.status == "Canceled" {
                let mut wo = self.working_orders.lock().await;
                wo.retain(|o| o.get("orderId").and_then(|v| v.as_str()) != Some(&event.order_id));
            }
            if event.status == "Filled" {
                info!("🎉 Strategy {} order fully filled!", sid);
                s.state = StrategyState::Working;
            } else if event.status == "Cancelled" {
                warn!("⚠️ Strategy {} order was cancelled.", sid);
                s.state = StrategyState::Idle;
                s.has_traded_today = false;
                s.active_order_id = None;
            }
        }
    }

    pub async fn confirm_trade(&self, strat_id: &str, overrides: Vec<LegOverride>) -> Result<()> {
        let pending = { self.pending_trade.lock().await.take() };
        if let Some(mut t) = pending {
            let fill_prices: Vec<f64> = t.trade.legs.iter().enumerate()
                .map(|(i, leg)| {
                    overrides.iter()
                        .find(|o| o.idx == i)
                        .map(|o| o.price_ea)
                        .unwrap_or(leg.price)
                })
                .collect();

            // Apply overrides to leg prices and recalculate credit
            for (i, leg) in t.trade.legs.iter_mut().enumerate() {
                leg.price = fill_prices[i];
            }
            t.trade.credit = t.trade.legs.iter().map(|l| -(l.quantity as f64) * l.price).sum::<f64>() * 100.0;

            let account_hash = self.account_hash.lock().await.clone().unwrap_or_default();

            let mut strats = self.sub_strategies.lock().await;
            if let Some(s) = strats.get_mut(strat_id) {
                let mut s_port = s.portfolio.lock().await;
                if let Ok(order_id) = execute_trade(
                    &self.execution_client, &account_hash, &t.trade,
                    self.config.dry_run, self.config.order_offset, &*s_port
                ).await {
                    if let Some(ref oid) = order_id {
                        self.working_orders.lock().await.push(json!({"orderId": oid, "strategy_id": t.strat_id}));
                    }
                    s_port.add_trade(&t.trade, None);
                    self.live_portfolio.lock().await.add_trade(&t.trade, None);

                    s.state = StrategyState::Working;
                    s.has_traded_today = true;
                    s.active_order_id = order_id;
                }
            }
        }
        Ok(())
    }

    pub async fn dismiss_trade(&self, _strat_id: &str) {
        let _ = self.pending_trade.lock().await.take();
    }

    pub fn set_timer_paused(&self, is_paused: bool) {
        self.timer_paused.store(is_paused, std::sync::atomic::Ordering::Relaxed);
    }

    pub fn toggle_trading_enabled(&self) -> bool {
        let current = self.trading_enabled.load(std::sync::atomic::Ordering::Relaxed);
        self.trading_enabled.store(!current, std::sync::atomic::Ordering::Relaxed);
        !current
    }
}
