/// Standalone backtest that mirrors the bootstrap logic in strategy.rs.
///
/// Reads today's SQLite options DB, runs all sub-strategies from 08:35 to current time,
/// and prints a detailed trade + PnL report — useful for verifying bootstrap correctness.
///
/// Run from terminator_rust/:
///   CONFIG_PATH=config.json cargo run --bin backtest_today

use std::collections::HashMap;
use std::time::Instant;

use anyhow::{Context, Result};
use chrono::{Datelike, NaiveTime, Timelike, TimeZone};
use chrono_tz::America::Chicago;
use ordered_float::OrderedFloat;
use serde::Deserialize;

use terminator_rust::{
    db::{estimate_spx_from_snapshot, load_historical_snapshots},
    grid::{OptionLegQuote, OptionQuote, OptionsGrid},
    portfolio::Portfolio,
    strategy::{
        check_entry, check_exit, check_rebalance, SubStrategy, StrategyState, Trade,
    },
};

// ── Minimal config subset (avoids needing Schwab env vars) ────────────────────

#[derive(Debug, Deserialize)]
struct ConfigJson {
    initial_sum_delta: f64,
    init_wing_delta: f64,
    rebalance_threshold: f64,
    long_leg_rebalance_delta_threshold: f64,
    min_long_delta: f64,
    max_spread_diff: f64,
    commission_per_contract: f64,
    default_unit_size: i32,
    db_path: String,
    portfolio_start_time: String,
    portfolio_end_time: String,
    portfolio_interval_minutes: u32,
    start_time: String,
    end_time: String,
    stale_guard_min_price: f64,
    order_offset: f64,
    order_auto_execute_timeout: u32,
    bootstrap_mode: String,
    account_id: String,
    server_name: Option<String>,
    otm_offset: f64,
    buffer_zone: f64,
    dry_run: bool,
    web_port: u16,
    min_credit: f64,
    stale_quote_threshold_secs: Option<u64>,
}

// ── Helpers ───────────────────────────────────────────────────────────────────

