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
    let mut closest_leg: Option<OptionLegQuote> = None;
    let mut min_delta_diff = f64::MAX;

    for entry in grid.quotes.iter() {
        let strike = entry.key().0;
        let quote = entry.value();

        // Safety stale quote check (Stale Quote Guard: 500ms limit)
        if quote.last_updated.elapsed() > Duration::from_millis(500) {
            continue;
        }

        if let Some(short) = short_strike {
            if let Some(diff) = max_diff {
                if is_call {
                    if strike < short || (strike - short) > diff {
                        continue;
                    }
                } else {
                    if strike > short || (short - strike) > diff {
                        continue;
                    }
                }
            }
        }

        let leg_quote_opt = if is_call { &quote.call } else { &quote.put };
        if let Some(leg) = leg_quote_opt {
            if leg.mid <= 0.0 || leg.last_update.elapsed() > Duration::from_millis(500) {
                continue;
            }
            let diff = (leg.delta.abs() - target_delta.abs()).abs();
            if diff < min_delta_diff {
                min_delta_diff = diff;
                closest_leg = Some(leg.clone());
            }
        }
    }

    closest_leg
}

/// Core Iron Condor check entry logic
pub fn check_entry(
    grid: &OptionsGrid,
    s: &SubStrategy,
    now: chrono::DateTime<Tz>,
    max_diff: f64,
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
    let commission = 1.13 * legs.len() as f64 * s.unit_size as f64;

    Some(Trade {
        timestamp: now.to_rfc3339(),
        legs,
        credit,
        commission,
        purpose: "IRON_CONDOR".to_string(),
        strategy_id: s.sid.clone(),
    })
}

/// Place the generated trade to Schwab API
pub async fn execute_trade(
    client: &ExecutionClient,
    account_hash: &str,
    trade: &Trade,
    dry_run: bool,
) -> Result<Option<String>> {
    if dry_run {
        info!("[DRY RUN] Bypassing REST endpoint. Strategy {} would trade Iron Condor (credit: ${:.2})", trade.strategy_id, trade.credit);
        return Ok(None);
    }

    let mut legs_collection = Vec::new();
    for leg in &trade.legs {
        let inst = if leg.quantity > 0 { "BUY_TO_OPEN" } else { "SELL_TO_OPEN" };
        legs_collection.push(json!({
            "instruction": inst,
            "quantity": leg.quantity.abs(),
            "instrument": {
                "symbol": leg.symbol,
                "assetType": "OPTION"
            }
        }));
    }

    // Absolute value of total credit divided by 100, formatted to 2 decimals
    let price_str = format!("{:.2}", (trade.credit / (trade.legs[0].quantity.abs() as f64 * 100.0)).abs());

    let order_body = json!({
        "orderType": "NET_CREDIT",
        "session": "NORMAL",
        "duration": "DAY",
        "price": price_str,
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "IRON_CONDOR",
        "quantity": trade.legs[0].quantity.abs(),
        "orderLegCollection": legs_collection
    });

    client.place_order(account_hash, order_body).await
}


// ============================================================================
// Strategy Supervisor & Execution State Machine
// ============================================================================

pub struct StrategySupervisor {
    config: crate::config::AppConfig,
    execution_client: Arc<ExecutionClient>,
    grid: Arc<OptionsGrid>,
    pub sub_strategies: Mutex<HashMap<String, SubStrategy>>,
    pub account_hash: Mutex<Option<String>>,
}

impl StrategySupervisor {
    pub fn new(
        config: crate::config::AppConfig,
        execution_client: Arc<ExecutionClient>,
        grid: Arc<OptionsGrid>,
    ) -> Self {
        let mut sub_strategies = HashMap::new();
        let times = vec![
            NaiveTime::from_hms_opt(9, 1, 0).unwrap(),
            NaiveTime::from_hms_opt(9, 31, 0).unwrap(),
            NaiveTime::from_hms_opt(10, 1, 0).unwrap(),
            NaiveTime::from_hms_opt(10, 31, 0).unwrap(),
        ];
        
        for t in times {
            let sid = format!("strat_{}", t.format("%H%M"));
            // Target short delta: 0.175 (0.35 / 2)
            // Target long delta: max(0.175 - 0.16, 0.025) = 0.025
            let s = SubStrategy::new(sid.clone(), t, 0.175, 0.025, 1);
            sub_strategies.insert(sid, s);
        }

        Self {
            config,
            execution_client,
            grid,
            sub_strategies: Mutex::new(sub_strategies),
            account_hash: Mutex::new(None),
        }
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

        loop {
            if let Err(e) = self.tick().await {
                error!("Error in Strategy Supervisor tick: {:?}", e);
            }
            tokio::time::sleep(Duration::from_secs(5)).await;
        }
    }

    /// Reconcile startup positions. If any open positions are found, conservatively
    /// treat all sub-strategies as already traded today to prevent duplicate entries.
    pub async fn reconcile_startup_positions(&self, positions: &[crate::execution::BrokerPosition]) {
        if !positions.is_empty() {
            warn!("Found {} existing open options positions at startup. Marking affected strategies as already traded.", positions.len());
            let mut strats = self.sub_strategies.lock().await;
            for (sid, s) in strats.iter_mut() {
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

        // Standard regular trading hours: 8:30 AM to 3:00 PM Chicago Time
        let start_time = NaiveTime::from_hms_opt(8, 30, 0).unwrap();
        let end_time = NaiveTime::from_hms_opt(15, 0, 0).unwrap();

        if current_time < start_time || current_time > end_time {
            return Ok(());
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
                info!("🔔 Sub-strategy {} start time reached ({}). Checking entry...", sid, s.trade_start_time);
                if let Some(trade) = check_entry(&self.grid, s, now_ct, 50.0) {
                    info!("🎯 Entry signal triggered for {}! Net credit: ${:.2}. Executing...", sid, trade.credit);
                    
                    s.state = StrategyState::EnteringSpread;
                    match execute_trade(&self.execution_client, &account_hash, &trade, self.config.dry_run).await {
                        Ok(order_id) => {
                            info!("✅ Order placed successfully for strategy {}. Order ID: {:?}", sid, order_id);
                            s.state = StrategyState::Working;
                            s.has_traded_today = true;
                            s.active_order_id = order_id;
                        }
                        Err(e) => {
                            error!("❌ Failed to place order for strategy {}: {:?}", sid, e);
                            s.state = StrategyState::Idle; // Revert to retry
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
        // Search if this belongs to a working sub-strategy that is entering/working, and transition state.
        for (sid, s) in strats.iter_mut() {
            let is_our_order = s.active_order_id.as_deref() == Some(&event.order_id);
            if !is_our_order {
                continue;
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
}
