import re

with open("terminator_rust/src/strategy.rs", "r") as f:
    content = f.read()

# 1. Add Types
types = """
#[derive(Clone, Debug)]
pub struct LegOverride { pub idx: usize, pub price_ea: f64 }
#[derive(Clone, Debug)]
pub struct PendingTrade { pub strat_id: String, pub trade: Trade }
#[derive(Clone, Debug)]
pub struct HistoryPoint { pub ts: String, pub spx: f64, pub live_pnl: f64, pub sim_pnl: f64 }
"""

# Insert types after `OptionLegQuote` or `parser::parse_occ_symbol;`
content = content.replace("use crate::parser::parse_occ_symbol;\n", "use crate::parser::parse_occ_symbol;\n" + types + "\n")

# 2. Modify StrategySupervisor struct
struct_def = """pub struct StrategySupervisor {
    pub config: crate::config::AppConfig,
    pub execution_client: Arc<ExecutionClient>,
    pub grid: Arc<OptionsGrid>,
    pub sub_strategies: Mutex<HashMap<String, SubStrategy>>,
    pub account_hash: Mutex<Option<String>>,
    pub live_portfolio: Arc<Mutex<crate::portfolio::Portfolio>>,
    pub working_orders: tokio::sync::Mutex<Vec<serde_json::Value>>,
    pub pending_trade: tokio::sync::Mutex<Option<PendingTrade>>,
    pub server_name: String,
    pub status: tokio::sync::Mutex<String>,
    pub broker_connected: std::sync::atomic::AtomicBool,
    pub trading_enabled: std::sync::atomic::AtomicBool,
    pub heartbeat_failures: std::sync::atomic::AtomicUsize,
    pub timer_paused: std::sync::atomic::AtomicBool,
    pub session_history: tokio::sync::Mutex<std::collections::VecDeque<HistoryPoint>>,
}"""

content = re.sub(r'pub struct StrategySupervisor \{.*?\}', struct_def, content, flags=re.DOTALL)

# 3. Modify StrategySupervisor::new
new_impl = """        Self {
            config: config.clone(),
            execution_client,
            grid,
            sub_strategies: Mutex::new(sub_strategies),
            account_hash: Mutex::new(None),
            live_portfolio: Arc::new(Mutex::new(crate::portfolio::Portfolio::new())),
            working_orders: Mutex::new(Vec::new()),
            pending_trade: Mutex::new(None),
            server_name: config.server_name.clone(),
            status: Mutex::new("Initializing".to_string()),
            broker_connected: std::sync::atomic::AtomicBool::new(false),
            trading_enabled: std::sync::atomic::AtomicBool::new(true),
            heartbeat_failures: std::sync::atomic::AtomicUsize::new(0),
            timer_paused: std::sync::atomic::AtomicBool::new(false),
            session_history: Mutex::new(std::collections::VecDeque::new()),
        }"""
content = re.sub(r'        Self \{\s+config,.*?\}', new_impl, content, flags=re.DOTALL)

# 4. In tick(), add session_history append
tick_prologue = """    pub async fn tick(&self) -> Result<()> {
        let now_ct = if std::env::var("TERMINATOR_TEST_ENV").is_ok() {
            Chicago.with_ymd_and_hms(2026, 5, 22, 9, 0, 0).unwrap()
        } else {
            Chicago.from_utc_datetime(&chrono::Utc::now().naive_utc())
        };
        let current_time = now_ct.time();

        {
            let mut hist = self.session_history.lock().await;
            let spx = self.grid.get_underlying_price();
            let net_pnl = self.live_portfolio.lock().await.net_pnl();
            let (live_pnl, sim_pnl) = if self.config.dry_run {
                (0.0, net_pnl)
            } else {
                (net_pnl, 0.0)
            };
            hist.push_back(HistoryPoint {
                ts: now_ct.to_rfc3339(),
                spx,
                live_pnl,
                sim_pnl,
            });
            if hist.len() > 1000 {
                hist.pop_front();
            }
        }"""
content = content.replace("""    pub async fn tick(&self) -> Result<()> {
        let now_ct = if std::env::var("TERMINATOR_TEST_ENV").is_ok() {
            Chicago.with_ymd_and_hms(2026, 5, 22, 9, 0, 0).unwrap()
        } else {
            Chicago.from_utc_datetime(&chrono::Utc::now().naive_utc())
        };
        let current_time = now_ct.time();""", tick_prologue)

# 5. Gate entry execution
entry_code = """                    let enabled = self.trading_enabled.load(std::sync::atomic::Ordering::Relaxed);
                    let paused = self.timer_paused.load(std::sync::atomic::Ordering::Relaxed);
                    if !enabled || paused {
                        info!("Trading disabled or paused. Routing to pending_trade for user confirmation.");
                        *self.pending_trade.lock().await = Some(PendingTrade {
                            strat_id: sid.clone(),
                            trade: trade.clone(),
                        });
                        continue;
                    }

                    s.state = StrategyState::EnteringSpread;
                    let mut port = s.portfolio.lock().await;
                    match execute_trade(&self.execution_client, &account_hash, &trade, self.config.dry_run, self.config.order_offset, &*port).await {
                        Ok(order_id) => {
                            info!("✅ Order placed successfully for strategy {}. Order ID: {:?}", sid, order_id);
                            s.state = StrategyState::Working;
                            s.has_traded_today = true;
                            s.active_order_id = order_id.clone();
                            if let Some(ref oid) = order_id {
                                self.working_orders.lock().await.push(json!({"orderId": oid, "strategy_id": sid.clone()}));
                            }
                            port.add_trade(&trade, None);
                            self.live_portfolio.lock().await.add_trade(&trade, None);
                        }"""
