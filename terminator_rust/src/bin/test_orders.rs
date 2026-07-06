use terminator_rust::config::AppConfig;
use terminator_rust::execution::ExecutionClient;
use terminator_rust::token::TokenManager;
use std::sync::Arc;

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
    let start_of_day_ct = now_ct.date_naive().and_hms_opt(0, 0, 0).unwrap().and_local_timezone(tz).unwrap();
    let from_time = start_of_day_ct.with_timezone(&chrono::Utc).to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
    let to_time = now_utc.to_rfc3339_opts(chrono::SecondsFormat::Millis, true);

    let url = format!("https://api.schwabapi.com/trader/v1/accounts/{}/orders", hash);
    let token = token_manager.get_access_token();
    let req_client = reqwest::Client::new();
    let response = req_client
        .get(&url)
        .query(&[
            ("fromEnteredTime", from_time.as_str()),
            ("toEnteredTime", to_time.as_str()),
            ("status", "FILLED"),
        ])
        .bearer_auth(token)
        .send()
        .await?;

    let text = response.text().await?;
    println!("Response: {}", text);
    Ok(())
}
