use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use tokio::sync::Mutex;
use tracing::info;

use crate::options_chain::StrikeAndSide;
use crate::websocket::WebsocketClient;

pub struct SlidingWindowManager {
    otm_offset: f64,
    buffer_zone: f64,
    symbol_map: HashMap<StrikeAndSide, String>,
    last_center_spx: Mutex<Option<f64>>,
    ws_client: Arc<WebsocketClient>,
}

impl SlidingWindowManager {
    pub fn new(
        otm_offset: f64,
        buffer_zone: f64,
        symbol_map: HashMap<StrikeAndSide, String>,
        ws_client: Arc<WebsocketClient>,
    ) -> Self {
        Self {
            otm_offset,
            buffer_zone,
            symbol_map,
            last_center_spx: Mutex::new(None),
            ws_client,
        }
    }

    /// Process the current underlying index price and update option subscriptions if necessary.
    pub async fn handle_index_update(&self, spx_price: f64) -> anyhow::Result<()> {
        let mut last_center = self.last_center_spx.lock().await;
        
        let should_update = match *last_center {
            None => true,
            Some(center) => (spx_price - center).abs() >= self.buffer_zone,
        };

        if should_update {
            info!("SPX price moved to {} (previous center: {:?}). Updating options window subscriptions...", spx_price, *last_center);
            
            // 1. Calculate target strike bounds
            // Puts: [spx_price - offset, spx_price]
            // Calls: [spx_price, spx_price + offset]
            let put_min = spx_price - self.otm_offset;
            let put_max = spx_price;
            let call_min = spx_price;
            let call_max = spx_price + self.otm_offset;

            // 2. Identify symbols within the target ranges
            let mut target_symbols = HashSet::new();
            for (key, sym) in &self.symbol_map {
                let strike = key.strike.0;
                if key.is_call {
                    if strike >= call_min && strike <= call_max {
                        target_symbols.insert(sym.clone());
                    }
                } else {
                    if strike >= put_min && strike <= put_max {
                        target_symbols.insert(sym.clone());
                    }
                }
            }

            // 3. Compare with currently active subscribed options in websocket client
            let currently_subscribed = self.ws_client.get_subscribed_symbols().lock().await.clone();

            // We only want options symbols here (filter out equities like $SPX, $VIX)
            let current_options: HashSet<String> = currently_subscribed
                .into_iter()
                .filter(|sym| !sym.starts_with('$'))
                .collect();

            // Calculate differences
            let to_subscribe: Vec<String> = target_symbols
                .difference(&current_options)
                .cloned()
                .collect();

            let to_unsubscribe: Vec<String> = current_options
                .difference(&target_symbols)
                .cloned()
                .collect();

            // 4. Send subscription changes to WebSocket client
            if !to_unsubscribe.is_empty() {
                self.ws_client.unsubscribe(to_unsubscribe, "LEVELONE_OPTIONS").await?;
            }
            if !to_subscribe.is_empty() {
                self.ws_client.subscribe(to_subscribe, "LEVELONE_OPTIONS").await?;
            }

            *last_center = Some(spx_price);
        }

        Ok(())
    }
}
