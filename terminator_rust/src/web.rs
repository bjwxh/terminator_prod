use std::sync::Arc;
use std::time::Duration;

use futures::{StreamExt, SinkExt};
use axum::{
    routing::{get, post},
    Router,
    extract::{Path, State, WebSocketUpgrade, ws::{WebSocket, Message}},
    response::{Redirect, IntoResponse},
    http::{StatusCode, HeaderValue, Method},
};
use tower_http::{
    services::ServeDir,
    cors::CorsLayer,
};
use tokio::sync::broadcast;
use serde_json::json;
use tracing::{info, error};

use crate::grid::OptionsGrid;
use crate::strategy::{StrategySupervisor, LegOverride, OptionLeg};
use crate::news::NewsFetcher;
use crate::logger::RingLogger;

fn build_orders_val(legs: &[OptionLeg], order_offset: f64) -> Vec<serde_json::Value> {
    let chunks = crate::strategy::get_smart_chunks(legs);
    let mut orders_val = Vec::new();
    for (i, chunk) in chunks.iter().enumerate() {
        let total_chunk_credit = chunk.iter().map(|l| -(l.quantity as f64) * l.price).sum::<f64>() * 100.0;
        let mut num_units = chunk[0].quantity.abs();
        for leg in chunk.iter().skip(1) {
            num_units = crate::strategy::gcd(num_units, leg.quantity.abs());
        }
        if num_units == 0 { num_units = 1; }
        let signed_mid = total_chunk_credit / (100.0 * num_units as f64);
        let (struct_type, is_credit_structural) = crate::strategy::classify_order_type(chunk);
        let lock_floor = struct_type != "unknown";
        let target = signed_mid + order_offset;
        let (is_credit, price) = if lock_floor {
            let is_cred = is_credit_structural.unwrap_or(true);
            let raw_price = if is_cred { target } else { -target };
            (is_cred, raw_price.max(0.0))
        } else {
            let is_cred = target >= 0.0;
            (is_cred, target.abs())
        };
        let price_ticked = (price / 0.05).round() * 0.05;
        let price_ea = if is_credit { price_ticked } else { -price_ticked };
        let legs_val: Vec<serde_json::Value> = chunk.iter().map(|l| {
            json!({ "symbol": l.symbol, "strike": l.strike, "side": l.side,
                    "quantity": l.quantity, "price": l.price, "delta": l.delta })
        }).collect();

        // Format desc string like "[IRON_CONDOR] SHORT CALL 7540 x1 | LONG CALL 7545 x1 ..."
        let mut leg_texts = Vec::new();
        for l in chunk {
            let side_str = if l.quantity < 0 { "SHORT" } else { "LONG" };
            leg_texts.push(format!("{} {} {} x{}", side_str, l.side, l.strike as i32, l.quantity.abs() / num_units));
        }
        let desc = format!("[{}] {}", struct_type.to_uppercase(), leg_texts.join(" | "));

        orders_val.push(json!({
            "idx": i,
            "legs": legs_val,
            "type": if is_credit { "SELL TO OPEN" } else { "BUY TO OPEN" },
            "qty": num_units,
            "desc": desc,
            "price_ea": price_ea,
            "is_credit": is_credit,
            "order_type": struct_type,
            "lock_floor": lock_floor
        }));
    }
    orders_val
}

#[derive(Clone)]
pub struct AppState {
    pub grid: Arc<OptionsGrid>,
    pub supervisor: Arc<StrategySupervisor>,
    pub news: Arc<NewsFetcher>,
    pub logger: RingLogger,
    pub ws_tx: broadcast::Sender<String>,
}

