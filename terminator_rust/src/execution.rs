use std::sync::Arc;
use std::time::Duration;
use anyhow::{Context, Result};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tracing::{info, error, warn, debug};

use crate::token::TokenManager;
use crate::parser::parse_occ_symbol;

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
            .get("https://api.schwabapi.com/v1/accounts/accountNumbers")
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
        let url = format!("https://api.schwabapi.com/v1/accounts/{}", account_hash);

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
        let url = format!("https://api.schwabapi.com/v1/accounts/{}/orders", account_hash);

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
        let url = format!("https://api.schwabapi.com/v1/accounts/{}/orders/{}", account_hash, order_id);

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
}
