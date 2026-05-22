use serde::{Deserialize, Serialize};
use serde_json::Value;
use tracing::{warn, debug};

use crate::grid::OptionsGrid;

// ============================================================================
// ACCT_ACTIVITY Stream Event Structures
// ============================================================================

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct OrderActivityLeg {
    pub leg_id: String,
    pub symbol: String,
    pub buy_sell: String, // "Buy", "Sell", "BuyToOpen", "SellToOpen", etc.
    pub quantity: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct OrderActivityEvent {
    pub order_id: String,
    pub account_number: String,
    pub message_type: String, // "OrderCreated", "OrderAccepted", "ExecutionCreated", "OrderUROutCompleted", "CancelAccepted", etc.
    pub status: String,       // "Created", "Open", "Cancelled", "Filled", "Unknown"
    pub legs: Vec<OrderActivityLeg>,
    pub limit_price: Option<f64>,
}

/// Helper function to parse Schwab's protobuf-decimal representation from JSON.
/// Formula: Value = lo * 10^-(signScale - 6) => Value = lo / 10^(signScale - 6)
pub fn parse_schwab_decimal(val: &Value) -> Option<f64> {
    let lo: f64 = if let Some(s) = val.get("lo").and_then(|v| v.as_str()) {
        s.parse().ok()?
    } else if let Some(n) = val.get("lo").and_then(|v| v.as_f64()) {
        n
    } else {
        return None;
    };
    
    let sign_scale = val.get("signScale")
        .or_else(|| val.get("sign-scale"))
        .and_then(|s| s.as_i64())?;
    
    let exponent = sign_scale - 6;
    let factor = 10.0f64.powi(exponent as i32);
    Some(lo / factor)
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ParsedSymbol {
    pub strike: f64,
    pub side: String,
}

pub fn parse_occ_symbol(symbol: &str) -> Option<ParsedSymbol> {
    let symbol = symbol.trim();
    let parts: Vec<&str> = symbol.split_whitespace().collect();
    if parts.is_empty() {
        return None;
    }
    let code = parts.last()?;
    if code.len() < 9 {
        return None;
    }

    let strike_str = &code[code.len() - 8..];
    let side_char = code.chars().nth(code.len() - 9)?;

    let strike = strike_str.parse::<f64>().ok()? / 1000.0;
    let side = if side_char == 'C' { "CALL" } else { "PUT" }.to_string();

    Some(ParsedSymbol { strike, side })
}

/// Decode the JSON-escaped MESSAGE_DATA content from ACCT_ACTIVITY feed
pub fn parse_acct_activity_data(
    message_type: &str,
    data_str: &str,
) -> Option<OrderActivityEvent> {
    let data: serde_json::Value = match serde_json::from_str(data_str) {
        Ok(v) => v,
        Err(e) => {
            warn!("Failed to parse MESSAGE_DATA JSON string: {:?}", e);
            return None;
        }
    };
    
    let order_id = data.get("SchwabOrderID")
        .or_else(|| data.get("schwabOrderID"))
        .and_then(|v| v.as_str())
        .unwrap_or_default()
        .to_string();
        
    let account_number = data.get("AccountNumber")
        .or_else(|| data.get("accountNumber"))
        .and_then(|v| v.as_str())
        .unwrap_or_default()
        .to_string();

    let mut legs = Vec::new();
    let mut limit_price = None;
    let mut status = "Unknown".to_string();

    // 1. Extract Order Legs if present (e.g. OrderCreated, Vertical spread events)
    if let Some(legs_val) = data.pointer("/BaseEvent/OrderCreatedEventEquityOrder/Order/Order/AssetOrderEquityOrderLeg/OrderLegs").and_then(|v| v.as_array()) {
        for leg_val in legs_val {
            let leg_id = leg_val.get("LegID").and_then(|v| v.as_str()).unwrap_or_default().to_string();
            let symbol = leg_val.pointer("/Security/Symbol").and_then(|v| v.as_str()).unwrap_or_default().to_string();
            let buy_sell = leg_val.get("BuySellCode").and_then(|v| v.as_str()).unwrap_or_default().to_string();
            let quantity = leg_val.get("Quantity").and_then(parse_schwab_decimal).unwrap_or(0.0);
            legs.push(OrderActivityLeg { leg_id, symbol, buy_sell, quantity });
        }
    }
    
    // Fallback: Check OrderAcceptedEvent -> QuoteOnOrderEntry (since OrderAccepted lacks OrderLegs)
    if legs.is_empty() {
        if let Some(quotes_val) = data.pointer("/BaseEvent/OrderAcceptedEvent/QuoteOnOrderEntry").and_then(|v| v.as_array()) {
            for quote_val in quotes_val {
                let leg_id = quote_val.get("SchwabOrderID").and_then(|v| v.as_str()).unwrap_or_default().to_string();
                let symbol = quote_val.get("Symbol").and_then(|v| v.as_str()).unwrap_or_default().to_string();
                let side = quote_val.pointer("/OptionsQuote/PutCallCode").and_then(|v| v.as_str()).unwrap_or_default().to_string();
                legs.push(OrderActivityLeg {
                    leg_id,
                    symbol,
                    buy_sell: side,
                    quantity: 0.0,
                });
            }
        }
    }

    // 2. Extract Limit Price if present
    if let Some(price_val) = data.pointer("/BaseEvent/OrderCreatedEventEquityOrder/Order/Order/AssetOrderEquityOrderLeg/OrderInstruction/ExecutionStrategy/LimitExecutionStrategy/LimitPrice") {
        limit_price = parse_schwab_decimal(price_val);
    }

    // 3. Map status based on Schwab MESSAGE_TYPE values
    match message_type {
        "OrderCreated" => {
            status = "Created".to_string();
        }
        "OrderAccepted" => {
            status = "Open".to_string();
        }
        "CancelAccepted" => {
            status = "Cancelled".to_string();
        }
        "OrderUROutCompleted" => {
            status = "Cancelled".to_string();
        }
        "ExecutionCreated" => {
            // Check trans type to see if it is cancellation ("UROut") or fill
            let trans_type = data.pointer("/BaseEvent/ExecutionCreatedEventExecutionInfo/ExecutionInfo/ExecutionTransType")
                .and_then(|v| v.as_str());
            if trans_type == Some("UROut") {
                status = "Cancelled".to_string();
            } else {
                status = "Filled".to_string();
            }
            
            // Extract single execution leg detail if array parsing is empty
            if legs.is_empty() {
                let leg_id = data.pointer("/BaseEvent/ExecutionCreatedEventExecutionInfo/LegId")
                    .and_then(|v| v.as_str())
                    .unwrap_or_default()
                    .to_string();
                let qty = data.pointer("/BaseEvent/ExecutionCreatedEventExecutionInfo/ExecutionInfo/ExecutionQuantity")
                    .and_then(parse_schwab_decimal)
                    .unwrap_or(0.0);
                legs.push(OrderActivityLeg {
                    leg_id,
                    symbol: String::new(),
                    buy_sell: String::new(),
                    quantity: qty,
                });
            }
        }
        _ => {}
    }

    Some(OrderActivityEvent {
        order_id,
        account_number,
        message_type: message_type.to_string(),
        status,
        legs,
        limit_price,
    })
}

/// Extract and parse all ACCT_ACTIVITY events from raw WebSocket text stream update
pub fn parse_acct_activity_message(txt: &str) -> Vec<OrderActivityEvent> {
    let mut events = Vec::new();
    let val: Value = match serde_json::from_str(txt) {
        Ok(v) => v,
        Err(_) => return events,
    };

    let mut process_item = |content_val: &Value| {
        let msg_type = content_val.get("MESSAGE_TYPE")
            .or_else(|| content_val.get("message-type"))
            .and_then(|v| v.as_str());
        let msg_data = content_val.get("MESSAGE_DATA")
            .or_else(|| content_val.get("message-data"))
            .and_then(|v| v.as_str());

        if let (Some(m_type), Some(m_data)) = (msg_type, msg_data) {
            if let Some(event) = parse_acct_activity_data(m_type, m_data) {
                events.push(event);
            }
        }
    };

    // Case 1: Standalone root ACCT_ACTIVITY message
    let service = val.get("service").and_then(|s| s.as_str()).unwrap_or_default();
    if service == "ACCT_ACTIVITY" {
        if let Some(content_array) = val.get("content").and_then(|c| c.as_array()) {
            for item in content_array {
                process_item(item);
            }
        }
    }

    // Case 2: Wrapped in "data" array list
    if let Some(data_array) = val.get("data").and_then(|d| d.as_array()) {
        for msg in data_array {
            let srv = msg.get("service").and_then(|s| s.as_str()).unwrap_or_default();
            if srv == "ACCT_ACTIVITY" {
                if let Some(content_array) = msg.get("content").and_then(|c| c.as_array()) {
                    for item in content_array {
                        process_item(item);
                    }
                }
            }
        }
    }

    events
}

/// Parse incoming Schwab WebSocket JSON stream updates, updating SPX and options grid data.
/// Returns the new SPX underlying price if updated.
pub fn parse_streaming_message(
    txt: &str,
    grid: &OptionsGrid,
) -> Option<f64> {
    let val: Value = match serde_json::from_str(txt) {
        Ok(v) => v,
        Err(e) => {
            // Some messages could be raw string lists or heartbeats
            debug!("Received non-JSON or control stream update: {} | Error: {:?}", txt, e);
            return None;
        }
    };

    let mut new_spx = None;

    if let Some(data_array) = val.get("data").and_then(|d| d.as_array()) {
        for msg in data_array {
            let service = msg.get("service").and_then(|s| s.as_str()).unwrap_or_default();
            let content_array = match msg.get("content").and_then(|c| c.as_array()) {
                Some(arr) => arr,
                None => continue,
            };

            if service == "LEVELONE_EQUITIES" {
                for entry in content_array {
                    let key = entry.get("key").and_then(|k| k.as_str()).unwrap_or_default();
                    if key == "$SPX" {
                        // Check for Bid, Ask, or Last field updates
                        let price_val = entry.get("3")
                            .or_else(|| entry.get("LAST_PRICE"));
                        if let Some(p_val) = price_val {
                            if let Some(price) = p_val.as_f64() {
                                grid.set_underlying_price(price);
                                grid.update_underlying(price);
                                new_spx = Some(price);
                            }
                        }
                    }
                }
            } else if service == "LEVELONE_OPTIONS" {
                let spx_price = grid.get_underlying_price();
                for entry in content_array {
                    let key = entry.get("key").and_then(|k| k.as_str()).unwrap_or_default();
                    
                    let bid_val = entry.get("2")
                        .or_else(|| entry.get("BID_PRICE"));
                    let ask_val = entry.get("3")
                        .or_else(|| entry.get("ASK_PRICE"));
                        
                    let bid = bid_val.and_then(|b| b.as_f64());
                    let ask = ask_val.and_then(|a| a.as_f64());

                    if bid.is_some() || ask.is_some() {
                        grid.update_option(key, bid, ask, spx_price);
                    }
                }
            }
        }
    }

    new_spx
}