pub async fn start_server(state: AppState, port: u16) {
    let cors = CorsLayer::new()
        .allow_origin([
            format!("http://localhost:{}", port).parse::<HeaderValue>().unwrap(),
            format!("http://127.0.0.1:{}", port).parse::<HeaderValue>().unwrap(),
        ])
        .allow_methods([Method::GET, Method::POST])
        .allow_headers(tower_http::cors::Any);

    // Serve static files from "./static" folder
    let serve_dir = ServeDir::new("static");

    let app = Router::new()
        .route("/", get(|| async { Redirect::temporary("/index.html") }))
        .route("/ws", get(ws_handler))
        .route("/api/status", get(api_status))
        .route("/api/trading/toggle", post(api_toggle_trading))
        .route("/api/trading/reconnect", post(api_reconnect_broker))
        .route("/api/orders/working", get(api_working_orders))
        .route("/api/orders/:order_id/cancel", post(api_cancel_order))
        .route("/api/orders/:order_id/chase", post(api_chase_order))
        .route("/api/orders/cancel_all", post(api_cancel_all_orders))
        .fallback_service(serve_dir)
        .with_state(state.clone())
        .layer(cors);

    // Start 500ms broadcast state update task
    let state_broadcast = state.clone();
    tokio::spawn(async move {
        let mut tick_count: u64 = 0;
        loop {
            tokio::time::sleep(Duration::from_millis(500)).await;
            
            // Only broadcast if there are active connections
            if state_broadcast.ws_tx.receiver_count() > 0 {
                tick_count += 1;
                let payload = build_state_snapshot(&state_broadcast, tick_count).await;
                if let Ok(json_str) = serde_json::to_string(&payload) {
                    let _ = state_broadcast.ws_tx.send(json_str);
                }
            }
        }
    });

    let listener = match tokio::net::TcpListener::bind(format!("0.0.0.0:{}", port)).await {
        Ok(l) => l,
        Err(e) => {
            error!("Failed to bind Axum web server to port {}: {:?}", port, e);
            return;
        }
    };

    info!("🚀 Standalone Web UI Dashboard serving at http://localhost:{}", port);
    if let Err(e) = axum::serve(listener, app).await {
        error!("Axum server runtime error: {:?}", e);
    }
}