fn inject_snapshot(
    grid: &OptionsGrid,
    quotes: &[terminator_rust::db::OptionQuoteRow],
    spx: f64,
) {
    // Clear stale entries by overwriting; grid uses DashMap
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

fn format_legs(trade: &Trade) -> String {
    trade
        .legs
        .iter()
        .map(|l| {
            let action = if l.quantity > 0 { "BUY" } else { "SELL" };
            format!("{} {} {} @{:.2}(Δ{:.3})", action, l.strike, l.side, l.price, l.delta)
        })
        .collect::<Vec<_>>()
        .join("  |  ")
}

// ── Main ──────────────────────────────────────────────────────────────────────

fn main() -> Result<()> {
    let config_path = std::env::var("CONFIG_PATH").unwrap_or_else(|_| "config.json".to_string());
    let raw = std::fs::read_to_string(&config_path)
        .with_context(|| format!("Cannot read {}", config_path))?;
    let cfg: ConfigJson = serde_json::from_str(&raw).context("Bad config.json")?;

    let tz = Chicago;
    let now_ct = if let Ok(val) = std::env::var("BACKTEST_END") {
        chrono::DateTime::parse_from_rfc3339(&val)
            .expect("Failed to parse BACKTEST_END as RFC3339")
            .with_timezone(&tz)
    } else {
        let now_utc = chrono::Utc::now();
        now_utc.with_timezone(&tz)
    };

    let date_str = now_ct.format("%Y%m%d").to_string();
    let db_path = cfg.db_path.replace("{date}", &date_str);

    let start_ct = tz
        .with_ymd_and_hms(now_ct.year(), now_ct.month(), now_ct.day(), 8, 30, 0)
        .unwrap();
    let start_str = start_ct.to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
    let end_str = now_ct.to_rfc3339_opts(chrono::SecondsFormat::Millis, true);

    let portfolio_start = NaiveTime::parse_from_str(&cfg.portfolio_start_time, "%H:%M:%S")?;
    let portfolio_end = NaiveTime::parse_from_str(&cfg.portfolio_end_time, "%H:%M:%S")?;
    let trading_end = NaiveTime::parse_from_str(&cfg.end_time, "%H:%M:%S")?;
    let trading_start = NaiveTime::parse_from_str(&cfg.start_time, "%H:%M:%S")?;

    // Build sub-strategies
    let init_s = cfg.initial_sum_delta / 2.0;
    let init_l = (init_s - cfg.init_wing_delta).max(cfg.min_long_delta);
    let mut sub_strats: Vec<(SubStrategy, Portfolio, StrategyState)> = {
        let mut v = Vec::new();
        let mut t = portfolio_start;
        while t <= portfolio_end {
            let sid = format!("strat_{}", t.format("%H%M"));
            let s = SubStrategy::new(sid, t, init_s, init_l, cfg.default_unit_size);
            v.push((s, Portfolio::new(), StrategyState::Idle));
            t += chrono::Duration::minutes(cfg.portfolio_interval_minutes as i64);
        }
        v
    };

    // Minimal AppConfig for passing to check_rebalance
    let app_cfg = terminator_rust::config::AppConfig {
        schwab_token_path: std::path::PathBuf::new(),
        schwab_account: String::new(),
        schwab_api_key: String::new(),
        schwab_api_secret: String::new(),
        schwab_callback_url: String::new(),
        dry_run: cfg.dry_run,
        initial_sum_delta: cfg.initial_sum_delta,
        init_wing_delta: cfg.init_wing_delta,
        rebalance_threshold: cfg.rebalance_threshold,
        long_leg_rebalance_delta_threshold: cfg.long_leg_rebalance_delta_threshold,
        min_credit: cfg.min_credit,
        max_spread_diff: cfg.max_spread_diff,
        commission_per_contract: cfg.commission_per_contract,
        order_offset: cfg.order_offset,
        stale_guard_min_price: cfg.stale_guard_min_price,
        default_unit_size: cfg.default_unit_size,
        account_id: cfg.account_id.clone(),
        server_name: cfg.server_name.unwrap_or_default(),
        min_long_delta: cfg.min_long_delta,
        order_auto_execute_timeout: cfg.order_auto_execute_timeout,
        bootstrap_mode: cfg.bootstrap_mode.clone(),
        db_path: cfg.db_path.clone(),
        portfolio_start_time: portfolio_start,
        portfolio_end_time: portfolio_end,
        portfolio_interval_minutes: cfg.portfolio_interval_minutes,
        start_time: trading_start,
        end_time: trading_end,
        otm_offset: cfg.otm_offset,
        buffer_zone: cfg.buffer_zone,
        web_port: cfg.web_port,
        email_alert_delay_seconds: 5,
        stale_quote_threshold_secs: cfg.stale_quote_threshold_secs.unwrap_or(60),
    };

    println!("================================================================================");
    println!("  Backtest: {} → {}", start_str, end_str);
    println!("  DB: {}", db_path);
    println!("  Sub-strategies: {}", sub_strats.len());
    println!("================================================================================\n");

    let snapshots = load_historical_snapshots(&db_path, &start_str, &end_str)
        .with_context(|| format!("Cannot load snapshots from {}", db_path))?;
    println!("Loaded {} snapshots from DB.\n", snapshots.len());

    let grid = OptionsGrid::new(HashMap::new());

    // Trade event log (for summary)
    let mut all_trades: Vec<(String, String, Trade)> = Vec::new(); // (sid, event_type, trade)

    for snap in &snapshots {
        let snap_ts = chrono::DateTime::parse_from_rfc3339(&snap.datetime)
            .with_context(|| format!("Bad datetime: {}", snap.datetime))?;
        let snap_ct = snap_ts.with_timezone(&tz);

        let spx = estimate_spx_from_snapshot(&snap.quotes)
            .unwrap_or_else(|| grid.get_spx());
        inject_snapshot(&grid, &snap.quotes, spx);

        for (s, port, state) in &mut sub_strats {
            if snap_ct.time() < s.trade_start_time {
                continue;
            }

            if matches!(state, StrategyState::Idle) {
                if let Some(trade) =
                    check_entry(&grid, s, snap_ct, cfg.max_spread_diff, cfg.commission_per_contract, None, trading_start, trading_end)
                {
                    println!(
                        "[{}] {} ENTRY  credit=${:.2}  {}",
                        snap_ct.format("%H:%M:%S"),
                        s.sid,
                        trade.credit,
                        format_legs(&trade)
                    );
                    all_trades.push((s.sid.clone(), "ENTRY".to_string(), trade.clone()));
                    port.add_trade(&trade, None);
                    *state = StrategyState::Working;
                }
            } else if matches!(state, StrategyState::Working) {
                port.update_pricing(&grid);

                let end_dt = tz
                    .with_ymd_and_hms(
                        snap_ct.year(), snap_ct.month(), snap_ct.day(),
                        trading_end.hour(), trading_end.minute(), trading_end.second(),
                    )
                    .unwrap();

                if snap_ct >= end_dt {
                    if !port.positions.is_empty() {
                        if let Some(exit_trade) =
                            check_exit(&grid, port, snap_ct, &s.sid, cfg.commission_per_contract, spx)
                        {
                            println!(
                                "[{}] {} EXIT   credit=${:.2}  {}",
                                snap_ct.format("%H:%M:%S"),
                                s.sid,
                                exit_trade.credit,
                                format_legs(&exit_trade)
                            );
                            all_trades.push((s.sid.clone(), "EXIT".to_string(), exit_trade.clone()));
                            port.add_trade(&exit_trade, None);
                            *state = StrategyState::Exiting;
                        }
                    }
                } else {
                    let rebal_trades = check_rebalance(
                        &grid, s, port, snap_ct, trading_start, trading_end, &app_cfg,
                    );
                    for trade in rebal_trades {
                        println!(
                            "[{}] {} REBAL  credit=${:.2}  {}",
                            snap_ct.format("%H:%M:%S"),
                            s.sid,
                            trade.credit,
                            format_legs(&trade)
                        );
                        all_trades.push((s.sid.clone(), "REBAL".to_string(), trade.clone()));
                        port.add_trade(&trade, None);
                    }
                }
            }
        }
    }

    // Final pricing update
    for (_, port, _) in &mut sub_strats {
        port.update_pricing(&grid);
    }

    // ── Summary ───────────────────────────────────────────────────────────────
    println!("\n================================================================================");
    println!("  FINAL PnL SUMMARY  (SPX ≈ {:.2})", grid.get_spx());
    println!("================================================================================");
    println!(
        "{:<16}  {:>7}  {:>10}  {:>10}  {:>10}  {:>8}  {}",
        "Strategy", "State", "Gross PnL", "Fees", "Net PnL", "Trades", "Positions"
    );
    println!("{}", "-".repeat(90));

    let mut total_gross = 0.0_f64;
    let mut total_fees = 0.0_f64;
    let mut total_net = 0.0_f64;
    let mut total_trades = 0;

    for (s, port, state) in &sub_strats {
        let gross = port.gross_pnl();
        let fees = port.fees();
        let net = port.net_pnl();
        let n_trades = port.trades.len();
        let pos_str = port
            .positions
            .iter()
            .map(|p| format!("{}{}×{}", if p.quantity < 0 { "-" } else { "+" }, p.side, p.strike))
            .collect::<Vec<_>>()
            .join(" ");

        total_gross += gross;
        total_fees += fees;
        total_net += net;
        total_trades += n_trades;

        let state_str = match state {
            StrategyState::Idle => "Idle",
            StrategyState::EnteringSpread => "Entering",
            StrategyState::Working => "Working",
            StrategyState::Exiting => "Exited",
        };

        println!(
            "{:<16}  {:>7}  {:>10.2}  {:>10.2}  {:>10.2}  {:>8}  {}",
            s.sid, state_str, gross, fees, net, n_trades,
            if pos_str.is_empty() { "(none)".to_string() } else { pos_str }
        );
    }

    println!("{}", "-".repeat(90));
    println!(
        "{:<16}  {:>7}  {:>10.2}  {:>10.2}  {:>10.2}  {:>8}",
        "TOTAL", "", total_gross, total_fees, total_net, total_trades
    );

    println!("\n--- Trade Log ({} events) ---", all_trades.len());
    for (sid, ev, t) in &all_trades {
        println!(
            "  {} {:8} {:6}  credit=${:8.2}  commission=${:.2}  {}",
            sid, ev, t.timestamp.get(..19).unwrap_or(&t.timestamp),
            t.credit, t.commission, format_legs(t)
        );
    }

    println!("\n================================================================================");
    println!("  Sim Total gross PnL : ${:.2}", total_gross);
    println!("  Sim Total fees       : ${:.2}", total_fees);
    println!("  Sim Total net PnL    : ${:.2}", total_net);
    println!("================================================================================");

    Ok(())
}
