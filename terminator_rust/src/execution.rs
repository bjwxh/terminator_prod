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

fn flatten_orders(orders: Vec<Value>, parent_order_id: Option<String>) -> Vec<Value> {
    let mut flattened = Vec::new();
    for mut o in orders {
        let children = o.get_mut("childOrderStrategies")
            .and_then(|v| v.as_array_mut())
            .map(|arr| std::mem::take(arr))
            .unwrap_or_default();

        let current_id = o.get("orderId")
            .and_then(|v| {
                if v.is_number() {
                    Some(v.to_string())
                } else {
                    v.as_str().map(|s| s.to_string())
                }
            })
            .unwrap_or_default();
        
        let top_parent_id = parent_order_id.clone().unwrap_or(current_id);

        if !children.is_empty() {
            // Recurse into child orders under the parent strategy container
            flattened.extend(flatten_orders(children, Some(top_parent_id)));
        } else {
            if let Some(ref p_id) = parent_order_id {
                if let Some(obj) = o.as_object_mut() {
                    obj.insert("_parent_order_id".to_string(), Value::String(p_id.clone()));
                }
            }
            flattened.push(o);
        }
    }
    flattened
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
        if let Ok(mock_json) = std::env::var("TERMINATOR_TEST_LIVE_POSITIONS") {
            if let Ok(pos) = serde_json::from_str::<Vec<BrokerPosition>>(&mock_json) {
                return Ok(pos);
            }
        }
        if std::env::var("TERMINATOR_TEST_ENV").is_ok() {
            return Ok(Vec::new());
        }
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

    pub async fn chase_order(&self, account_hash: &str, order_id: &str, grid: &crate::grid::OptionsGrid) -> Result<bool> {
        let working = self.get_working_orders(account_hash).await?;
        let order = working.into_iter().find(|o| {
            o.get("orderId").and_then(|v| {
                if v.is_number() {
                    Some(v.to_string())
                } else {
                    v.as_str().map(|s| s.to_string())
                }
            }).as_deref() == Some(order_id)
        });

        let order = match order {
            Some(o) => o,
            None => {
                error!("Cannot chase order {}: Not found in working orders.", order_id);
                return Ok(false);
            }
        };

        let mark = match Self::get_order_mark(&order, grid) {
            Some(m) => m,
            None => {
                error!("Cannot chase order {}: Could not calculate mark.", order_id);
                return Ok(false);
            }
        };

        // Pricing logic:
        // Credit (mark < 0): Round DOWN in absolute value to accept less credit (e.g. 5.27 -> 5.25)
        // Debit (mark >= 0): Round UP in absolute value to pay more debit (e.g. 5.27 -> 5.30)
        let mark_abs = mark.abs();
        let is_credit = mark < 0.0;
        let new_abs_price = if is_credit {
            (mark_abs / 0.05).floor() * 0.05
        } else {
            (mark_abs / 0.05).ceil() * 0.05
        };
        let new_abs_price = (new_abs_price * 100.0).round() / 100.0;

        let old_price = order.get("price").and_then(|v| v.as_f64()).unwrap_or(0.0);
        if (old_price - new_abs_price).abs() < 0.01 {
            info!("Order {} price ${} is already at target ${:.2}. Skipping.", order_id, old_price, new_abs_price);
            return Ok(true);
        }

        info!("Chasing order {} from ${} to ${:.2} (Mark: {:.2})", order_id, old_price, new_abs_price, mark);

        // Build replacement payload
        let mut legs_collection = Vec::new();
        if let Some(legs) = order.get("orderLegCollection").and_then(|v| v.as_array()) {
            for leg in legs {
                let instr = leg.get("instruction").and_then(|v| v.as_str()).unwrap_or("");
                let qty = leg.get("quantity").and_then(|v| v.as_f64()).unwrap_or(1.0);
                let symbol = leg.pointer("/instrument/symbol").and_then(|v| v.as_str()).unwrap_or("");
                
                legs_collection.push(serde_json::json!({
                    "instruction": instr,
                    "quantity": qty,
                    "instrument": {
                        "symbol": symbol,
                        "assetType": "OPTION"
                    }
                }));
            }
        }
        
        let mut replacement_spec = serde_json::json!({
            "orderType": order.get("orderType").and_then(|v| v.as_str()).unwrap_or("LIMIT"),
            "session": "NORMAL",
            "duration": "DAY",
            "price": format!("{:.2}", new_abs_price),
            "orderStrategyType": "SINGLE",
            "quantity": order.get("quantity").and_then(|v| v.as_f64()).unwrap_or(1.0),
            "orderLegCollection": legs_collection
        });
        
        if let Some(complex_type) = order.get("complexOrderStrategyType") {
            if !complex_type.is_null() {
                replacement_spec["complexOrderStrategyType"] = complex_type.clone();
            }
        }

        let access_token = self.token_manager.get_access_token();
        let url = format!("https://api.schwabapi.com/trader/v1/accounts/{}/orders/{}", account_hash, order_id);
        
        let response = self.client
            .put(&url)
            .bearer_auth(access_token)
            .json(&replacement_spec)
            .send()
            .await
            .context("Failed HTTP request to replace order")?;

        if response.status().is_success() {
            info!("Order replacement request accepted for ID: {}", order_id);
            Ok(true)
        } else {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            error!("REST order replacement failed with status {}: {}", status, body);
            anyhow::bail!("REST order replacement failure: {} - {}", status, body)
        }
    }

    pub async fn cancel_all_orders(&self, account_hash: &str) -> Result<Vec<String>> {
        let working = self.get_working_orders(account_hash).await?;
        let mut cancelled_ids = Vec::new();
        for order in working {
            if let Some(order_id) = order.get("orderId").and_then(|v| {
                if v.is_number() {
                    Some(v.to_string())
                } else {
                    v.as_str().map(|s| s.to_string())
                }
            }) {
                if self.cancel_order(account_hash, &order_id).await.unwrap_or(false) {
                    cancelled_ids.push(order_id);
                }
            }
        }
        Ok(cancelled_ids)
    }

fn get_order_mark(order: &Value, grid: &crate::grid::OptionsGrid) -> Option<f64> {
    let legs = order.get("orderLegCollection")?.as_array()?;
    if legs.is_empty() { return None; }
    
    let order_qty = order.get("quantity").and_then(|v| v.as_f64()).unwrap_or(1.0);
    let mut total_mark = 0.0;
    
    for leg in legs {
        let instr = leg.get("instrument")?;
        let symbol = instr.get("symbol")?.as_str()?;
        let instruction = leg.get("instruction")?.as_str()?;
        let qty = leg.get("quantity").and_then(|v| v.as_f64()).unwrap_or(1.0);
        
        let lookup = grid.symbol_lookup.get(symbol)?;
        let quote_ref = grid.quotes.get(&lookup.strike)?;
        let quote = quote_ref.value();
        
        let leg_quote = if lookup.is_call {
            quote.call.as_ref()?
        } else {
            quote.put.as_ref()?
        };
        
        let mid = leg_quote.mid;
        let leg_val = mid * (qty / order_qty);
        if instruction.contains("SELL") {
            total_mark -= leg_val;
        } else {
            total_mark += leg_val;
        }
    }
    
    Some(total_mark)
}

    pub async fn get_working_orders(&self, account_hash: &str) -> Result<Vec<Value>> {
        let access_token = self.token_manager.get_access_token();
        
        let now_utc = Utc::now();
        let from_time = (now_utc - chrono::Duration::days(60)).to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        let to_time = now_utc.to_rfc3339_opts(chrono::SecondsFormat::Millis, true);

        let url = format!("https://api.schwabapi.com/trader/v1/accounts/{}/orders", account_hash);
        let response = self.client
            .get(&url)
            .query(&[
                ("fromEnteredTime", from_time.as_str()),
                ("toEnteredTime", to_time.as_str()),
            ])
            .bearer_auth(access_token)
            .send()
            .await
            .context("Failed HTTP request to get working orders")?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            anyhow::bail!("Get working orders API returned error {}: {}", status, body);
        }

        let orders: Vec<Value> = response.json().await?;
        info!("Schwab GET /orders returned {} total orders (before filter).", orders.len());
        
        let flattened = flatten_orders(orders, None);
        let active_orders: Vec<Value> = flattened.into_iter().filter(|o| {
            let status = o.get("status").and_then(|v| v.as_str()).unwrap_or("");
            status != "FILLED" && status != "CANCELED" && status != "REJECTED" && status != "EXPIRED" && status != "REPLACED"
        }).collect();

        Ok(active_orders)
    }

    /// Fetch today's filled orders and parse them into Trades for Soft Bootstrap
    pub async fn get_today_filled_orders(&self, account_hash: &str, commission_per_contract: f64) -> Result<Vec<Trade>> {
        let access_token = self.token_manager.get_access_token();
        
        let now_utc = Utc::now();
        // Today 00:00:00 CT -> represented in UTC
        // This is a naive approximation for the Schwab API which accepts UTC ISO8601 strings
        let tz: chrono_tz::Tz = "America/Chicago".parse().unwrap();
        let now_ct = now_utc.with_timezone(&tz);
        let start_of_day_ct = now_ct.date_naive().and_hms_opt(0, 0, 0).unwrap().and_local_timezone(tz).unwrap();
        let from_time = (start_of_day_ct - chrono::Duration::hours(24)).with_timezone(&Utc).to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        let to_time = now_utc.to_rfc3339_opts(chrono::SecondsFormat::Millis, true);

        let today_yymmdd = now_ct.format("%y%m%d").to_string();

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
        let flattened = flatten_orders(orders, None);
        let mut filled_trades = Vec::new();

        for order in flattened {
            let status = order.get("status").and_then(|v| v.as_str()).unwrap_or("");
            if status != "FILLED" { continue; }

            // Filter out orders filled before today's start of day (Chicago time)
            if let Some(close_time_str) = order.get("closeTime").and_then(|v| v.as_str()) {
                if let Ok(close_dt) = chrono::DateTime::parse_from_rfc3339(&close_time_str.replace("Z", "+00:00")) {
                    if close_dt.with_timezone(&Utc) < start_of_day_ct.with_timezone(&Utc) {
                        continue;
                    }
                }
            }

            let legs = order.get("orderLegCollection").and_then(|v| v.as_array());
            if let Some(leg_array) = legs {
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

                        let is_0dte_spx = if let Some(code) = symbol.split_whitespace().last() {
                            if code.len() >= 15 {
                                let date_str = &code[code.len() - 15..code.len() - 9];
                                date_str == today_yymmdd
                            } else { false }
                        } else { false };

                        if !is_0dte_spx {
                            continue;
                        }

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

                    if !option_legs.is_empty() {
                        let mut executed_price = order.get("price").and_then(|v| v.as_f64()).unwrap_or(0.0);
                        let (_, is_credit_structural) = crate::strategy::classify_order_type(&option_legs);
                        
                        let mut found_execution_legs = false;
                        let mut total_qty = 0.0;
                        let mut weighted_price_sum = 0.0;
                        if let Some(activities) = order.get("orderActivityCollection").and_then(|v| v.as_array()) {
                            for activity in activities {
                                if activity.get("activityType").and_then(|v| v.as_str()) == Some("EXECUTION") {
                                    let qty = activity.get("quantity").and_then(|v| v.as_f64()).unwrap_or(0.0);
                                    if let Some(exec_legs) = activity.get("executionLegs").and_then(|v| v.as_array()) {
                                        let mut net_price = 0.0;
                                        for exec_leg in exec_legs {
                                            if let Some(leg_id) = exec_leg.get("legId").and_then(|v| v.as_i64()) {
                                                if let Some(price) = exec_leg.get("price").and_then(|v| v.as_f64()) {
                                                    let mut instruction = "";
                                                    if let Some(leg_collection) = order.get("orderLegCollection").and_then(|v| v.as_array()) {
                                                        for l in leg_collection {
                                                            if l.get("legId").and_then(|v| v.as_i64()) == Some(leg_id) {
                                                                instruction = l.get("instruction").and_then(|v| v.as_str()).unwrap_or("");
                                                            }
                                                        }
                                                    }
                                                    if instruction.starts_with("SELL") {
                                                        net_price += price;
                                                    } else if instruction.starts_with("BUY") {
                                                        net_price -= price;
                                                    }
                                                }
                                            }
                                        }
                                        if net_price != 0.0 && qty > 0.0 {
                                            weighted_price_sum += net_price * qty;
                                            total_qty += qty;
                                            found_execution_legs = true;
                                        }
                                    }
                                }
                            }
                        }
                        if found_execution_legs && total_qty > 0.0 {
                            executed_price = weighted_price_sum / total_qty;
                        }

                        let base_qty = option_legs.first().map(|l| l.quantity.abs() as f64).unwrap_or(1.0);
                        let mut credit = executed_price * base_qty * 100.0;
                        if !found_execution_legs && is_credit_structural == Some(false) {
                            credit = -credit;
                        }

                        let total_contracts: i32 = option_legs.iter().map(|l| l.quantity.abs()).sum();
                        let commission = total_contracts as f64 * commission_per_contract;

                        let timestamp = order.get("closeTime").and_then(|v| v.as_str()).unwrap_or("").to_string();
                        let order_id = order.get("orderId")
                            .and_then(|v| {
                                if v.is_number() {
                                    Some(v.to_string())
                                } else {
                                    v.as_str().map(|s| s.to_string())
                                }
                            }).unwrap_or_default();
                        
                        filled_trades.push(Trade {
                            timestamp,
                            credit,
                            commission,
                            purpose: "HISTORICAL_BROKER".to_string(),
                            legs: option_legs,
                            strategy_id: order_id,
                        });
                    }
            }
        }

        Ok(filled_trades)
    }
}
