use std::collections::HashMap;
use serde::Deserialize;
use anyhow::{Result, Context};
use tracing::{info, error};

use crate::token::TokenManager;

#[derive(Debug, Clone, Hash, Eq, PartialEq)]
pub struct StrikeAndSide {
    pub strike: ordered_float::OrderedFloat<f64>,
    pub is_call: bool,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct SchwabOptionChainResponse {
    call_exp_date_map: Option<HashMap<String, HashMap<String, Vec<SchwabOptionContract>>>>,
    put_exp_date_map: Option<HashMap<String, HashMap<String, Vec<SchwabOptionContract>>>>,
    call_strategy_chain: Option<HashMap<String, HashMap<String, Vec<SchwabOptionContract>>>>,
    put_strategy_chain: Option<HashMap<String, HashMap<String, Vec<SchwabOptionContract>>>>,
}

#[derive(Debug, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
struct SchwabOptionContract {
    symbol: String,
    days_to_expiration: i32,
    strike_price: f64,
}

/// Fetch 0DTE options chain for $SPX and return a mapping from (Strike, is_call) to option symbol.
pub async fn fetch_0dte_option_chain(
    token_manager: &TokenManager,
) -> Result<HashMap<StrikeAndSide, String>> {
    let access_token = token_manager.get_access_token();
    let client = reqwest::Client::new();
    
    // Format today's date in YYYY-MM-DD local format
    let today = chrono::Local::now().format("%Y-%m-%d").to_string();
    
    info!("Fetching Schwab options chain for $SPX 0DTE on {}...", today);
    
    let response = client
        .get("https://api.schwabapi.com/marketdata/v1/chains")
        .bearer_auth(access_token)
        .query(&[
            ("symbol", "$SPX"),
            ("strikeCount", "150"), // Retrieve a robust strike window around the underlying ATM
            ("fromDate", &today),
            ("toDate", &today),
        ])
        .send()
        .await
        .context("Failed HTTP request to option chain API")?;

    if !response.status().is_success() {
        let status = response.status();
        let body = response.text().await.unwrap_or_default();
        error!("Option chains API returned error: Status = {}, Body = {}", status, body);
        anyhow::bail!("Option chains API error: {}", status);
    }

    let chain: SchwabOptionChainResponse = response.json().await
        .context("Failed to deserialize option chain JSON response")?;

    let mut symbol_map = HashMap::new();

    // 1. Process ExpDateMap structures
    let mut process_map = |exp_map: &HashMap<String, HashMap<String, Vec<SchwabOptionContract>>>, is_call: bool| {
        for (_exp_str, strikes) in exp_map {
            for (strike_str, contracts) in strikes {
                let strike = strike_str.parse::<f64>().unwrap_or_default();
                for contract in contracts {
                    if contract.days_to_expiration == 0 {
                        let key = StrikeAndSide {
                            strike: ordered_float::OrderedFloat(strike),
                            is_call,
                        };
                        symbol_map.insert(key, contract.symbol.clone());
                    }
                }
            }
        }
    };

    if let Some(ref calls) = chain.call_exp_date_map {
        process_map(calls, true);
    }
    if let Some(ref puts) = chain.put_exp_date_map {
        process_map(puts, false);
    }

    // 2. Process StrategyChain structures
    let mut process_chain = |strategy_chain: &HashMap<String, HashMap<String, Vec<SchwabOptionContract>>>, is_call: bool| {
        for (strike_str, exps) in strategy_chain {
            let strike = strike_str.parse::<f64>().unwrap_or_default();
            for (_exp_str, contracts) in exps {
                for contract in contracts {
                    if contract.days_to_expiration == 0 {
                        let key = StrikeAndSide {
                            strike: ordered_float::OrderedFloat(strike),
                            is_call,
                        };
                        symbol_map.insert(key, contract.symbol.clone());
                    }
                }
            }
        }
    };

    if let Some(ref calls) = chain.call_strategy_chain {
        process_chain(calls, true);
    }
    if let Some(ref puts) = chain.put_strategy_chain {
        process_chain(puts, false);
    }

    info!("Option chain mapping parsed successfully. Resolved {} active 0DTE contracts.", symbol_map.len());
    Ok(symbol_map)
}
