use std::sync::Arc;
use tokio::sync::mpsc;
use tracing::{info, error, warn};

use terminator_rust::{
    config::AppConfig,
    token::TokenManager,
    websocket::WebsocketClient,
    grid::OptionsGrid,
    manager::SlidingWindowManager,
    options_chain,
    parser,
    tui,
    execution::ExecutionClient,
    strategy::StrategySupervisor,
};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Initialize logging & tracing output
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::from_default_env()
                .add_directive(tracing::Level::INFO.into())
        )
        .init();

    info!("🦀 Terminator Rust Engine: Starting Phase 2 & 3 Dynamic TUI Pricing Engine...");

    // 1. Load Configurations
    let app_config = match AppConfig::load() {
        Ok(cfg) => cfg,
        Err(e) => {
            error!("Failed to load configuration from environment or .env: {:?}", e);
            return Err(e);
        }
    };
    info!("Configuration loaded successfully.");
    info!("- Target Account: {}", app_config.schwab_account);
    info!("- OTM Offset: {} pts | Buffer Zone: {} pts", app_config.otm_offset, app_config.buffer_zone);

    // 2. Initialize Token Manager
    let token_manager = match TokenManager::new(app_config.clone()) {
        Ok(tm) => tm,
        Err(e) => {
            error!("Failed to initialize TokenManager: {:?}", e);
            return Err(e);
        }
    };

    let token_manager_arc = Arc::new(token_manager);
    
    // Spawn background token auto-refresh loop
    let refresh_tm_clone = Arc::clone(&token_manager_arc);
    tokio::spawn(async move {
        refresh_tm_clone.start_background_loop().await;
    });

    // 4. Fetch 0DTE Options Chain Map at Startup
    let symbol_map = match options_chain::fetch_0dte_option_chain(&token_manager_arc).await {
        Ok(map) => map,
        Err(e) => {
            warn!("Failed to fetch live 0DTE options chain at startup: {:?}", e);
            warn!("Falling back to empty/mock symbol map for dry-run/weekend verification.");
            std::collections::HashMap::new()
        }
    };

    // 5. Initialize concurrent Options Grid
    let grid = Arc::new(OptionsGrid::new(symbol_map.clone()));

    // 6. Setup WebSocket message channel & Client
    let (message_tx, mut message_rx) = mpsc::unbounded_channel::<String>();
    
    let ws_client = Arc::new(WebsocketClient::new(
        app_config.clone(),
        Arc::clone(&token_manager_arc),
        message_tx,
    ));

    // 7. Initialize Sliding Window Manager
    let sliding_window_manager = Arc::new(SlidingWindowManager::new(
        app_config.otm_offset,
        app_config.buffer_zone,
        symbol_map,
        Arc::clone(&ws_client),
    ));

    // 8. Register index subscription ($SPX) in dynamic registry
    ws_client.subscribe(vec!["$SPX".to_string()], "LEVELONE_EQUITIES").await?;

    // 9. Start Schwab WS Stream Supervisor loop in background
    let ws_supervisor_client = Arc::clone(&ws_client);
    tokio::spawn(async move {
        ws_supervisor_client.start_supervisor_loop().await;
    });

    // 10. Create mpsc channel for real-time Schwab account activity events
    let (acct_tx, mut acct_rx) = mpsc::unbounded_channel::<parser::OrderActivityEvent>();

    // 11. Start background WebSocket streaming parser task
    let grid_parser = Arc::clone(&grid);
    let manager_parser = Arc::clone(&sliding_window_manager);
    let acct_tx_clone = acct_tx.clone();
    tokio::spawn(async move {
        while let Some(msg) = message_rx.recv().await {
            // Process streaming market data (Level 1 equities and options)
            if let Some(new_spx) = parser::parse_streaming_message(&msg, &grid_parser) {
                if let Err(e) = manager_parser.handle_index_update(new_spx).await {
                    error!("Error during sliding-window subscription updates: {:?}", e);
                }
            }

            // Process real-time account activity order events
            let acct_events = parser::parse_acct_activity_message(&msg);
            for event in acct_events {
                info!("🔔 Order activity event received: ID={} Type={} Status={}", event.order_id, event.message_type, event.status);
                if let Err(e) = acct_tx_clone.send(event) {
                    error!("Failed to dispatch order event to strategy supervisor channel: {:?}", e);
                }
            }
        }
    });

    // 12. Initialize Execution Client and Strategy Supervisor
    let execution_client = Arc::new(ExecutionClient::new(Arc::clone(&token_manager_arc)));
    let supervisor = Arc::new(StrategySupervisor::new(
        app_config.clone(),
        execution_client,
        Arc::clone(&grid),
    ));

    // 13. Start background Strategy Supervisor / Order Processor task
    let supervisor_event_clone = Arc::clone(&supervisor);
    tokio::spawn(async move {
        info!("🤖 Strategy Supervisor event loop started. Awaiting order activity events...");
        while let Some(event) = acct_rx.recv().await {
            supervisor_event_clone.process_account_event(event).await;
        }
    });

    // 14. Spawn supervisor background tick loop
    let supervisor_loop_clone = Arc::clone(&supervisor);
    tokio::spawn(async move {
        supervisor_loop_clone.run_supervisor_loop().await;
    });

    info!("Terminator Rust Engine fully initialized! Booting TUI Dashboard...");

    // 11. Run interactive TUI loop in the foreground
    if let Err(e) = tui::run_tui_loop(grid, token_manager_arc).await {
        error!("TUI session error or interruption: {:?}", e);
    }

    info!("Terminator Rust Engine shutdown complete.");
    Ok(())
}
