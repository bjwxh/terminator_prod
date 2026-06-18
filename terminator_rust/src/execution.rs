use std::sync::Arc;
use std::time::Duration;
use anyhow::{Context, Result};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tracing::{info, error, warn, debug};

use crate::token::TokenManager;
use crate::parser::parse_occ_symbol;
use crate::strategy::{Trade, OptionLeg};
use chrono::{Utc, TimeZone};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrokerPosition {
    pub symbol: String,
    pub strike: f64,
    pub side: String, // "CALL" or "PUT"
    pub quantity: i32,
    pub price: f64,
    pub avg_price: f64,
    pub current_day_pnl: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrokerOrder {
    pub order_id: String,
    pub status: String,
    pub symbol: String,
    pub quantity: f64,
    pub price: Option<f64>,
}

pub struct ExecutionClient {
    token_manager: Arc<TokenManager>,
    client: Client,
}

impl ExecutionClient {
    pub fn new(token_manager: Arc<TokenManager>) -> Self {
        let client = Client::builder()
            .timeout(Duration::from_secs(30))
            .build()
            .expect("Failed to build reqwest Client");
        Self { token_manager, client }
    }

    /// Retrieve the Schwab account hash value for the active account ID.
    pub async fn get_account_hash(&self) -> Result<String> {
        let access_token = self.token_manager.get_access_token();
        let account_id = self.token_manager.get_account_id();

        debug!("Resolving account hash for account ID: {}...", account_id);

        let response = self.client
            .get("https://api.schwabapi.com/trader/v1/accounts/accountNumbers")
            .bearer_auth(access_token)
            .send()
            .await
            .context("Failed HTTP request to accountNumbers endpoint")?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            anyhow::bail!("Account numbers API returned error {}: {}", status, body);
        }

        let accounts: Vec<Value> = response.json().await?;
        for acc in accounts {
            if let Some(num) = acc.get("accountNumber").and_then(|v| v.as_str()) {
                if num == account_id {
                    if let Some(hash) = acc.get("hashValue").and_then(|v| v.as_str()) {
                        info!("Successfully resolved account hash for {}: {}", account_id, hash);
                        return Ok(hash.to_string());
                    }
                }
            }
        }

        anyhow::bail!("Could not resolve account hash for account ID: {}", account_id)
    }

    /// Fetch SPX options positions currently open on the broker.
    pub async fn get_live_positions(&self, account_hash: &str) -> Result<Vec<BrokerPosition>> {
        let access_token = self.token_manager.get_access_token();
        let url = format!("https://api.schwabapi.com/trader/v1/accounts/{}", account_hash);

        let response = self.client
            .get(&url)
            .query(&[("fields", "positions")])
            .bearer_auth(access_token)
            .send()
            .await
            .context("Failed HTTP request to get account positions")?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            anyhow::bail!("Get positions API returned error {}: {}", status, body);
        }

        let res_json: Value = response.json().await?;
        let positions = res_json
            .pointer("/securitiesAccount/positions")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();

        let mut broker_positions = Vec::new();

        for pos in positions {
            let instr = pos.get("instrument").cloned().unwrap_or(Value::Null);
            let asset_type = instr.get("assetType").and_then(|v| v.as_str()).unwrap_or_default();
            let symbol = instr.get("symbol").and_then(|v| v.as_str()).unwrap_or_default().to_string();
            let underlying_sym = instr.get("underlyingSymbol").and_then(|v| v.as_str()).unwrap_or_default();

            if asset_type == "OPTION" && (underlying_sym == "$SPX" || symbol.starts_with("SPX")) {
                if let Some(parsed) = parse_occ_symbol(&symbol) {
                    let long_qty = pos.get("longQuantity").and_then(|v| v.as_f64()).unwrap_or(0.0) as i32;
                    let short_qty = pos.get("shortQuantity").and_then(|v| v.as_f64()).unwrap_or(0.0) as i32;
                    let qty = long_qty - short_qty;

                    if qty != 0 {
                        let mv = pos.get("marketValue").and_then(|v| v.as_f64()).unwrap_or(0.0);
                        let avg_price = pos.get("averagePrice").and_then(|v| v.as_f64()).unwrap_or(0.0);
                        let current_day_pnl = pos.get("currentDayProfitLoss").and_then(|v| v.as_f64()).unwrap_or(0.0);

                        broker_positions.push(BrokerPosition {
                            symbol,
                            strike: parsed.strike,
                            side: parsed.side,
                            quantity: qty,
                            price: mv / (qty as f64 * 100.0),
                            avg_price,
                            current_day_pnl,
                        });
                    }
                }
            }
        }

        Ok(broker_positions)
    }

    /// Place a REST order to the Schwab margin account.
    pub async fn place_order(&self, account_hash: &str, order_body: Value) -> Result<Option<String>> {
        let access_token = self.token_manager.get_access_token();
        let url = format!("https://api.schwabapi.com/trader/v1/accounts/{}/orders", account_hash);

        info!("Transmitting REST Order Placement: {}", serde_json::to_string(&order_body)?);

        let response = self.client
            .post(&url)
            .json(&order_body)
            .bearer_auth(access_token)
            .send()
            .await
            .context("Failed HTTP request to place order")?;

        let status = response.status();
        if status == reqwest::StatusCode::CREATED || status.is_success() {
            // Attempt to extract SchwabOrderID from the "Location" header
            if let Some(loc_header) = response.headers().get(reqwest::header::LOCATION) {
                if let Ok(loc_str) = loc_header.to_str() {
                    if let Some(order_id) = loc_str.split('/').last() {
                        info!("Successfully placed order. Assigned Order ID: {}", order_id);
                        return Ok(Some(order_id.to_string()));
                    }
                }
            }
            info!("Successfully placed order, but Location header not found/parseable");
            Ok(None)
        } else {
            let body = response.text().await.unwrap_or_default();
            error!("REST order placement failed with status {}: {}", status, body);
            anyhow::bail!("REST order failure: {} - {}", status, body)
        }
    }

    /// Cancel a working REST order.
    pub async fn cancel_order(&self, account_hash: &str, order_id: &str) -> Result<bool> {
        let access_token = self.token_manager.get_access_token();
        let url = format!("https://api.schwabapi.com/trader/v1/accounts/{}/orders/{}", account_hash, order_id);

        info!("Sending Cancel request for Order ID: {}...", order_id);

        let response = self.client
            .delete(&url)
            .bearer_auth(access_token)
            .send()
            .await
            .context("Failed HTTP request to cancel order")?;

        if response.status().is_success() {
            info!("Order cancellation request accepted for ID: {}", order_id);
            Ok(true)
        } else {
            let body = response.text().await.unwrap_or_default();
            warn!("Cancellation request rejected for ID {}: {}", order_id, body);
            Ok(false)
        }
    }

    pub async fn chase_order(&self, account_hash: &str, order_id: &str) -> Result<bool> {
        info!("Sending Chase request for Order ID: {} (currently just cancels)", order_id);
        self.cancel_order(account_hash, order_id).await
    }

    pub async fn cancel_all_orders(&self, _account_hash: &str) -> Result<Vec<String>> {
        warn!("cancel_all_orders is a stub that currently does nothing");
        Ok(Vec::new())
    }

    /// Fetch today's filled orders and parse them into Trades for Soft Bootstrap
    pub async fn get_today_filled_orders(&self, account_hash: &str) -> Result<Vec<Trade>> {
        let access_token = self.token_manager.get_access_token();
        
        let now_utc = Utc::now();
        // Today 00:00:00 CT -> represented in UTC
        // This is a naive approximation for the Schwab API which accepts UTC ISO8601 strings
        let tz: chrono_tz::Tz = "America/Chicago".parse().unwrap();
        let now_ct = now_utc.with_timezone(&tz);
        let start_of_day_ct = now_ct.date_naive().and_hms_opt(0, 0, 0).unwrap().and_local_timezone(tz).unwrap();
        let from_time = start_of_day_ct.with_timezone(&Utc).to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        let to_time = now_utc.to_rfc3339_opts(chrono::SecondsFormat::Millis, true);

        let url = format!("https://api.schwabapi.com/trader/v1/accounts/{}/orders", account_hash);
        let response = self.client
            .get(&url)
            .query(&[
                ("fromEnteredTime", from_time.as_str()),
                ("toEnteredTime", to_time.as_str()),
                ("status", "FILLED"),
            ])
            .bearer_auth(access_token)
            .send()
            .await
            .context("Failed HTTP request to get orders")?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            anyhow::bail!("Get orders API returned error {}: {}", status, body);
        }

        let orders: Vec<Value> = response.json().await?;
        let mut filled_trades = Vec::new();

        for order in orders {
            let status = order.get("status").and_then(|v| v.as_str()).unwrap_or("");
            if status != "FILLED" { continue; }

            // Check if it's an Iron Condor (4 legs)
            let legs = order.get("orderLegCollection").and_then(|v| v.as_array());
            if let Some(leg_array) = legs {
                if leg_array.len() == 4 {
                    let mut option_legs = Vec::new();
                    for leg in leg_array {
                        let instr = leg.get("instrument").cloned().unwrap_or(Value::Null);
                        let symbol = instr.get("symbol").and_then(|v| v.as_str()).unwrap_or("").to_string();
                        let instruction = leg.get("instruction").and_then(|v| v.as_str()).unwrap_or("");
                        let quantity = leg.get("quantity").and_then(|v| v.as_f64()).unwrap_or(0.0) as i32;
                        
                        let signed_qty = match instruction {
                            "BUY_TO_OPEN" | "BUY_TO_CLOSE" => quantity,
                            "SELL_TO_OPEN" | "SELL_TO_CLOSE" => -quantity,
                            _ => quantity,
                        };

                        if let Some(parsed) = parse_occ_symbol(&symbol) {
                            option_legs.push(OptionLeg {
                                symbol,
                                strike: parsed.strike,
                                side: parsed.side,
                                quantity: signed_qty,
                                delta: 0.0, // Historical DB provides delta later
                                theta: 0.0,
                                price: 0.0, // Filled price per leg isn't easily mapped without execution chunks
                                instruction: Some(instruction.to_string()),
                            });
                        }
                    }

                    if option_legs.len() == 4 {
                        let credit = order.get("price").and_then(|v| v.as_f64()).unwrap_or(0.0) * 100.0;
                        let timestamp = order.get("closeTime").and_then(|v| v.as_str()).unwrap_or("").to_string();
                        let order_id = order.get("orderId").and_then(|v| {
                            if v.is_number() {
                                Some(v.to_string())
                            } else {
                                v.as_str().map(|s| s.to_string())
                            }
                        }).unwrap_or_default();
                        
                        filled_trades.push(Trade {
                            timestamp,
                            legs: option_legs,
                            credit,
                            commission: 0.0,
                            purpose: "IRON_CONDOR".to_string(),
                            strategy_id: order_id, // temporarily store order_id in strategy_id to map later
                        });
                    }
                }
            }
        }

        Ok(filled_trades)
    }
}
