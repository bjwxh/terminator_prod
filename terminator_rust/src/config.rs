use anyhow::Context;
use chrono::NaiveTime;
use serde::Deserialize;
use std::path::PathBuf;

#[derive(Debug, Clone)]
pub struct AppConfig {
    pub schwab_token_path: PathBuf,
    pub schwab_account: String,
    pub schwab_api_key: String,
    pub schwab_api_secret: String,
    pub schwab_callback_url: String,
    pub dry_run: bool,
    pub initial_sum_delta: f64,
    pub init_wing_delta: f64,
    pub rebalance_threshold: f64,
    pub long_leg_rebalance_delta_threshold: f64,
    pub min_credit: f64,
    pub max_spread_diff: f64,
    pub commission_per_contract: f64,
    pub order_offset: f64,
    pub stale_guard_min_price: f64,
    pub default_unit_size: i32,
    pub account_id: String,
    pub server_name: String,
    pub min_long_delta: f64,
    pub order_auto_execute_timeout: u32,
    pub bootstrap_mode: String,
    pub db_path: String,
    pub portfolio_start_time: NaiveTime,
    pub portfolio_end_time: NaiveTime,
    pub portfolio_interval_minutes: u32,
    pub start_time: NaiveTime,
    pub end_time: NaiveTime,
    pub otm_offset: f64,
    pub buffer_zone: f64,
    pub web_port: u16,
    pub email_alert_delay_seconds: u32,
    pub stale_quote_threshold_secs: u64,
}

#[derive(Deserialize)]
struct SchwabApiFile {
    api_key: String,
    api_secret: String,
    callback_url: String,
}

fn expand_tilde(path: &str) -> PathBuf {
    if path.starts_with("~/") {
        if let Ok(home) = std::env::var("HOME") {
            return PathBuf::from(home).join(&path[2..]);
        }
    }
    PathBuf::from(path)
}

#[derive(Deserialize)]
struct ConfigJson {
    credentials_file: Option<String>,
    token_file: Option<String>,
    initial_sum_delta: f64,
    init_wing_delta: f64,
    rebalance_threshold: f64,
    long_leg_rebalance_delta_threshold: f64,
    min_credit: f64,
    max_spread_diff: f64,
    commission_per_contract: f64,
    order_offset: f64,
    stale_guard_min_price: f64,
    default_unit_size: i32,
    account_id: String,
    server_name: String,
    min_long_delta: f64,
    order_auto_execute_timeout: u32,
    bootstrap_mode: String,
    db_path: String,
    
    portfolio_start_time: String,
    portfolio_end_time: String,
    portfolio_interval_minutes: u32,
    start_time: String,
    end_time: String,
    
    otm_offset: f64,
    buffer_zone: f64,
    dry_run: bool,
    web_port: u16,
    email_alert_delay_seconds: Option<u32>,
    stale_quote_threshold_secs: Option<u64>,
}

