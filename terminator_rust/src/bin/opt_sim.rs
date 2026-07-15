use std::collections::HashMap;
use std::time::Instant;
use anyhow::{Context, Result};
use chrono::{Datelike, NaiveTime, Timelike, TimeZone};
use chrono_tz::America::Chicago;
use chrono_tz::Tz;
use ordered_float::OrderedFloat;
use serde::{Deserialize, Serialize};

use terminator_rust::{
    db::{estimate_spx_from_snapshot, load_historical_snapshots},
    grid::{OptionLegQuote, OptionQuote, OptionsGrid},
    portfolio::Portfolio,
    strategy::{
        check_entry, check_rebalance, SubStrategy, StrategyState, Trade,
    },
    config::AppConfig,
};

#[derive(Debug, Deserialize, Clone)]
struct OptParamSet {
    hash: String,
    init_wing_delta: f64,
    initial_sum_delta: f64,
    rebalance_threshold: f64,
    long_leg_rebalance_delta_threshold: f64,
    min_credit: f64,
    min_long_delta: f64,
}

#[derive(Serialize)]
struct OptResult {
    param_hash: String,
    date: String,
    pnl: f64,
    execution_trades: usize,
    fees: f64,
    success: bool,
}

fn inject_snapshot(
    grid: &OptionsGrid,
    quotes: &[terminator_rust::db::OptionQuoteRow],
    spx: f64,
) {
    for q in quotes {
        let strike_key = OrderedFloat(q.strike);
        let mut entry = grid.quotes.entry(strike_key).or_insert_with(|| OptionQuote {
            strike: q.strike,
            call: None,
            put: None,
            last_updated: Instant::now(),
        });

        let leg = OptionLegQuote {
            symbol: q.symbol.clone(),
            bid: q.bid,
            ask: q.ask,
            mid: (q.bid + q.ask) / 2.0,
            delta: q.delta,
            theta: q.theta,
            last_update: Instant::now(),
        };

        if q.side == "CALL" {
            entry.call = Some(leg);
        } else {
            entry.put = Some(leg);
        }
    }
    grid.set_underlying_price(spx);
}

fn parse_datetime(dt_str: &str, tz: Tz) -> Result<chrono::DateTime<Tz>> {
    if let Ok(dt) = chrono::DateTime::parse_from_rfc3339(dt_str) {
        return Ok(dt.with_timezone(&tz));
    }
    if let Ok(naive) = chrono::NaiveDateTime::parse_from_str(dt_str, "%Y-%m-%dT%H:%M:%S.%f") {
        if let chrono::LocalResult::Single(dt) = tz.from_local_datetime(&naive) {
            return Ok(dt);
        }
    }
    if let Ok(naive) = chrono::NaiveDateTime::parse_from_str(dt_str, "%Y-%m-%dT%H:%M:%S") {
        if let chrono::LocalResult::Single(dt) = tz.from_local_datetime(&naive) {
            return Ok(dt);
        }
    }
    anyhow::bail!("Failed to parse datetime: {}", dt_str)
}

