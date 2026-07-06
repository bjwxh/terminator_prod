use std::sync::Arc;
use terminator_rust::config::AppConfig;
use terminator_rust::execution::ExecutionClient;
use terminator_rust::token::TokenManager;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let config = AppConfig::load().unwrap();
    let token_manager = TokenManager::new(config).unwrap();
    let token_manager = Arc::new(token_manager);
    let client = ExecutionClient::new(token_manager.clone());
    let hash = client.get_account_hash().await?;

    // Manual request to print raw json
    let now_utc = chrono::Utc::now();
    let tz: chrono_tz::Tz = "America/Chicago".parse().unwrap();
    let now_ct = now_utc.with_timezone(&tz);

    use chrono::Datelike;
    let start_of_day_ct = now_ct
        .date_naive()
        .and_hms_opt(0, 0, 0)
        .unwrap()
        .and_local_timezone(tz)
        .unwrap() - chrono::Duration::hours(24);
    let from_time = (start_of_day_ct)
        .with_timezone(&chrono::Utc)
        .to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
    let to_time = now_utc.to_rfc3339_opts(chrono::SecondsFormat::Millis, true);

    let filled_trades = client.get_today_filled_orders(&hash, 1.30).await?;
    for trade in filled_trades {
        println!("Trade: {} | Credit: {} | Legs: {:?}", trade.strategy_id, trade.credit, trade.legs);
    }
    Ok(())
}
