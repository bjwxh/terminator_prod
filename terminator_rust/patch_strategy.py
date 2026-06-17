import re

with open("src/strategy.rs", "r") as f:
    content = f.read()

# Add chrono::TimeZone
content = content.replace("use chrono::{DateTime, Local, NaiveTime, Utc};", "use chrono::{DateTime, Local, NaiveTime, Utc, TimeZone};")
if "use crate::db::" not in content:
    content = content.replace("use crate::execution::{BrokerPosition, ExecutionClient};", "use crate::execution::{BrokerPosition, ExecutionClient};\nuse crate::db::{load_historical_snapshots, estimate_spx_from_snapshot};")

bootstrap_code = """
    pub async fn bootstrap_from_history(&self, account_hash: &str) {
        if self.config.db_path.is_empty() {
            return;
        }

        let mode = self.config.bootstrap_mode.to_lowercase();
        if mode == "none" { return; }

        info!("Starting historical bootstrap replay (Mode: {})", mode);

        let now_utc = Utc::now();
        let tz: chrono_tz::Tz = "America/Chicago".parse().unwrap();
        let now_ct = now_utc.with_timezone(&tz);
        let start_time = now_ct.date_naive().and_hms_opt(8, 30, 0).unwrap().and_local_timezone(tz).unwrap();

        if now_ct < start_time {
            info!("Skipping bootstrap (before 8:30 AM)");
            return;
        }

        let mut live_trades = Vec::new();
        if mode == "soft" {
            match self.execution_client.get_today_filled_orders(account_hash).await {
                Ok(trades) => {
                    live_trades = trades;
                    info!("Fetched {} live filled trades for soft bootstrap matching", live_trades.len());
                }
                Err(e) => {
                    warn!("Failed to fetch live trades for soft bootstrap: {:?}", e);
                }
            }
        }

        // Reset all strategies
        {
            let mut strats = self.sub_strategies.lock().await;
            for s in strats.values_mut() {
                *s.portfolio.lock().await = Portfolio::new();
                s.has_traded_today = false;
                s.state = StrategyState::Idle;
            }
            *self.live_portfolio.lock().await = Portfolio::new();
            *self.session_history.lock().await = std::collections::VecDeque::new();
        }

        // Mutual Clarity Matching (Soft Mode)
        let mut assigned_live_entry = std::collections::HashMap::new();
        if mode == "soft" {
            let mut strat_matches = std::collections::HashMap::new();
            let mut trade_strats = std::collections::HashMap::new();

            for lt in &live_trades {
                if let Ok(lt_ts) = DateTime::parse_from_rfc3339(&lt.timestamp) {
                    let lt_ct = lt_ts.with_timezone(&tz);
                    for (sid, s) in self.sub_strategies.lock().await.iter() {
                        let win_start = start_time.date_naive().and_time(s.trade_start_time).and_local_timezone(tz).unwrap();
                        let win_end = win_start + chrono::Duration::hours(1);
                        if lt_ct >= win_start && lt_ct <= win_end {
                            strat_matches.entry(sid.clone()).or_insert_with(Vec::new).push(lt.clone());
                            trade_strats.entry(lt.strategy_id.clone()).or_insert_with(Vec::new).push(sid.clone());
                        }
                    }
                }
            }

            for (sid, matches) in strat_matches {
                if matches.len() == 1 {
                    let lt = &matches[0];
                    if let Some(strats) = trade_strats.get(&lt.strategy_id) {
                        if strats.len() == 1 {
                            info!("Soft Bootstrap: Assigned live trade {} to {}", lt.strategy_id, sid);
                            assigned_live_entry.insert(sid.clone(), lt.clone());
                        } else {
                            info!("Soft Bootstrap: Match for {} is ambiguous (trade matches multiple strategies). Sim fallback.", sid);
                        }
                    }
                } else if matches.len() > 1 {
                    info!("Soft Bootstrap: Match for {} is ambiguous (multiple trades in window). Sim fallback.", sid);
                }
            }
        }

        let start_str = start_time.to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        let end_str = now_ct.to_rfc3339_opts(chrono::SecondsFormat::Millis, true);

        match load_historical_snapshots(&self.config.db_path, &start_str, &end_str) {
            Ok(snapshots) => {
                info!("Loaded {} historical snapshots for replay", snapshots.len());
                for snap in snapshots {
                    if let Ok(snap_ts) = DateTime::parse_from_rfc3339(&snap.datetime) {
                        let snap_ct = snap_ts.with_timezone(&tz);
                        
                        let spx = estimate_spx_from_snapshot(&snap.quotes).unwrap_or_else(|| self.grid.get_spx());
                        self.grid.inject_snapshot(&snap.quotes, spx);

                        // Run sub-strategies
                        let mut strats = self.sub_strategies.lock().await;
                        for (sid, s) in strats.iter_mut() {
                            if snap_ct.time() < s.trade_start_time { continue; }

                            if !s.has_traded_today {
                                if mode == "soft" {
                                    if let Some(lt) = assigned_live_entry.get(sid) {
                                        if let Ok(lt_ts) = DateTime::parse_from_rfc3339(&lt.timestamp) {
                                            if lt_ts.with_timezone(&tz) <= snap_ct {
                                                info!("Soft Bootstrap: Seeding {} with live trade {} at {}", sid, lt.strategy_id, snap_ct);
                                                let sync_trade = create_sync_entry(lt, snap_ct);
                                                s.portfolio.lock().await.add_trade(&sync_trade, None);
                                                self.live_portfolio.lock().await.add_trade(&sync_trade, None);
                                                s.has_traded_today = true;
                                                s.state = StrategyState::Working;
                                            }
                                        }
                                        continue;
                                    }
                                }

                                // Hard simulation entry
                                if let Some(trade) = check_entry(&self.grid, s, snap_ct, self.config.max_spread_diff, self.config.commission_per_contract) {
                                    info!("Bootstrap [HARD]: Entry for {} via sim logic at {}", sid, snap_ct);
                                    s.portfolio.lock().await.add_trade(&trade, None);
                                    self.live_portfolio.lock().await.add_trade(&trade, None);
                                    s.has_traded_today = true;
                                    s.state = StrategyState::Working;
                                }
                            } else if s.state == StrategyState::Working {
                                let mut s_port = s.portfolio.lock().await;
                                s_port.update_pricing(&self.grid);
                                
                                let end_time = start_time.date_naive().and_time(self.config.end_time).and_local_timezone(tz).unwrap();
                                if snap_ct >= end_time {
                                    if !s_port.positions.is_empty() {
                                        if let Some(exit_trade) = check_exit(&self.grid, &*s_port, snap_ct, sid, self.config.commission_per_contract, spx) {
                                            s.state = StrategyState::Exiting;
                                            s_port.add_trade(&exit_trade, None);
                                            self.live_portfolio.lock().await.add_trade(&exit_trade, None);
                                        }
                                    }
                                } else {
                                    let rebal_trades = check_rebalance(&self.grid, s, &*s_port, snap_ct, start_time.time(), end_time.time(), &self.config);
                                    for trade in rebal_trades {
                                        s_port.add_trade(&trade, None);
                                        self.live_portfolio.lock().await.add_trade(&trade, None);
                                    }
                                }
                            }
                        }

                        // Add to session history
                        let sim_pnl = {
                            let strats = self.sub_strategies.lock().await;
                            let mut total = 0.0;
                            for s in strats.values() {
                                let port = s.portfolio.lock().await;
                                total += port.cash + port.positions.iter().map(|p| p.price * (p.quantity as f64) * 100.0).sum::<f64>();
                            }
                            total
                        };
                        let live_pnl = {
                            let port = self.live_portfolio.lock().await;
                            port.cash + port.positions.iter().map(|p| p.price * (p.quantity as f64) * 100.0).sum::<f64>()
                        };

                        let mut hist = self.session_history.lock().await;
                        hist.push_back(serde_json::json!({
                            "ts": snap.datetime,
                            "spx": spx,
                            "sim_pnl": sim_pnl,
                            "live_pnl": live_pnl
                        }));
                    }
                }
            }
            Err(e) => {
                error!("Failed to load historical snapshots for bootstrap: {:?}", e);
            }
        }
        
        info!("Bootstrap complete. Transitioning to live ticking.");
    }
"""

