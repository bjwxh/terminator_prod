use std::collections::HashMap;
use std::time::Instant;
use anyhow::{Result, Context};
use serde::Deserialize;
use chrono::TimeZone;
use chrono_tz::America::Chicago;
use ordered_float::OrderedFloat;

use terminator_rust::{
    grid::{OptionsGrid, OptionQuote, OptionLegQuote},
    strategy::{SubStrategy, check_entry},
};

#[derive(Debug, Deserialize, Clone)]
struct SnapshotQuote {
    datetime: String,
    strike_price: f64,
    side: String,
    bidprice: f64,
    askprice: f64,
    delta: f64,
    theta: f64,
    symbol: String,
}

#[tokio::main]
async fn main() -> Result<()> {
    println!("===============================================================================");
    println!(" 🤖  Rust Backtest Simulation: Ingesting Python JSON Snapshots");
    println!("===============================================================================\n");

    let snapshot_file = "tmp/snapshots_20260521.json";
    println!("📖 Loading offline options chain snapshots from {}...", snapshot_file);
    let file_content = std::fs::read_to_string(snapshot_file)
        .context(format!("Failed to read snapshot file: {}", snapshot_file))?;
    
    let snapshots: HashMap<String, Vec<SnapshotQuote>> = serde_json::from_str(&file_content)
        .context("Failed to parse JSON snapshot data")?;

    let entry_times = vec!["09:01", "09:31", "10:01", "10:31"];

    for time_str in entry_times {
        let quotes = match snapshots.get(time_str) {
            Some(q) => q,
            None => {
                println!("⚠️ No snapshot found in JSON for entry time {}", time_str);
                continue;
            }
        };

        // Create a new options grid
        let grid = OptionsGrid::new(HashMap::new());

        // Populate the options grid with the snapshot's quotes
        for q in quotes {
            let strike_key = OrderedFloat(q.strike_price);
            let mut entry = grid.quotes.entry(strike_key).or_insert_with(|| OptionQuote {
                strike: q.strike_price,
                call: None,
                put: None,
                last_updated: Instant::now(),
            });

            let leg = OptionLegQuote {
                symbol: q.symbol.clone(),
                bid: q.bidprice,
                ask: q.askprice,
                mid: (q.bidprice + q.askprice) / 2.0,
                delta: q.delta,
                theta: q.theta,
                last_update: Instant::now(), // guaranteed fresh to pass the stale guard
            };

            if q.side == "CALL" {
                entry.call = Some(leg);
            } else {
                entry.put = Some(leg);
            }
        }

        // Parse hour and minute
        let parts: Vec<&str> = time_str.split(':').collect();
        let hour: u32 = parts[0].parse()?;
        let min: u32 = parts[1].parse()?;
        let time_obj = chrono::NaiveTime::from_hms_opt(hour, min, 0).unwrap();

        // Build timezone-aware timestamp
        let now_ct = Chicago.with_ymd_and_hms(2026, 5, 21, hour, min, 0).unwrap();

        // Create the SubStrategy
        let sid = format!("strat_{}", time_str.replace(":", ""));
        let s = SubStrategy::new(sid.clone(), time_obj, 0.175, 0.025, 1);

        println!("-------------------------------------------------------------------------------");
        println!("Checking Entry for {} at {} CT (Chicago)...", sid, time_str);
        println!("-------------------------------------------------------------------------------");

        if let Some(trade) = check_entry(&grid, &s, now_ct, 50.0) {
            println!("🎯 Rust Trade triggered successfully!");
            println!("  Net Entry Credit: ${:.2}", trade.credit);
            println!("  Commission: ${:.2}", trade.commission);
            println!("  Legs Selected:");
            for leg in &trade.legs {
                let action = if leg.quantity > 0 { "BUY" } else { "SELL" };
                println!("    {} {}x {} (Strike: {}, Mid: {:.2}, Delta: {:.4})",
                    action, leg.quantity.abs(), leg.symbol, leg.strike, leg.price, leg.delta);
            }
        } else {
            println!("❌ Rust check_entry returned None (Signal did not trigger or spread verification failed)");
        }
        println!();
    }

    println!("===============================================================================");
    println!(" ✅  Rust Backtest Simulation Completed.");
    println!("===============================================================================");

    Ok(())
}