fn main() -> Result<()> {
    // 1. Manual parsing of command line arguments
    let args: Vec<String> = std::env::args().collect();
    let mut db_path = String::new();
    let mut date_str = String::new();
    let mut params_file = String::new();

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--db-path" => {
                if i + 1 < args.len() {
                    db_path = args[i + 1].clone();
                    i += 2;
                } else {
                    anyhow::bail!("Missing value for --db-path");
                }
            }
            "--date" => {
                if i + 1 < args.len() {
                    date_str = args[i + 1].clone();
                    i += 2;
                } else {
                    anyhow::bail!("Missing value for --date");
                }
            }
            "--params-file" => {
                if i + 1 < args.len() {
                    params_file = args[i + 1].clone();
                    i += 2;
                } else {
                    anyhow::bail!("Missing value for --params-file");
                }
            }
            _ => {
                anyhow::bail!("Unknown argument: {}", args[i]);
            }
        }
    }

    if db_path.is_empty() || date_str.is_empty() || params_file.is_empty() {
        anyhow::bail!("Usage: opt_sim --db-path <path> --date <YYYY-MM-DD> --params-file <path>");
    }

    // 2. Load parameters JSON
    let params_content = std::fs::read_to_string(&params_file)
        .with_context(|| format!("Failed to read params file: {}", params_file))?;
    let param_sets: Vec<OptParamSet> = serde_json::from_str(&params_content)
        .context("Failed to parse params file JSON")?;

    // 3. Setup dates and timezones
    let tz = Chicago;
    // Parse date YYYY-MM-DD
    let parts: Vec<&str> = date_str.split('-').collect();
    if parts.len() != 3 {
        anyhow::bail!("Invalid date format: {}", date_str);
    }
    let year: i32 = parts[0].parse()?;
    let month: u32 = parts[1].parse()?;
    let day: u32 = parts[2].parse()?;

    let start_ct = tz.with_ymd_and_hms(year, month, day, 8, 30, 0).unwrap();
    let end_ct = tz.with_ymd_and_hms(year, month, day, 15, 0, 0).unwrap();

    let start_str = start_ct.to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
    let end_str = end_ct.to_rfc3339_opts(chrono::SecondsFormat::Millis, true);

    // 4. Load options snapshots once into RAM
    let snapshots = load_historical_snapshots(&db_path, &start_str, &end_str)
        .with_context(|| format!("Cannot load snapshots from {}", db_path))?;

    if snapshots.is_empty() {
        // Print failure/empty JSON lines for all parameter sets
        for param in &param_sets {
            let res = OptResult {
                param_hash: param.hash.clone(),
                date: date_str.clone(),
                pnl: 0.0,
                execution_trades: 0,
                fees: 0.0,
                success: false,
            };
            println!("{}", serde_json::to_string(&res)?);
        }
        return Ok(());
    }

    // 5. Evaluate all parameter sets sequentially on cached snapshots
    for param in &param_sets {
        // Construct AppConfig for rebalancing logic
        let mut app_cfg = AppConfig::default();
        app_cfg.initial_sum_delta = param.initial_sum_delta;
        app_cfg.init_wing_delta = param.init_wing_delta;
        app_cfg.rebalance_threshold = param.rebalance_threshold;
        app_cfg.long_leg_rebalance_delta_threshold = param.long_leg_rebalance_delta_threshold;
        app_cfg.min_credit = param.min_credit;
        app_cfg.min_long_delta = param.min_long_delta;
        app_cfg.max_spread_diff = 50.0;
        app_cfg.commission_per_contract = 1.13;
        app_cfg.default_unit_size = 1;
        app_cfg.stale_guard_min_price = 0.20;
        app_cfg.start_time = NaiveTime::from_hms_opt(8, 30, 0).unwrap();
        app_cfg.end_time = NaiveTime::from_hms_opt(15, 0, 0).unwrap();

        // Build sub-strategies
        let portfolio_start = NaiveTime::from_hms_opt(8, 35, 0).unwrap();
        let portfolio_end = NaiveTime::from_hms_opt(14, 0, 0).unwrap();
        let portfolio_interval_minutes = 5;

        let init_s = param.initial_sum_delta / 2.0;
        let init_l = (init_s - param.init_wing_delta).max(param.min_long_delta);

        let mut sub_strats: Vec<(SubStrategy, Portfolio, StrategyState)> = {
            let mut v = Vec::new();
            let mut t = portfolio_start;
            while t <= portfolio_end {
                let sid = format!("strat_{}", t.format("%H%M"));
                let s = SubStrategy::new(sid, t, init_s, init_l, 1);
                v.push((s, Portfolio::new(), StrategyState::Idle));
                t += chrono::Duration::minutes(portfolio_interval_minutes);
            }
            v
        };

        let grid = OptionsGrid::new(HashMap::new());
        let mut final_spx = 0.0;

        // Run day simulation
        for snap in &snapshots {
            let snap_ct = parse_datetime(&snap.datetime, tz)
                .with_context(|| format!("Bad datetime: {}", snap.datetime))?;

            let spx = estimate_spx_from_snapshot(&snap.quotes).unwrap_or_else(|| grid.get_spx());
            inject_snapshot(&grid, &snap.quotes, spx);
            final_spx = spx;

            for (s, port, state) in &mut sub_strats {
                if snap_ct.time() < s.trade_start_time {
                    continue;
                }

                if matches!(state, StrategyState::Idle) {
                    if let Some(trade) = check_entry(&grid, s, snap_ct, app_cfg.max_spread_diff, app_cfg.commission_per_contract, None) {
                        port.add_trade(&trade, None);
                        *state = StrategyState::Working;
                    }
                } else if matches!(state, StrategyState::Working) {
                    port.update_pricing(&grid);

                    let end_dt = tz
                        .with_ymd_and_hms(
                            snap_ct.year(), snap_ct.month(), snap_ct.day(),
                            15, 0, 0,
                        )
                        .unwrap();

                    if snap_ct < end_dt {
                        let rebal_trades = check_rebalance(
                            &grid, s, port, snap_ct, app_cfg.start_time, app_cfg.end_time, &app_cfg,
                        );
                        for trade in rebal_trades {
                            port.add_trade(&trade, None);
                        }
                    }
                }
            }
        }

        // Apply 15:00 Expiration Rule
        let mut total_pnl = 0.0;
        let mut total_trades_count = 0;
        let mut total_fees = 0.0;

        for (_, port, state) in &mut sub_strats {
            if matches!(state, StrategyState::Working) {
                // Settle all positions at intrinsic values based on final SPX price
                for pos in &mut port.positions {
                    let intrinsic = if pos.side == "CALL" {
                        (final_spx - pos.strike).max(0.0)
                    } else {
                        (pos.strike - final_spx).max(0.0)
                    };
                    pos.price = intrinsic;
                }
            }
            // Add up performance metrics
            total_pnl += port.gross_pnl();
            total_trades_count += port.trades.len();
            total_fees += port.fees();
        }

        let result = OptResult {
            param_hash: param.hash.clone(),
            date: date_str.clone(),
            pnl: total_pnl,
            execution_trades: total_trades_count,
            fees: total_fees,
            success: true,
        };

        println!("{}", serde_json::to_string(&result)?);
    }

    Ok(())
}