async fn build_state_snapshot(state: &AppState, _tick_count: u64) -> serde_json::Value {
    let now_chi = chrono::Local::now().with_timezone(&chrono_tz::America::Chicago).to_rfc3339();
    let spx_ts = state.grid.get_exchange_ts_ms();
    
    let now_ms = chrono::Utc::now().timestamp_millis();
    let latency = if spx_ts > 0 {
        (now_ms - spx_ts as i64).max(0)
    } else {
        0
    };

    let spx = state.grid.get_spx();
    let vix = state.grid.get_vix();

    let logs = state.logger.get_logs();
    
    // DB status mirrors WS stream health
    let db_status = if spx_ts > 0 && (now_ms - spx_ts as i64) > 120_000 {
        json!({ "status": "Lag", "age_minutes": (now_ms - spx_ts as i64) / 60_000, "should_alert": true })
    } else {
        json!({ "status": "Healthy", "age_minutes": 0, "should_alert": false })
    };

    // Serialized portfolio snapshots
    let live_snap = state.supervisor.live_portfolio.lock().await.snapshot();

    // Option book — OTM window only, matching the SlidingWindowManager subscription range.
    // Calls: [spx, spx + otm_offset], Puts: [spx - otm_offset, spx].
    // Slides automatically as SPX moves; no hard dependency on subscription status.
    let option_book: Vec<serde_json::Value> = {
        let otm_offset = state.supervisor.config.otm_offset;
        let put_min = spx - otm_offset;
        let call_max = spx + otm_offset;

        let mut sorted_quotes: Vec<crate::grid::OptionQuote> = state.grid.quotes.iter()
            .filter(|entry| {
                let strike = entry.value().strike;
                strike >= put_min && strike <= call_max
            })
            .map(|entry| entry.value().clone())
            .collect();
        sorted_quotes.sort_by(|a, b| a.strike.partial_cmp(&b.strike).unwrap_or(std::cmp::Ordering::Equal));

        sorted_quotes.iter().filter_map(|q| {
            // Use the same constant as manager.rs so overlap stays in sync.
            let in_call_range = q.strike >= spx - crate::manager::ATM_OVERLAP_PTS;
            let in_put_range = q.strike <= spx + crate::manager::ATM_OVERLAP_PTS;

            // elapsed().as_secs() is the freshness signal:
            // low (green) = live WS tick, high (red) = bootstrap/stale data
            let (c_bid, c_ask, c_delta, c_updated) = if in_call_range {
                match &q.call {
                    Some(call) => (
                        json!(call.bid), json!(call.ask), json!(call.delta),
                        json!(call.last_update.elapsed().as_secs()),
                    ),
                    None => (serde_json::Value::Null, serde_json::Value::Null, serde_json::Value::Null, serde_json::Value::Null),
                }
            } else {
                (serde_json::Value::Null, serde_json::Value::Null, serde_json::Value::Null, serde_json::Value::Null)
            };

            let (p_delta, p_bid, p_ask, p_updated) = if in_put_range {
                match &q.put {
                    Some(put) => (
                        json!(put.delta), json!(put.bid), json!(put.ask),
                        json!(put.last_update.elapsed().as_secs()),
                    ),
                    None => (serde_json::Value::Null, serde_json::Value::Null, serde_json::Value::Null, serde_json::Value::Null),
                }
            } else {
                (serde_json::Value::Null, serde_json::Value::Null, serde_json::Value::Null, serde_json::Value::Null)
            };

            if c_bid.is_null() && p_bid.is_null() {
                return None;
            }

            Some(json!({
                "strike": q.strike,
                "call_updated_secs": c_updated,
                "call_bid": c_bid,
                "call_ask": c_ask,
                "call_delta": c_delta,
                "put_delta": p_delta,
                "put_bid": p_bid,
                "put_ask": p_ask,
                "put_updated_secs": p_updated,
            }))
        }).collect()
    };
    
    // live_portfolio = sim (strategy trades, used by reconciliation as ground truth).
    // broker_portfolio = live (only trades physically sent to the exchange).
    let broker_snap = state.supervisor.broker_portfolio.lock().await.snapshot();
    let sim_payload = json!(live_snap);
    let live_payload = json!(broker_snap);

    // Sub-strategies
    let mut strategies_data = serde_json::Map::new();
    let supervisor = &state.supervisor;
    let strats = supervisor.sub_strategies.lock().await;
    for (sid, s) in &*strats {
        let s_port = s.portfolio.lock().await;
        
        let positions: Vec<serde_json::Value> = s_port.positions.iter().map(|p| {
            json!({
                "symbol": p.symbol, "strike": p.strike, "side": p.side, "qty": p.quantity,
                "pnl": p.current_day_pnl, "sim_pnl": p.current_day_pnl, "delta": p.delta,
                "bid": p.bid, "ask": p.ask
            })
        }).collect();
        
        let history: Vec<serde_json::Value> = s_port.trades.iter().rev().take(50).map(|t| {
            json!({
                "ts": t.timestamp, "purpose": t.purpose, "credit": t.credit,
                "legs": t.legs.iter().map(|l| json!({"symbol": l.symbol, "qty": l.quantity, "strike": l.strike, "side": l.side})).collect::<Vec<_>>()
            })
        }).collect();

        let s_data = json!({
            "pnl": s_port.net_pnl(),
            "traded": s.has_traded_today,
            "positions": positions,
            "history": history,
        });
        
        strategies_data.insert(sid.clone(), s_data);
    }

    // Working orders
    let working_orders = state.supervisor.working_orders.lock().await.clone();

    // Pending confirmation trade
    let pending_trade_val = match &*state.supervisor.pending_trade.lock().await {
        Some(t) => {
            let orders_val = build_orders_val(&t.trade.legs, state.supervisor.config.order_offset);

            let legs_val: Vec<serde_json::Value> = t.trade.legs.iter().map(|l| {
                json!({
                    "symbol": l.symbol, "strike": l.strike, "side": l.side, "quantity": l.quantity,
                    "price": l.price, "delta": l.delta
                })
            }).collect();

            json!({
                "strat_id": t.strat_id,
                "trade": {
                    "timestamp": t.trade.timestamp,
                    "credit": t.trade.credit,
                    "commission": t.trade.commission,
                    "purpose": t.trade.purpose,
                    "legs": legs_val,
                    "orders": orders_val
                }
            })
        }
        None => serde_json::Value::Null,
    };

    // Sina news poll updates
    let news_payload = state.news.get_latest(50).await;

    json!({
        "type": "state_update",
        "state": {
            "ts": now_chi,
            "exchange_ts": spx_ts,
            "latency_ms": latency,
            "spx": spx,
            "vix": vix,
            "server_name": state.supervisor.server_name,
            "status": state.supervisor.status.lock().await.clone(),
            "broker_connected": state.supervisor.broker_connected.load(std::sync::atomic::Ordering::Relaxed),
            "trading_enabled": state.supervisor.trading_enabled.load(std::sync::atomic::Ordering::Relaxed),
            "db_status": db_status,
            "heartbeat_failures": state.supervisor.heartbeat_failures.load(std::sync::atomic::Ordering::Relaxed),
            "working_orders": working_orders,
            "sim": sim_payload,
            "live": live_payload,
            "strategies": strategies_data,
            "logs": logs,
            "news": news_payload,
            "option_book": option_book,
            "pending_trade": pending_trade_val,
            "version": "0.1.0"
        }
    })
}

