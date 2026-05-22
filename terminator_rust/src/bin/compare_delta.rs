use std::collections::HashMap;
use anyhow::{Result, Context};
use serde::Deserialize;
use tracing::{warn, Level};
use tracing_subscriber::FmtSubscriber;

use terminator_rust::{
    config::AppConfig,
    token::TokenManager,
    greeks::{calculate_delta, calculate_t_to_expiration},
};

#[derive(Debug, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
struct SchwabContract {
    symbol: String,
    strike_price: f64,
    days_to_expiration: i32,
    bid: f64,
    ask: f64,
    delta: Option<f64>,
    volatility: Option<f64>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct SchwabOptionChainResponse {
    underlying_price: Option<f64>,
    call_exp_date_map: Option<HashMap<String, HashMap<String, Vec<SchwabContract>>>>,
    put_exp_date_map: Option<HashMap<String, HashMap<String, Vec<SchwabContract>>>>,
}

struct DeltaComparison {
    symbol: String,
    strike: f64,
    mid: f64,
    schwab_delta: f64,
    local_delta: f64,
}

#[tokio::main]
async fn main() -> Result<()> {
    // Initialize standard logging
    let subscriber = FmtSubscriber::builder()
        .with_max_level(Level::WARN)
        .finish();
    tracing::subscriber::set_global_default(subscriber).ok();

    println!("===============================================================================");
    println!(" 📈  Live Option Delta Comparison: Schwab REST API vs Local BSM Engine");
    println!("===============================================================================\n");

    println!("🔑 Loading credentials from .env and JSON token file...");
    let config = AppConfig::load().context("Failed to load application configuration")?;
    let token_manager = TokenManager::new(config).context("Failed to initialize TokenManager")?;
    let access_token = token_manager.get_access_token();

    println!("📡 Fetching option chain for $SPX from Schwab REST API...");
    let client = reqwest::Client::new();
    let today = chrono::Local::now().format("%Y-%m-%d").to_string();

    let response = client
        .get("https://api.schwabapi.com/marketdata/v1/chains")
        .bearer_auth(access_token)
        .query(&[
            ("symbol", "$SPX"),
            ("strikeCount", "80"), // Fetch a robust window around ATM safely
            ("fromDate", &today),
            ("toDate", &today),
        ])
        .send()
        .await
        .context("HTTP request to option chain API failed")?;

    if !response.status().is_success() {
        let status = response.status();
        let body = response.text().await.unwrap_or_default();
        anyhow::bail!("Schwab Option Chain API returned {}: {}", status, body);
    }

    let chain: SchwabOptionChainResponse = response.json().await
        .context("Failed to deserialize option chain response")?;

    let spx_price = match chain.underlying_price {
        Some(price) if price > 0.0 => price,
        _ => {
            warn!("Schwab returned missing or invalid underlying price. Defaulting to 5300.0");
            5300.0
        }
    };
    println!("💲 Current $SPX Index Price: ${:.2}", spx_price);

    // Collect first available expiration date
    let mut selected_exp = String::new();
    let mut calls_list = Vec::new();
    let mut puts_list = Vec::new();

    if let Some(ref calls_map) = chain.call_exp_date_map {
        for (exp_date, strikes) in calls_map {
            selected_exp = exp_date.clone();
            for (_strike_str, contracts) in strikes {
                for c in contracts {
                    calls_list.push(c.clone());
                }
            }
            break; // Pull first available expiration
        }
    }

    if let Some(ref puts_map) = chain.put_exp_date_map {
        for (exp_date, strikes) in puts_map {
            if selected_exp.is_empty() || &selected_exp == exp_date {
                selected_exp = exp_date.clone();
                for (_strike_str, contracts) in strikes {
                    for c in contracts {
                        puts_list.push(c.clone());
                    }
                }
            }
        }
    }

    if selected_exp.is_empty() {
        println!("❌ No active option contracts found in the returned chain.");
        return Ok(());
    }

    println!("📅 Selected Expiration Date: {} (DTE: {})", 
        selected_exp.split(':').next().unwrap_or(&selected_exp),
        calls_list.first().map(|c| c.days_to_expiration).unwrap_or(0)
    );

    // Sort by strike price for perfect readability
    calls_list.sort_by(|a, b| a.strike_price.partial_cmp(&b.strike_price).unwrap());
    puts_list.sort_by(|a, b| a.strike_price.partial_cmp(&b.strike_price).unwrap());

    // Parameters for local BSM delta calculation
    let t = calculate_t_to_expiration();
    let r = 0.0525; // 5.25% risk-free rate estimate

    println!("⏰ Calculated Time to Expiration (T): {:.6} years ({:.1} hours left)", 
        t, t * 365.0 * 24.0
    );

    // Build comparison lists
    let mut call_comparisons = Vec::new();
    let mut put_comparisons = Vec::new();

    let mut build_comparisons = |contracts: &[SchwabContract], is_call: bool, list: &mut Vec<DeltaComparison>| {
        for c in contracts {
            let mid = (c.bid + c.ask) / 2.0;
            if mid <= 0.0 {
                continue; // Skip zero-price strikes
            }
            let sch_delta = match c.delta {
                Some(d) => d,
                None => continue,
            };
            let contract_t = if c.days_to_expiration > 0 {
                (c.days_to_expiration as f64) / 365.0
            } else {
                t
            };
            let local_delta = calculate_delta(mid, spx_price, c.strike_price, contract_t, r, is_call);
            list.push(DeltaComparison {
                symbol: c.symbol.clone(),
                strike: c.strike_price,
                mid,
                schwab_delta: sch_delta,
                local_delta,
            });
        }
    };

    build_comparisons(&calls_list, true, &mut call_comparisons);
    build_comparisons(&puts_list, false, &mut put_comparisons);

    // Print Detailed Option Tables
    println!("\n-------------------------------------------------------------------------------------------------");
    println!(" {:<20} | {:<8} | {:<8} | {:<10} | {:<12} | {:<12} | {:<10}", 
        "Contract Symbol", "Strike", "Side", "Mid Price", "Schwab Delta", "BSM Delta", "Difference"
    );
    println!("-------------------------------------------------------------------------------------------------");

    for c in &call_comparisons {
        if (c.strike - spx_price).abs() > 50.0 {
            continue;
        }
        let diff = (c.schwab_delta - c.local_delta).abs();
        println!(" {:<20} | {:<8.1} | {:<8} | {:<10.2} | {:<12.4} | {:<12.4} | {:<10.4}",
            c.symbol, c.strike, "CALL", c.mid, c.schwab_delta, c.local_delta, diff
        );
    }
    println!("-------------------------------------------------------------------------------------------------");
    for p in &put_comparisons {
        if (p.strike - spx_price).abs() > 50.0 {
            continue;
        }
        let diff = (p.schwab_delta - p.local_delta).abs();
        println!(" {:<20} | {:<8.1} | {:<8} | {:<10.2} | {:<12.4} | {:<12.4} | {:<10.4}",
            p.symbol, p.strike, "PUT", p.mid, p.schwab_delta, p.local_delta, diff
        );
    }
    println!("-------------------------------------------------------------------------------------------------");

    // Let's test the target delta matching behavior!
    println!("\n===============================================================================");
    println!(" 🎯  Target Delta Strikes Selection Comparison");
    println!("===============================================================================");
    println!("We test if selecting an option near a target delta yields the same strike price.");
    println!("-------------------------------------------------------------------------------");
    println!(" {:<10} | {:<6} | {:<18} | {:<18} | {:<10}",
        "Side", "Target", "Schwab Chosen Strike", "BSM Chosen Strike", "Match?"
    );
    println!("-------------------------------------------------------------------------------");

    let test_call_deltas = vec![0.02, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.50];
    let test_put_deltas = vec![-0.02, -0.05, -0.10, -0.15, -0.20, -0.30, -0.40, -0.50];

    for target in test_call_deltas {
        // Schwab closest
        let sch_best = call_comparisons.iter()
            .min_by(|a, b| {
                let diff_a = (a.schwab_delta - target).abs();
                let diff_b = (b.schwab_delta - target).abs();
                diff_a.partial_cmp(&diff_b).unwrap()
            });

        // Local BSM closest
        let bsm_best = call_comparisons.iter()
            .min_by(|a, b| {
                let diff_a = (a.local_delta - target).abs();
                let diff_b = (b.local_delta - target).abs();
                diff_a.partial_cmp(&diff_b).unwrap()
            });

        if let (Some(s_opt), Some(b_opt)) = (sch_best, bsm_best) {
            let strikes_match = s_opt.strike == b_opt.strike;
            println!(" {:<10} | {:<6.2} | ${:<5.1} (sch: {:<+.3}) | ${:<5.1} (bsm: {:<+.3}) | {:<10}",
                "CALL",
                target,
                s_opt.strike,
                s_opt.schwab_delta,
                b_opt.strike,
                b_opt.local_delta,
                if strikes_match { "✅ YES" } else { "❌ NO" }
            );
        }
    }

    println!("-------------------------------------------------------------------------------");

    for target in test_put_deltas {
        // Schwab closest
        let sch_best = put_comparisons.iter()
            .min_by(|a, b| {
                let diff_a = (a.schwab_delta - target).abs();
                let diff_b = (b.schwab_delta - target).abs();
                diff_a.partial_cmp(&diff_b).unwrap()
            });

        // Local BSM closest
        let bsm_best = put_comparisons.iter()
            .min_by(|a, b| {
                let diff_a = (a.local_delta - target).abs();
                let diff_b = (b.local_delta - target).abs();
                diff_a.partial_cmp(&diff_b).unwrap()
            });

        if let (Some(s_opt), Some(b_opt)) = (sch_best, bsm_best) {
            let strikes_match = s_opt.strike == b_opt.strike;
            println!(" {:<10} | {:<6.2} | ${:<5.1} (sch: {:<+.3}) | ${:<5.1} (bsm: {:<+.3}) | {:<10}",
                "PUT",
                target,
                s_opt.strike,
                s_opt.schwab_delta,
                b_opt.strike,
                b_opt.local_delta,
                if strikes_match { "✅ YES" } else { "❌ NO" }
            );
        }
    }

    println!("===============================================================================");
    println!("\n💡 Note: local BSM delta is dynamically solved using implied volatility back-calculation.");
    println!("Differences are expected due to varying models, interest rates, and discrete settlement rules.");
    println!("===============================================================================\n");

    Ok(())
}
