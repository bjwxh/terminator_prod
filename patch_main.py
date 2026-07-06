import re

with open("terminator_rust/src/main.rs", "r") as f:
    content = f.read()

# 1. Update tracing initialization
old_tracing = """    // Initialize logging & tracing output
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::from_default_env()
                .add_directive(tracing::Level::INFO.into())
        )
        .init();"""

new_tracing = """    // Initialize logging & tracing output
    let ring_logger = terminator_rust::logger::RingLogger::new();
    let ring_layer = ring_logger.clone();
    use tracing_subscriber::layer::SubscriberExt;
    use tracing_subscriber::util::SubscriberInitExt;

    tracing_subscriber::registry()
        .with(tracing_subscriber::EnvFilter::from_default_env().add_directive(tracing::Level::INFO.into()))
        .with(tracing_subscriber::fmt::layer())
        .with(ring_layer)
        .init();"""

content = content.replace(old_tracing, new_tracing)

# 2. Add $VIX.X subscription
old_sub = '    ws_client.subscribe(vec!["$SPX".to_string()], "LEVELONE_EQUITIES").await?;'
new_sub = '    ws_client.subscribe(vec!["$SPX".to_string(), "$VIX.X".to_string()], "LEVELONE_EQUITIES").await?;'
content = content.replace(old_sub, new_sub)

# 3. Add Web UI setup before TUI loop
old_tui = """    info!("Terminator Rust Engine fully initialized! Booting TUI Dashboard...");

    // 11. Run interactive TUI loop in the foreground"""

new_web = """    // 14.5 Web UI setup
    let news_fetcher = terminator_rust::news::NewsFetcher::new();
    let news_fetcher_clone = Arc::clone(&news_fetcher);
    tokio::spawn(async move {
        news_fetcher_clone.run().await;
    });

    let (ws_tx, _) = tokio::sync::broadcast::channel(100);

    let app_state = terminator_rust::web::AppState {
        grid: Arc::clone(&grid),
        supervisor: Arc::clone(&supervisor),
        news: news_fetcher,
        logger: ring_logger,
        ws_tx: ws_tx.clone(),
    };

    let web_port = app_config.web_port;
    tokio::spawn(async move {
        terminator_rust::web::start_server(app_state, web_port).await;
    });

    info!("Terminator Rust Engine fully initialized! Booting TUI Dashboard...");

    // 11. Run interactive TUI loop in the foreground"""

content = content.replace(old_tui, new_web)

with open("terminator_rust/src/main.rs", "w") as f:
    f.write(content)

