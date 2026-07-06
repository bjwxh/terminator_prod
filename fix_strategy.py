import re

with open("terminator_rust/src/strategy.rs", "r") as f:
    content = f.read()

# 1. Update session_history to be inside the trading hours gate and rate limited
old_history = """        {
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
        }

        let start_time = self.config.start_time;
        let end_time = self.config.end_time;

        if current_time < start_time || current_time > end_time {
            return Ok(());
        }"""

new_history = """        let start_time = self.config.start_time;
        let end_time = self.config.end_time;

        if current_time < start_time || current_time > end_time {
            return Ok(());
        }

        let now_secs = now_ct.timestamp();
        if now_secs % 30 == 0 {
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

content = content.replace(old_history, new_history)

# 2. Update broker_connected and status after resolving account hash
old_hash = """                    *h = Some(hash.clone());
                    break hash;
                }
                Err(e) => {"""

new_hash = """                    *h = Some(hash.clone());
                    self.broker_connected.store(true, std::sync::atomic::Ordering::Relaxed);
                    *self.status.lock().await = "Running".to_string();
                    break hash;
                }
                Err(e) => {"""

content = content.replace(old_hash, new_hash)

# 3. Fix confirm_trade to execute and update sub-strategy portfolio
old_confirm = """    pub async fn confirm_trade(&self, _strat_id: &str, overrides: Vec<LegOverride>) -> Result<()> {
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
    }"""

new_confirm = """    pub async fn confirm_trade(&self, strat_id: &str, overrides: Vec<LegOverride>) -> Result<()> {
        let pending = { self.pending_trade.lock().await.take() };
        if let Some(mut t) = pending {
            let fill_prices: Vec<f64> = t.trade.legs.iter().enumerate()
                .map(|(i, leg)| {
                    overrides.iter()
                        .find(|o| o.idx == i)
                        .map(|o| o.price_ea)
                        .unwrap_or(leg.price)
                })
                .collect();
            
            for (i, leg) in t.trade.legs.iter_mut().enumerate() {
                leg.price = fill_prices[i];
            }
            t.trade.credit = t.trade.legs.iter().map(|l| -(l.quantity as f64) * l.price).sum::<f64>() * 100.0;

            let account_hash = self.account_hash.lock().await.clone().unwrap_or_default();
            
            let mut strats = self.sub_strategies.lock().await;
            if let Some(s) = strats.get_mut(strat_id) {
                let mut s_port = s.portfolio.lock().await;
                if let Ok(order_id) = execute_trade(
                    &self.execution_client, &account_hash, &t.trade,
                    self.config.dry_run, self.config.order_offset, &*s_port
                ).await {
                    if let Some(ref oid) = order_id {
                        self.working_orders.lock().await.push(json!({"orderId": oid, "strategy_id": t.strat_id}));
                    }
                    s_port.add_trade(&t.trade, None);
                    self.live_portfolio.lock().await.add_trade(&t.trade, None);
                    
                    s.state = StrategyState::Working;
                    s.has_traded_today = true;
                    s.active_order_id = order_id;
                }
            }
        }
        Ok(())
    }"""

content = content.replace(old_confirm, new_confirm)

with open("terminator_rust/src/strategy.rs", "w") as f:
    f.write(content)