impl AppConfig {
    pub fn load() -> anyhow::Result<Self> {
        let _ = dotenvy::dotenv();

        let config_path = std::env::var("CONFIG_PATH")
            .unwrap_or_else(|_| "config.json".to_string());
            
        let config_content = std::fs::read_to_string(&config_path)
            .with_context(|| format!("Failed to read config file at {}", config_path))?;
            
        let config_json: ConfigJson = serde_json::from_str(&config_content)
            .context("Failed to parse config.json")?;

        let schwab_api_path = if let Some(ref path) = config_json.credentials_file {
            if !path.is_empty() && path != "/path/to/schwab_api.json" {
                expand_tilde(path)
            } else {
                PathBuf::from(
                    std::env::var("SCHWAB_API_PATH")
                        .context("SCHWAB_API_PATH environment variable is not set")?
                )
            }
        } else {
            PathBuf::from(
                std::env::var("SCHWAB_API_PATH")
                    .context("SCHWAB_API_PATH environment variable is not set")?
            )
        };

        let schwab_token_path = if let Some(ref path) = config_json.token_file {
            if !path.is_empty() && path != "/path/to/schwab_token.json" {
                expand_tilde(path)
            } else {
                PathBuf::from(
                    std::env::var("SCHWAB_TOKEN_PATH")
                        .context("SCHWAB_TOKEN_PATH environment variable is not set")?
                )
            }
        } else {
            PathBuf::from(
                std::env::var("SCHWAB_TOKEN_PATH")
                    .context("SCHWAB_TOKEN_PATH environment variable is not set")?
            )
        };

        let schwab_account = std::env::var("SCHWAB_ACCOUNT")
            .unwrap_or_else(|_| config_json.account_id.clone());

        let api_file: SchwabApiFile = {
            let content = std::fs::read_to_string(&schwab_api_path)
                .with_context(|| format!("Failed to read Schwab API file at {:?}", schwab_api_path))?;
            serde_json::from_str(&content)
                .context("Failed to parse Schwab API JSON")?
        };

        Ok(Self {
            schwab_token_path,
            schwab_account,
            schwab_api_key: api_file.api_key,
            schwab_api_secret: api_file.api_secret,
            schwab_callback_url: api_file.callback_url,
            
            initial_sum_delta: config_json.initial_sum_delta,
            init_wing_delta: config_json.init_wing_delta,
            rebalance_threshold: config_json.rebalance_threshold,
            long_leg_rebalance_delta_threshold: config_json.long_leg_rebalance_delta_threshold,
            min_credit: config_json.min_credit,
            max_spread_diff: config_json.max_spread_diff,
            commission_per_contract: config_json.commission_per_contract,
            order_offset: config_json.order_offset,
            stale_guard_min_price: config_json.stale_guard_min_price,
            default_unit_size: config_json.default_unit_size,
            account_id: config_json.account_id,
            server_name: config_json.server_name,
            min_long_delta: config_json.min_long_delta,
            order_auto_execute_timeout: config_json.order_auto_execute_timeout,
            bootstrap_mode: config_json.bootstrap_mode,
            db_path: config_json.db_path,
            
            portfolio_start_time: NaiveTime::parse_from_str(&config_json.portfolio_start_time, "%H:%M:%S")?,
            portfolio_end_time: NaiveTime::parse_from_str(&config_json.portfolio_end_time, "%H:%M:%S")?,
            portfolio_interval_minutes: config_json.portfolio_interval_minutes,
            start_time: NaiveTime::parse_from_str(&config_json.start_time, "%H:%M:%S")?,
            end_time: NaiveTime::parse_from_str(&config_json.end_time, "%H:%M:%S")?,
            
            otm_offset: config_json.otm_offset,
            buffer_zone: config_json.buffer_zone,
            dry_run: config_json.dry_run,
            web_port: config_json.web_port,
            email_alert_delay_seconds: config_json.email_alert_delay_seconds.unwrap_or(5),
            stale_quote_threshold_secs: config_json.stale_quote_threshold_secs.unwrap_or(60),
        })
    }
}

impl Default for AppConfig {
    fn default() -> Self {
        Self {
            schwab_token_path: std::path::PathBuf::from("dummy_token.json"),
            schwab_account: "dummy_account".to_string(),
            schwab_api_key: "dummy_api_key".to_string(),
            schwab_api_secret: "dummy_api_secret".to_string(),
            schwab_callback_url: "dummy_callback_url".to_string(),
            dry_run: true,
            initial_sum_delta: 0.25,
            init_wing_delta: 0.05,
            rebalance_threshold: 0.1,
            long_leg_rebalance_delta_threshold: 0.1,
            min_credit: 0.5,
            max_spread_diff: 0.1,
            commission_per_contract: 1.13,
            order_offset: 0.0,
            stale_guard_min_price: 0.0,
            default_unit_size: 1,
            account_id: "dummy_account_id".to_string(),
            server_name: "dummy_server".to_string(),
            min_long_delta: 0.01,
            order_auto_execute_timeout: 60,
            bootstrap_mode: "soft".to_string(),
            db_path: "dummy_db.db".to_string(),
            portfolio_start_time: NaiveTime::from_hms_opt(8, 30, 0).unwrap(),
            portfolio_end_time: NaiveTime::from_hms_opt(23, 59, 0).unwrap(),
            portfolio_interval_minutes: 1,
            start_time: NaiveTime::from_hms_opt(8, 30, 0).unwrap(),
            end_time: NaiveTime::from_hms_opt(23, 59, 0).unwrap(),
            otm_offset: 50.0,
            buffer_zone: 10.0,
            web_port: 8080,
            email_alert_delay_seconds: 5,
            stale_quote_threshold_secs: 60,
        }
    }
}