content = re.sub(r'                    s\.state = StrategyState::EnteringSpread;.*?self\.live_portfolio\.lock\(\)\.await\.add_trade\(&trade, None\);\n                        \}', entry_code, content, flags=re.DOTALL)

# 6. Gate exit execution
exit_code = """                        if let Some(exit_trade) = check_exit(&self.grid, &*s_port, now_ct, &s.sid, self.config.commission_per_contract, spx) {
                            let enabled = self.trading_enabled.load(std::sync::atomic::Ordering::Relaxed);
                            let paused = self.timer_paused.load(std::sync::atomic::Ordering::Relaxed);
                            if !enabled || paused {
                                info!("Trading disabled or paused. Routing to pending_trade for user confirmation.");
                                *self.pending_trade.lock().await = Some(PendingTrade {
                                    strat_id: sid.clone(),
                                    trade: exit_trade.clone(),
                                });
                                continue;
                            }
                            s.state = StrategyState::Exiting;
                            if let Ok(order_id) = execute_trade(&self.execution_client, &account_hash, &exit_trade, self.config.dry_run, self.config.order_offset, &*s_port).await {
                                if let Some(ref oid) = order_id {
                                    self.working_orders.lock().await.push(json!({"orderId": oid, "strategy_id": sid.clone()}));
                                }
                                s_port.add_trade(&exit_trade, None);
                                self.live_portfolio.lock().await.add_trade(&exit_trade, None);
                            } else {
                                s.state = StrategyState::Working; // Retry next tick
                            }
                        }"""
content = re.sub(r'                        if let Some\(exit_trade\) = check_exit.*?s\.state = StrategyState::Working; // Retry next tick\n                            \}\n                        \}', exit_code, content, flags=re.DOTALL)

# 7. Gate rebalance execution
rebalance_code = """                        let enabled = self.trading_enabled.load(std::sync::atomic::Ordering::Relaxed);
                        let paused = self.timer_paused.load(std::sync::atomic::Ordering::Relaxed);
                        if !enabled || paused {
                            info!("Trading disabled or paused. Routing to pending_trade for user confirmation.");
                            *self.pending_trade.lock().await = Some(PendingTrade {
                                strat_id: sid.clone(),
                                trade: trade.clone(),
                            });
                            continue;
                        }
                        if let Ok(order_id) = execute_trade(&self.execution_client, &account_hash, &trade, self.config.dry_run, self.config.order_offset, &*s_port).await {
                            if let Some(ref oid) = order_id {
                                self.working_orders.lock().await.push(json!({"orderId": oid, "strategy_id": sid.clone()}));
                            }
                            s_port.add_trade(&trade, None);
                            self.live_portfolio.lock().await.add_trade(&trade, None);
                        }"""
content = re.sub(r'                        if let Ok\(_\) = execute_trade\(&self\.execution_client, &account_hash, &trade, self\.config\.dry_run, self\.config\.order_offset, &\*s_port\)\.await \{\n                            s_port\.add_trade\(&trade, None\);\n                            self\.live_portfolio\.lock\(\)\.await\.add_trade\(&trade, None\);\n                        \}', rebalance_code, content)

# 8. Add control methods at the end of the impl StrategySupervisor block
methods = """    pub async fn confirm_trade(&self, _strat_id: &str, overrides: Vec<LegOverride>) -> Result<()> {
        let pending = { self.pending_trade.lock().await.take() };
        if let Some(t) = pending {
            let fill_prices: Vec<f64> = t.trade.legs.iter().enumerate()
                .map(|(i, leg)| {
                    overrides.iter()
                        .find(|o| o.idx == i)
                        .map(|o| o.price_ea)
                        .unwrap_or(leg.price)
                })
                .collect();
            let mut port = self.live_portfolio.lock().await;
            port.add_trade(&t.trade, Some(fill_prices));
        }
        Ok(())
    }

    pub async fn dismiss_trade(&self, _strat_id: &str) {
        let _ = self.pending_trade.lock().await.take();
    }

    pub fn set_timer_paused(&self, is_paused: bool) {
        self.timer_paused.store(is_paused, std::sync::atomic::Ordering::Relaxed);
    }

    pub fn toggle_trading_enabled(&self) -> bool {
        let current = self.trading_enabled.load(std::sync::atomic::Ordering::Relaxed);
        self.trading_enabled.store(!current, std::sync::atomic::Ordering::Relaxed);
        !current
    }
}"""
content = re.sub(r'\}\s*$', methods + "\n", content)

# 9. Clear working_orders in process_account_event
process_event = """            if event.status == "Filled" || event.status == "Cancelled" || event.status == "Rejected" || event.status == "Canceled" {
                let mut wo = self.working_orders.lock().await;
                wo.retain(|o| o.get("orderId").and_then(|v| v.as_str()) != Some(&event.order_id));
            }
            if event.status == "Filled" {"""
content = content.replace('            if event.status == "Filled" {', process_event)

with open("terminator_rust/src/strategy.rs", "w") as f:
    f.write(content)