async fn ws_handler(
    ws: WebSocketUpgrade,
    State(state): State<AppState>,
) -> impl IntoResponse {
    ws.on_upgrade(|socket| handle_ws_socket(socket, state))
}

async fn handle_ws_socket(socket: WebSocket, state: AppState) {
    let (mut sender, mut receiver) = socket.split();
    let mut ws_rx = state.ws_tx.subscribe();

    // 1. Send history_init immediately on connect
    let history_list: Vec<serde_json::Value> = {
        let history_lock = state.supervisor.session_history.lock().await;
        history_lock.iter().map(|hp| {
            json!({
                "ts": hp.ts,
                "spx": hp.spx,
                "live_pnl": hp.live_pnl,
                "sim_pnl": hp.sim_pnl,
            })
        }).collect()
    };

    let news = state.news.get_latest(50).await;
    
    let config_payload = json!({
        "account_number": state.supervisor.config.account_id,
        "server_name": state.supervisor.config.server_name,
        "web_port": state.supervisor.config.web_port,
        "dry_run": state.supervisor.config.dry_run,
        "trading_hours": format!("{} - {}", state.supervisor.config.start_time, state.supervisor.config.end_time),
        "otm_offset": state.supervisor.config.otm_offset,
        "buffer_zone": state.supervisor.config.buffer_zone,
        "init_wing_delta": state.supervisor.config.init_wing_delta,
        "initial_sum_delta": state.supervisor.config.initial_sum_delta,
        "rebalance_threshold": state.supervisor.config.rebalance_threshold,
        "min_credit": state.supervisor.config.min_credit,
        "min_long_delta": state.supervisor.config.min_long_delta,
        "max_spread_diff": state.supervisor.config.max_spread_diff,
        "default_unit_size": state.supervisor.config.default_unit_size,
        "commission_per_contract": state.supervisor.config.commission_per_contract,
        "order_offset": state.supervisor.config.order_offset,
        "order_auto_execute_timeout": state.supervisor.config.order_auto_execute_timeout,
        "portfolio_sessions": format!("{} - {} every {}min",
            state.supervisor.config.portfolio_start_time,
            state.supervisor.config.portfolio_end_time,
            state.supervisor.config.portfolio_interval_minutes),
        "bootstrap_mode": state.supervisor.config.bootstrap_mode,
        "stale_guard_min_price": state.supervisor.config.stale_guard_min_price,
    });

    let init_payload = json!({
        "type": "history_init",
        "history": history_list,
        "news": news,
        "config": config_payload
    });

    if let Ok(init_str) = serde_json::to_string(&init_payload) {
        let _ = sender.send(Message::Text(init_str)).await;
    }

    // 2. Re-send pending trade signal if it exists (so UI modal pops up on refresh)
    let pending_trade_opt = {
        let lock = state.supervisor.pending_trade.lock().await;
        lock.clone()
    };
    if let Some(t) = pending_trade_opt {
        let legs_val: Vec<serde_json::Value> = t.trade.legs.iter().map(|l| {
            json!({
                "symbol": l.symbol, "strike": l.strike, "side": l.side, "quantity": l.quantity,
                "price": l.price, "delta": l.delta
            })
        }).collect();

        // Build chunked orders — same logic as build_state_snapshot so the reconnect modal
        // renders grouped multi-leg cards instead of individual legs.
        let orders_val = build_orders_val(&t.trade.legs, state.supervisor.config.order_offset);

        let reconnect_payload = json!({
            "type": "trade_signal",
            "strat_id": t.strat_id,
            "trade": {
                "timestamp": t.trade.timestamp,
                "credit": t.trade.credit,
                "commission": t.trade.commission,
                "purpose": t.trade.purpose,
                "legs": legs_val,
                "orders": orders_val
            },
            "is_reconnect": true
        });

        if let Ok(rec_str) = serde_json::to_string(&reconnect_payload) {
            let _ = sender.send(Message::Text(rec_str)).await;
        }
    }

    // Spawn write forwarding task
    let mut write_task = tokio::spawn(async move {
        while let Ok(msg) = ws_rx.recv().await {
            if sender.send(Message::Text(msg)).await.is_err() {
                break;
            }
        }
    });

    // Spawn read processing task
    let state_clone = state.clone();
    let mut read_task = tokio::spawn(async move {
        while let Some(Ok(Message::Text(text))) = receiver.next().await {
            if let Ok(msg) = serde_json::from_str::<serde_json::Value>(&text) {
                if let Some(action) = msg.get("action").and_then(|v| v.as_str()) {
                    match action {
                        "confirm_trade" => {
                            if let Some(strat_id) = msg.get("strat_id").and_then(|v| v.as_str()) {
                                // Parse overrides: list of {idx, price_ea}
                                let mut leg_overrides = Vec::new();
                                if let Some(arr) = msg.get("overrides").and_then(|v| v.as_array()) {
                                    for item in arr {
                                        if let (Some(idx), Some(price)) = (item.get("idx").and_then(|v| v.as_u64()), item.get("price_ea").and_then(|v| v.as_f64())) {
                                            leg_overrides.push(LegOverride { idx: idx as usize, price_ea: price });
                                        }
                                    }
                                }

                                info!("Manual trade confirmation received via WS for strategy {}", strat_id);
                                let _ = state_clone.supervisor.confirm_trade(strat_id, leg_overrides).await;

                                // Broadcast close_modal to all clients
                                let close_msg = json!({
                                    "type": "trade_action",
                                    "action": "close_modal",
                                    "strat_id": strat_id
                                });
                                if let Ok(c_str) = serde_json::to_string(&close_msg) {
                                    let _ = state_clone.ws_tx.send(c_str);
                                }
                            }
                        }
                        "dismiss_trade" => {
                            if let Some(strat_id) = msg.get("strat_id").and_then(|v| v.as_str()) {
                                info!("Manual trade dismissal received via WS for strategy {}", strat_id);
                                state_clone.supervisor.dismiss_trade(strat_id).await;

                                // Broadcast close_modal to all clients
                                let close_msg = json!({
                                    "type": "trade_action",
                                    "action": "close_modal",
                                    "strat_id": strat_id
                                });
                                if let Ok(c_str) = serde_json::to_string(&close_msg) {
                                    let _ = state_clone.ws_tx.send(c_str);
                                }
                            }
                        }
                        "toggle_trade_pause" => {
                            let is_paused = msg.get("is_paused").and_then(|v| v.as_bool()).unwrap_or(false);
                            info!("Trade timer execution pause toggled: {}", is_paused);
                            state_clone.supervisor.set_timer_paused(is_paused);

                            // Broadcast pause sync to all clients
                            let pause_msg = json!({
                                "type": "trade_action",
                                "action": "pause_sync",
                                "is_paused": is_paused
                            });
                            if let Ok(p_str) = serde_json::to_string(&pause_msg) {
                                let _ = state_clone.ws_tx.send(p_str);
                            }
                        }
                        _ => {}
                    }
                }
            }
        }
    });

    // Wait until one of the tasks finishes
    tokio::select! {
        _ = &mut write_task => read_task.abort(),
        _ = &mut read_task => write_task.abort(),
    }
}

