use std::sync::Arc;
use std::time::Duration;
use anyhow::Context;
use arc_swap::ArcSwap;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use tracing::{info, error, warn};
use tokio::time::sleep;

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct SchwabToken {
    pub creation_timestamp: i64,
    pub token: TokenDetails,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct TokenDetails {
    pub expires_in: i64,
    pub token_type: String,
    pub scope: String,
    pub refresh_token: String,
    pub access_token: String,
    pub id_token: String,
    pub expires_at: i64,
}

#[derive(Debug, Deserialize)]
pub struct OAuthResponse {
    pub access_token: String,
    pub expires_in: i64,
    pub token_type: String,
    pub scope: String,
    pub refresh_token: Option<String>,
    pub id_token: String,
}

pub struct TokenManager {
    config: crate::config::AppConfig,
    client: Client,
    current_token: Arc<ArcSwap<SchwabToken>>,
}

impl TokenManager {
    pub fn new(config: crate::config::AppConfig) -> anyhow::Result<Self> {
        let initial_token = Self::load_token_from_file(&config.schwab_token_path)?;
        let client = Client::builder()
            .timeout(Duration::from_secs(10))
            .build()?;

        Ok(Self {
            config,
            client,
            current_token: Arc::new(ArcSwap::from_pointee(initial_token)),
        })
    }

    /// Read the current active access token without locking.
    pub fn get_access_token(&self) -> String {
        self.current_token.load().token.access_token.clone()
    }

    /// Read the full token struct for testing/reconnection validation.
    pub fn get_token(&self) -> Arc<SchwabToken> {
        self.current_token.load_full()
    }

    /// Read the active Schwab account number.
    pub fn get_account_id(&self) -> String {
        self.config.schwab_account.clone()
    }

    /// Load the token JSON from disk
    fn load_token_from_file(path: &std::path::Path) -> anyhow::Result<SchwabToken> {
        if !path.exists() {
            anyhow::bail!("Schwab token file not found at: {:?}", path);
        }
        let content = std::fs::read_to_string(path)?;
        let token: SchwabToken = serde_json::from_str(&content)
            .context("Failed to deserialize Schwab token JSON")?;
        Ok(token)
    }

    /// Save the token JSON to disk
    fn save_token_to_file(path: &std::path::Path, token: &SchwabToken) -> anyhow::Result<()> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let content = serde_json::to_string_pretty(token)?;
        std::fs::write(path, content)?;
        Ok(())
    }

    /// Perform a direct, blocking refresh request (useful for startup synchronization).
    pub async fn refresh_token_now(&self) -> anyhow::Result<()> {
        let old_token = self.current_token.load_full();
        let refresh_token = &old_token.token.refresh_token;

        info!("Initiating OAuth token refresh from Schwab API...");

        // Construct standard OAuth2 request form parameters
        let params = [
            ("grant_type", "refresh_token"),
            ("refresh_token", refresh_token),
        ];

        let response = self.client
            .post("https://api.schwabapi.com/v1/oauth/token")
            .basic_auth(&self.config.schwab_api_key, Some(&self.config.schwab_api_secret))
            .form(&params)
            .send()
            .await?;

        if !response.status().is_success() {
            let status = response.status();
            let err_body = response.text().await.unwrap_or_else(|_| "No body".to_string());
            error!("Schwab OAuth token refresh failed: Status = {}, Details = {}", status, err_body);
            anyhow::bail!("OAuth response error: {} - {}", status, err_body);
        }

        let oauth_res: OAuthResponse = response.json().await?;
        let now_sec = chrono::Utc::now().timestamp();
        
        // Schwab may not return a new refresh token on every refresh. If omitted, retain the current one.
        let new_refresh_token = oauth_res.refresh_token
            .unwrap_or_else(|| refresh_token.clone());

        let new_schwab_token = SchwabToken {
            creation_timestamp: now_sec,
            token: TokenDetails {
                expires_in: oauth_res.expires_in,
                token_type: oauth_res.token_type,
                scope: oauth_res.scope,
                refresh_token: new_refresh_token,
                access_token: oauth_res.access_token,
                id_token: oauth_res.id_token,
                expires_at: now_sec + oauth_res.expires_in,
            },
        };

        // Thread-safely swap pointer
        self.current_token.store(Arc::new(new_schwab_token.clone()));

        // Save persistent copy
        Self::save_token_to_file(&self.config.schwab_token_path, &new_schwab_token)
            .context("Failed to persist refreshed token to file system")?;

        info!("Schwab access token successfully refreshed and stored to disk!");
        Ok(())
    }

    /// Background async loop to monitor and auto-refresh the token prior to expiration.
    pub async fn start_background_loop(self: Arc<Self>) {
        info!("Starting TokenManager background auto-refresh loop.");
        
        loop {
            // Check current expiration state
            let token = self.current_token.load_full();
            let now_sec = chrono::Utc::now().timestamp();
            let time_to_expiry = token.token.expires_at - now_sec;

            // Access tokens are typically valid for 30 mins (1800s). We refresh if < 5 mins (300s) left.
            if time_to_expiry < 300 {
                warn!("Access token close to expiration ({}s remaining). Refreshing...", time_to_expiry);
                if let Err(e) = self.refresh_token_now().await {
                    error!("TokenManager failed background token refresh: {:?}", e);
                    // On failure, retry in 30 seconds instead of waiting full interval
                    sleep(Duration::from_secs(30)).await;
                    continue;
                }
            } else {
                info!("Access token is healthy ({}s remaining). Next check in 60s.", time_to_expiry);
            }

            // Sleep for 60 seconds between health checks
            sleep(Duration::from_secs(60)).await;
        }
    }
}