sync_entry_code = """
pub fn create_sync_entry(live_trade: &Trade, now: DateTime<chrono_tz::Tz>) -> Trade {
    let mut sync_trade = live_trade.clone();
    sync_trade.timestamp = now.to_rfc3339();
    sync_trade.purpose = "IRON_CONDOR".to_string();
    sync_trade.commission = 0.0;
    // Map legs directly. Actual price fills are used if we have them, else 0.
    sync_trade
}
"""

if "pub async fn bootstrap_from_history" not in content:
    content = content.replace("pub async fn run_supervisor_loop(self: Arc<Self>) {", bootstrap_code + "\n    pub async fn run_supervisor_loop(self: Arc<Self>) {")

if "pub fn create_sync_entry" not in content:
    content = content + "\n" + sync_entry_code

content = content.replace("self.reconcile_startup_positions(&positions).await;", "self.reconcile_startup_positions(&positions).await;\n                }\n                Err(e) => {\n                    warn!(\"Could not verify open positions at startup: {:?}. Proceeding with fresh state.\", e);\n                }\n            }\n        }\n\n        self.bootstrap_from_history(&resolved_hash).await;\n\n        // Remove the extra match arm below as we inject it here")

# Clean up the replace mess manually
# It's better to just do a precise regex replace for the insertion of bootstrap_from_history call
import re
content = re.sub(r'(if !resolved_hash\.is_empty\(\) \{\n\s*match self\.execution_client\.get_live_positions\(&resolved_hash\)\.await \{\n\s*Ok\(positions\) => \{\n\s*self\.reconcile_startup_positions\(&positions\)\.await;\n\s*\}\n\s*Err\(e\) => \{\n\s*warn!\("Could not verify open positions at startup: \{\:\?\}\. Proceeding with fresh state\.", e\);\n\s*\}\n\s*\}\n\s*\})', r'\1\n\n        self.bootstrap_from_history(&resolved_hash).await;', content)


with open("src/strategy.rs", "w") as f:
    f.write(content)