// REST endpoints implementation

async fn api_status(State(state): State<AppState>) -> impl IntoResponse {
    let now_chi = chrono::Local::now().with_timezone(&chrono_tz::America::Chicago).to_rfc3339();
    let res = json!({
        "ts": now_chi,
        "status": *state.supervisor.status.lock().await,
        "is_running": true,
        "broker_connected": state.supervisor.broker_connected.load(std::sync::atomic::Ordering::Relaxed),
        "trading_enabled": state.supervisor.trading_enabled.load(std::sync::atomic::Ordering::Relaxed),
        "heartbeat_failures": state.supervisor.heartbeat_failures.load(std::sync::atomic::Ordering::Relaxed)
    });
    (StatusCode::OK, axum::Json(res))
}

async fn api_toggle_trading(State(state): State<AppState>) -> impl IntoResponse {
    let enabled = state.supervisor.toggle_trading_enabled();
    let status_str = if enabled { "enabled" } else { "disabled" };
    let res = json!({
        "status": format!("Trading {}", status_str),
        "enabled": enabled
    });
    (StatusCode::OK, axum::Json(res))
}

async fn api_reconnect_broker(State(_state): State<AppState>) -> impl IntoResponse {
    // Rust WebSocket reconnect wrapper
    info!("Manual broker reconnect triggered via REST API.");
    let res = json!({ "status": "Reconnect not yet implemented in Rust backend" });
    (StatusCode::OK, axum::Json(res))
}

