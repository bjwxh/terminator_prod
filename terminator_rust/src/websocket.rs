use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;
use anyhow::{Context, Result};
use futures::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use tokio::sync::{mpsc, Mutex};
use tokio::time::sleep;
use tokio_tungstenite::{connect_async, tungstenite::protocol::Message};
use tracing::{info, error, warn, debug};

use crate::config::AppConfig;
use crate::token::TokenManager;

// ============================================================================
// Schwab JSON API Structures
// ============================================================================

#[derive(Debug, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
pub struct UserPreferences {
    pub streamer_info: Vec<StreamerInfo>,
}

#[derive(Debug, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
pub struct StreamerInfo {
    pub streamer_socket_url: String,
    pub schwab_client_customer_id: String,
    pub schwab_client_correl_id: String,
    pub schwab_client_channel: String,
    pub schwab_client_function_id: String,
}

#[derive(Debug, Serialize)]
pub struct WsRequestContainer {
    pub requests: Vec<WsRequest>,
}

#[derive(Debug, Serialize)]
pub struct WsRequest {
    pub service: String,
    pub requestid: String,
    pub command: String,
    #[serde(rename = "SchwabClientCustomerId")]
    pub customer_id: String,
    #[serde(rename = "SchwabClientCorrelId")]
    pub correl_id: String,
    pub parameters: serde_json::Value,
}

// ============================================================================
// Streaming WebSocket Client
// ============================================================================

pub struct WebsocketClient {
    config: AppConfig,
    token_manager: Arc<TokenManager>,
    // Tracks currently active subscriptions (Option Symbols and Indices)
    subscribed_symbols: Arc<Mutex<HashSet<String>>>,
    // Sender channel to forward messages to our consumer loops
    message_tx: mpsc::UnboundedSender<String>,
    // Request ID counter for WS command correlation
    request_counter: Arc<Mutex<u64>>,
    // Active command channel sender for dynamic subscriptions
    active_cmd_tx: Arc<Mutex<Option<mpsc::UnboundedSender<Message>>>>,
    // Active streamer configuration details for dynamic headers
    active_streamer_info: Arc<Mutex<Option<StreamerInfo>>>,
    // Persistent HTTP client for REST calls
    http_client: reqwest::Client,
}

impl WebsocketClient {
    pub fn new(
        config: AppConfig,
        token_manager: Arc<TokenManager>,
        message_tx: mpsc::UnboundedSender<String>,
    ) -> Self {
        let http_client = reqwest::Client::builder()
            .timeout(Duration::from_secs(10))
            .build()
            .expect("Failed to build HTTP client for WebsocketClient");

        Self {
            config,
            token_manager,
            subscribed_symbols: Arc::new(Mutex::new(HashSet::new())),
            message_tx,
            request_counter: Arc::new(Mutex::new(0)),
            active_cmd_tx: Arc::new(Mutex::new(None)),
            active_streamer_info: Arc::new(Mutex::new(None)),
            http_client,
        }
    }

    /// Exposes thread-safe pointer to currently active subscribed symbols list.
    pub fn get_subscribed_symbols(&self) -> Arc<Mutex<HashSet<String>>> {
        Arc::clone(&self.subscribed_symbols)
    }

    /// Fetches streamer parameters from User Preferences REST API
    async fn fetch_streamer_info(&self) -> Result<StreamerInfo> {
        let access_token = self.token_manager.get_access_token();
        let client = &self.http_client;
        
        info!("Fetching streamer credentials from User Preferences API...");
        let response = client
            .get("https://api.schwabapi.com/v1/userPreference")
            .bearer_auth(access_token)
            .send()
            .await
            .context("Failed HTTP request to User Preferences endpoint")?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            error!("User Preference API returned error: Status = {}, Body = {}", status, body);
            anyhow::bail!("User preferences error: {}", status);
        }

        let prefs: UserPreferences = response.json().await
            .context("Failed to deserialize UserPreferences JSON response")?;

        let streamer = prefs.streamer_info.first()
            .context("Empty streamer_info returned from Schwab preferences")?
            .clone();

