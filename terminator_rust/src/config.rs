use anyhow::Context;
use serde::Deserialize;
use std::path::PathBuf;

#[derive(Debug, Clone)]
pub struct AppConfig {
    pub schwab_token_path: PathBuf,
    pub schwab_account: String,
    pub schwab_api_key: String,
    pub schwab_api_secret: String,
    pub schwab_callback_url: String,
    pub otm_offset: f64,
    pub buffer_zone: f64,
    pub dry_run: bool,
}

#[derive(Deserialize)]
struct SchwabApiFile {
    api_key: String,
    api_secret: String,
    callback_url: String,
}

impl AppConfig {
    pub fn load() -> anyhow::Result<Self> {
        let _ = dotenvy::dotenv();

        let schwab_api_path = PathBuf::from(
            std::env::var("SCHWAB_API_PATH")
                .context("SCHWAB_API_PATH environment variable is not set")?
        );

        let schwab_token_path = PathBuf::from(
            std::env::var("SCHWAB_TOKEN_PATH")
                .context("SCHWAB_TOKEN_PATH environment variable is not set")?
        );

        let schwab_account = std::env::var("SCHWAB_ACCOUNT")
            .context("SCHWAB_ACCOUNT environment variable is not set")?;

        let api_file: SchwabApiFile = {
            let content = std::fs::read_to_string(&schwab_api_path)
                .with_context(|| format!("Failed to read Schwab API file at {:?}", schwab_api_path))?;
            serde_json::from_str(&content)
                .context("Failed to parse Schwab API JSON")?
        };

        let otm_offset = std::env::var("OTM_OFFSET")
            .ok()
            .and_then(|val| val.parse::<f64>().ok())
            .unwrap_or(50.0);

        let buffer_zone = std::env::var("BUFFER_ZONE")
            .ok()
            .and_then(|val| val.parse::<f64>().ok())
            .unwrap_or(10.0);

        let dry_run = std::env::var("DRY_RUN")
            .ok()
            .and_then(|val| val.parse::<bool>().ok())
            .unwrap_or(true);

        Ok(Self {
            schwab_token_path,
            schwab_account,
            schwab_api_key: api_file.api_key,
            schwab_api_secret: api_file.api_secret,
            schwab_callback_url: api_file.callback_url,
            otm_offset,
            buffer_zone,
            dry_run,
        })
    }
}