async fn api_working_orders(State(state): State<AppState>) -> impl IntoResponse {
    let orders = state.supervisor.working_orders.lock().await.clone();
    (StatusCode::OK, axum::Json(orders))
}

async fn api_cancel_order(
    State(state): State<AppState>,
    Path(order_id): Path<String>,
) -> impl IntoResponse {
    info!("Manual order cancellation requested via REST for order ID {}", order_id);
    let hash = {
        let h = state.supervisor.account_hash.lock().await;
        h.clone().unwrap_or_default()
    };
    if hash.is_empty() {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            axum::Json(json!({ "detail": "Account hash not yet resolved, retry in a moment" })),
        );
    }
    match state.supervisor.execution_client.cancel_order(&hash, &order_id).await {
        Ok(_) => (StatusCode::OK, axum::Json(json!({ "msg": format!("Order {} cancel requested", order_id) }))),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, axum::Json(json!({ "detail": format!("Cancel failed: {:?}", e) }))),
    }
}

async fn api_chase_order(
    State(state): State<AppState>,
    Path(order_id): Path<String>,
) -> impl IntoResponse {
    info!("Manual order chasing/pricing improvement requested via REST for order ID {}", order_id);
    let hash = {
        let h = state.supervisor.account_hash.lock().await;
        h.clone().unwrap_or_default()
    };
    if hash.is_empty() {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            axum::Json(json!({ "detail": "Account hash not yet resolved, retry in a moment" })),
        );
    }
    match state.supervisor.execution_client.chase_order(&hash, &order_id).await {
        Ok(success) => {
            if success {
                (StatusCode::OK, axum::Json(json!({ "msg": format!("Order {} chase/improvement requested", order_id) })))
            } else {
                (StatusCode::INTERNAL_SERVER_ERROR, axum::Json(json!({ "detail": "Chase failed to match quotes" })))
            }
        }
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, axum::Json(json!({ "detail": format!("Chase failed: {:?}", e) }))),
    }
}

async fn api_cancel_all_orders(State(state): State<AppState>) -> impl IntoResponse {
    info!("Manual cancellation of ALL working orders requested via REST.");
    let hash = {
        let h = state.supervisor.account_hash.lock().await;
        h.clone().unwrap_or_default()
    };
    if hash.is_empty() {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            axum::Json(json!({ "detail": "Account hash not yet resolved, retry in a moment" })),
        );
    }
    match state.supervisor.execution_client.cancel_all_orders(&hash).await {
        Ok(ids) => (StatusCode::OK, axum::Json(json!({ "success": true, "msg": format!("Cancelled {} orders", ids.len()), "ids": ids }))),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, axum::Json(json!({ "success": false, "msg": format!("Cancel all failed: {:?}", e) }))),
    }
}