        Ok(streamer)
    }

    /// Increment and retrieve next request ID as String
    async fn next_request_id(&self) -> String {
        let mut count = self.request_counter.lock().await;
        *count += 1;
        count.to_string()
    }

    /// Primary dynamic entry point to subscribe to options/indices.
    /// Can be safely called from the strategy or sliding-window controller threads.
    pub async fn subscribe(&self, symbols: Vec<String>, service: &str) -> Result<()> {
        if symbols.is_empty() {
            return Ok(());
        }

        info!("Registering subscription for {} symbols on service {}...", symbols.len(), service);
        let mut registry = self.subscribed_symbols.lock().await;
        let mut new_symbols = Vec::new();
        for sym in &symbols {
            if registry.insert(sym.clone()) {
                new_symbols.push(sym.clone());
            }
        }

        if !new_symbols.is_empty() {
            if let Some(tx) = self.active_cmd_tx.lock().await.as_ref() {
                let req_id = self.next_request_id().await;
                if let Some(streamer) = self.active_streamer_info.lock().await.as_ref() {
                    let sub_req = WsRequest {
                        service: service.to_string(),
                        requestid: req_id,
                        command: "SUBS".to_string(),
                        customer_id: streamer.schwab_client_customer_id.clone(),
                        correl_id: streamer.schwab_client_correl_id.clone(),
                        parameters: serde_json::json!({
                            "keys": new_symbols.join(","),
                            "fields": if service == "LEVELONE_OPTIONS" { "0,2,3" } else { "0,1,2,3,34,35" }
                        }),
                    };
                    let payload = WsRequestContainer { requests: vec![sub_req] };
                    let msg_str = serde_json::to_string(&payload)?;
                    let _ = tx.send(Message::Text(msg_str.into()));
                    info!("Dynamically sent subscription request for: {:?}", new_symbols);
                }
            }
        }
        Ok(())
    }

    /// Dynamically unsubscribe from symbols.
    pub async fn unsubscribe(&self, symbols: Vec<String>, service: &str) -> Result<()> {
        if symbols.is_empty() {
            return Ok(());
        }

        info!("Unregistering subscription for {} symbols on service {}...", symbols.len(), service);
        let mut registry = self.subscribed_symbols.lock().await;
        let mut removed_symbols = Vec::new();
        for sym in &symbols {
            if registry.remove(sym) {
                removed_symbols.push(sym.clone());
            }
        }

        if !removed_symbols.is_empty() {
            if let Some(tx) = self.active_cmd_tx.lock().await.as_ref() {
                let req_id = self.next_request_id().await;
                if let Some(streamer) = self.active_streamer_info.lock().await.as_ref() {
                    let sub_req = WsRequest {
                        service: service.to_string(),
                        requestid: req_id,
                        command: "UNSUBS".to_string(),
                        customer_id: streamer.schwab_client_customer_id.clone(),
                        correl_id: streamer.schwab_client_correl_id.clone(),
                        parameters: serde_json::json!({
                            "keys": removed_symbols.join(","),
                        }),
                    };
                    let payload = WsRequestContainer { requests: vec![sub_req] };
                    let msg_str = serde_json::to_string(&payload)?;
                    let _ = tx.send(Message::Text(msg_str.into()));
                    info!("Dynamically sent unsubscription request for: {:?}", removed_symbols);
                }
            }
        }
        Ok(())
    }

    /// Main supervisor connection loop. Manages auto-reconnects with exponential backoff.
    pub async fn start_supervisor_loop(self: Arc<Self>) {
        let mut backoff = Duration::from_secs(1);
        let max_backoff = Duration::from_secs(60);

        info!("Schwab Stream Supervisor loop started.");

        loop {
            match self.connect_and_stream().await {
                Ok(_) => {
                    info!("Stream session exited normally. Reconnecting...");
                    backoff = Duration::from_secs(1); // Reset backoff on successful session
                }
                Err(e) => {
                    error!("WebSocket stream connection error: {:?}", e);
                    warn!("Attempting connection recovery in {}s...", backoff.as_secs());
                    sleep(backoff).await;
                    
                    // Exponential backoff with jitter
                    backoff = std::cmp::min(backoff * 2, max_backoff);
                }
            }
            
            // On disconnection, clear the active command channel sender and streamer info
            {
                let mut cmd_tx_lock = self.active_cmd_tx.lock().await;
                *cmd_tx_lock = None;
                let mut info_lock = self.active_streamer_info.lock().await;
                *info_lock = None;
            }
        }
    }

    /// Connect, authenticate, perform handshake, and pipe updates to consumers
    async fn connect_and_stream(&self) -> Result<()> {
        // 1. Resolve socket credentials and URL
        let streamer = self.fetch_streamer_info().await?;
        
        info!("Opening secure WebSocket connection to {}...", streamer.streamer_socket_url);
        let (ws_stream, _) = connect_async(&streamer.streamer_socket_url)
            .await
            .context("Failed secure WebSocket connection handshake")?;

        let (mut write_half, mut read_half) = ws_stream.split();
        info!("WebSocket connection handshake successful!");

        // 2. Perform ADMIN LOGIN negotiation
        let req_id = self.next_request_id().await;
        let login_params = serde_json::json!({
            "Authorization": self.token_manager.get_access_token(),
            "SchwabClientChannel": streamer.schwab_client_channel,
            "SchwabClientFunctionId": streamer.schwab_client_function_id,
        });

        let login_req = WsRequest {
            service: "ADMIN".to_string(),
            requestid: req_id,
            command: "LOGIN".to_string(),
            customer_id: streamer.schwab_client_customer_id.clone(),
            correl_id: streamer.schwab_client_correl_id.clone(),
            parameters: login_params,
        };

        let payload = WsRequestContainer {
            requests: vec![login_req],
        };

        let msg_str = serde_json::to_string(&payload)?;
        debug!("Sending WS ADMIN LOGIN request payload...");
        write_half.send(Message::Text(msg_str.into())).await
            .context("Failed to transmit WebSocket ADMIN LOGIN payload")?;

        // 3. Await LOGIN response confirmation
        if let Some(msg_res) = read_half.next().await {
            let msg = msg_res.context("Error reading login handshake response")?;
            if let Message::Text(txt) = msg {
                debug!("Received initial WebSocket response: {}", txt);
                // Perform quick validation to verify code is 0 (Success)
                if !txt.contains(r#""code":0"#) && !txt.contains(r#""code": 0"#) {
                    anyhow::bail!("WebSocket login rejected or failed: {}", txt);
                }
            } else {
                anyhow::bail!("Unexpected initial response type during handshake: {:?}", msg);
            }
        } else {
            anyhow::bail!("WebSocket connection closed during ADMIN login handshake");
        }

        info!("Streamer login successful! Sending initial subscriptions...");

        // Subscribe to real-time ACCT_ACTIVITY feed using dynamic correlation ID
        let acct_req_id = self.next_request_id().await;
        let acct_req = WsRequest {
            service: "ACCT_ACTIVITY".to_string(),
            requestid: acct_req_id,
            command: "SUBS".to_string(),
            customer_id: streamer.schwab_client_customer_id.clone(),
            correl_id: streamer.schwab_client_correl_id.clone(),
            parameters: serde_json::json!({
                "keys": streamer.schwab_client_correl_id.clone(),
                "fields": "0,1,2,3"
            }),
        };
        let acct_payload = WsRequestContainer { requests: vec![acct_req] };
        write_half.send(Message::Text(serde_json::to_string(&acct_payload)?.into())).await?;
        info!("Subscribed to real-time ACCT_ACTIVITY feed for account correl ID: {}", streamer.schwab_client_correl_id);

        // 4. Send dynamic subscriptions registered in our dynamic registry
        let active_subs = {
            let registry = self.subscribed_symbols.lock().await;
            registry.clone()
        };

        if !active_subs.is_empty() {
            // Split options and indices ($SPX, $VIX)
            let mut options = Vec::new();
            let mut equities = Vec::new();

            for sym in active_subs {
                if sym.starts_with('$') {
                    equities.push(sym);
                } else {
                    options.push(sym);
                }
            }

            // Subscribe to Level 1 Equities ($SPX, $VIX)
            if !equities.is_empty() {
                let req_id = self.next_request_id().await;
                let sub_req = WsRequest {
                    service: "LEVELONE_EQUITIES".to_string(),
                    requestid: req_id,
                    command: "SUBS".to_string(),
                    customer_id: streamer.schwab_client_customer_id.clone(),
                    correl_id: streamer.schwab_client_correl_id.clone(),
                    parameters: serde_json::json!({
                        "keys": equities.join(","),
                        "fields": "0,1,2,3,34,35" // SYMBOL, BID, ASK, LAST, QUOTE_TIME, TRADE_TIME
                    }),
                };
                let payload = WsRequestContainer { requests: vec![sub_req] };
                write_half.send(Message::Text(serde_json::to_string(&payload)?.into())).await?;
                info!("Subscribed to real-time index feeds: {:?}", equities);
            }

            // Subscribe to Level 1 Options (SPXW Strikes)
            if !options.is_empty() {
                let req_id = self.next_request_id().await;
                let sub_req = WsRequest {
                    service: "LEVELONE_OPTIONS".to_string(),
                    requestid: req_id,
                    command: "SUBS".to_string(),
                    customer_id: streamer.schwab_client_customer_id.clone(),
                    correl_id: streamer.schwab_client_correl_id.clone(),
                    parameters: serde_json::json!({
                        "keys": options.join(","),
                        "fields": "0,2,3" // SYMBOL, BID_PRICE, ASK_PRICE
                    }),
                };
                let payload = WsRequestContainer { requests: vec![sub_req] };
                write_half.send(Message::Text(serde_json::to_string(&payload)?.into())).await?;
                info!("Subscribed to {} real-time options strikes.", options.len());
            }
        }

        // Create the session command channel
        let (session_tx, mut session_rx) = mpsc::unbounded_channel::<Message>();
        {
            let mut cmd_tx_lock = self.active_cmd_tx.lock().await;
            *cmd_tx_lock = Some(session_tx);
            let mut info_lock = self.active_streamer_info.lock().await;
            *info_lock = Some(streamer.clone());
        }

        info!("Entering main streaming select loop...");

        // 5. Main concurrent select loop
        loop {
            tokio::select! {
                Some(msg_res) = read_half.next() => {
                    let msg = msg_res?;
                    match msg {
                        Message::Text(txt) => {
                            let _ = self.message_tx.send(txt.to_string());
                        }
                        Message::Binary(bin) => {
                            if let Ok(txt) = String::from_utf8(bin) {
                                let _ = self.message_tx.send(txt);
                            }
                        }
                        Message::Ping(_) => {
                            debug!("Ping received, Pong sent.");
                        }
                        Message::Close(_) => {
                            warn!("Streamer sent WebSocket close frame. Reconnecting...");
                            break;
                        }
                        _ => {}
                    }
                }
                Some(cmd) = session_rx.recv() => {
                    write_half.send(cmd).await
                        .context("Failed to forward outgoing command to WebSocket")?;
                }
                else => {
                    break;
                }
            }
        }

        Ok(())
    }
}

