use serde::Serialize;
use crate::strategy::Trade;
use crate::grid::OptionsGrid;

#[derive(Debug, Clone, Serialize)]
pub struct PositionLeg {
    pub symbol: String,
    pub strike: f64,
    pub side: String, // "CALL" or "PUT"
    #[serde(rename = "qty")]
    pub quantity: i32, // Positive = long, Negative = short
    pub delta: f64,
    pub theta: f64,
    pub price: f64,
    pub entry_price: f64,
    pub bid: f64,
    pub ask: f64,
    pub current_day_pnl: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct CompletedTrade {
    pub ts: String,
    pub purpose: String,
    pub credit: f64,
    pub strategy: String,
    pub legs: Vec<OptionLegSnapshot>,
}

#[derive(Debug, Clone, Serialize)]
pub struct OptionLegSnapshot {
    pub symbol: String,
    pub qty: i32,
    pub strike: f64,
    pub side: String,
}

pub struct Portfolio {
    pub positions: Vec<PositionLeg>,
    pub trades: Vec<Trade>,
    pub cash: f64,
}

#[derive(Serialize, Clone)]
pub struct PortfolioSnapshot {
    pub pnl: f64,
    pub fees: f64,
    pub net_pnl: f64,
    pub realized: f64,
    pub unrealized: f64,
    pub margin: f64,
    pub trades: usize,
    pub recent_trades: Vec<CompletedTrade>,
    pub delta: f64,
    pub theta: f64,
    pub positions: Vec<PositionLeg>,
}

impl Portfolio {
    pub fn new() -> Self {
        Self {
            positions: Vec::new(),
            trades: Vec::new(),
            cash: 0.0,
        }
    }

    pub fn total_contracts(&self) -> usize {
        self.trades
            .iter()
            .map(|t| t.legs.iter().map(|l| l.quantity.abs() as usize).sum::<usize>())
            .sum()
    }

    pub fn fees(&self) -> f64 {
        self.trades.iter().map(|t| t.commission).sum()
    }

    pub fn gross_pnl(&self) -> f64 {
        // cash + sum(price * qty * 100)
        let open_value: f64 = self.positions
            .iter()
            .map(|p| p.price * (p.quantity as f64) * 100.0)
            .sum();
        self.cash + open_value
    }

    pub fn net_pnl(&self) -> f64 {
        self.gross_pnl() - self.fees()
    }

    pub fn realized_pnl(&self) -> f64 {
        let open_entry_credits: f64 = self.positions
            .iter()
            .filter(|p| p.quantity < 0)
            .map(|p| p.entry_price * (p.quantity.abs() as f64) * 100.0)
            .sum();
        
        let open_entry_costs: f64 = self.positions
            .iter()
            .filter(|p| p.quantity > 0)
            .map(|p| p.entry_price * (p.quantity as f64) * 100.0)
            .sum();

        self.cash - open_entry_credits + open_entry_costs - self.fees()
    }

    pub fn unrealized_pnl(&self) -> f64 {
        self.positions
            .iter()
            .map(|p| (p.price - p.entry_price) * (p.quantity as f64) * 100.0)
            .sum()
    }

    pub fn total_delta(&self) -> f64 {
        self.positions
            .iter()
            .map(|p| p.delta * (p.quantity as f64))
            .sum()
    }

    pub fn total_theta(&self) -> f64 {
        self.positions
            .iter()
            .map(|p| p.theta * (p.quantity as f64))
            .sum()
    }

    pub fn current_margin(&self) -> f64 {
        // Reg-T Margin: evaluate max loss for CALL and PUT separately at each strike boundary
        let calls: Vec<&PositionLeg> = self.positions.iter().filter(|p| p.side == "CALL").collect();
        let puts: Vec<&PositionLeg> = self.positions.iter().filter(|p| p.side == "PUT").collect();

        let calculate_side_risk = |legs: &[&PositionLeg]| -> f64 {
            if legs.is_empty() {
                return 0.0;
            }
            let mut strikes: Vec<f64> = legs.iter().map(|l| l.strike).collect();
            strikes.sort_by(|a, b| a.partial_cmp(b).unwrap());
            strikes.dedup();

            let mut max_loss: f64 = 0.0;
            for &test_strike in &strikes {
                let mut loss_at_strike = 0.0;
                for leg in legs {
                    // Intrinsic value of option side at test_strike
                    let intrinsic = if leg.side == "CALL" {
                        (test_strike - leg.strike).max(0.0)
                    } else {
                        (leg.strike - test_strike).max(0.0)
                    };
                    // Value change relative to entry_price
                    // For short: entry_price - intrinsic
                    // For long: intrinsic - entry_price
                    let leg_qty = leg.quantity as f64;
                    let pnl = if leg_qty < 0.0 {
                        (leg.entry_price - intrinsic) * leg_qty.abs() * 100.0
                    } else {
                        (intrinsic - leg.entry_price) * leg_qty * 100.0
                    };
                    loss_at_strike += pnl;
                }
                if loss_at_strike < 0.0 {
                    max_loss = max_loss.max(-loss_at_strike);
                }
            }
            max_loss
        };

        let call_risk = calculate_side_risk(&calls);
        let put_risk = calculate_side_risk(&puts);
        call_risk.max(put_risk)
    }

    pub fn add_trade(&mut self, trade: &Trade, fill_prices: Option<Vec<f64>>) {
        self.trades.push(trade.clone());
        self.cash += trade.credit;

        for (i, leg) in trade.legs.iter().enumerate() {
            let fill_price = fill_prices.as_ref().and_then(|v| v.get(i).cloned()).unwrap_or(leg.price);
            
            // Check if leg already exists in position to net out
            if let Some(pos) = self.positions.iter_mut().find(|p| p.symbol == leg.symbol) {
                let old_qty = pos.quantity;
                pos.quantity += leg.quantity;
                if pos.quantity == 0 {
                    // Net zero, remove it
                    self.positions.retain(|p| p.symbol != leg.symbol);
                } else {
                    // Recalculate average entry price if adding to position on same side
                    if (old_qty > 0 && leg.quantity > 0) || (old_qty < 0 && leg.quantity < 0) {
                        let total_qty = old_qty.abs() + leg.quantity.abs();
                        pos.entry_price = ((pos.entry_price * (old_qty.abs() as f64)) + (fill_price * (leg.quantity.abs() as f64))) / (total_qty as f64);
                    }
                    pos.price = leg.price;
                    pos.delta = leg.delta;
                    pos.theta = leg.theta;
                }
            } else {
                // New position
                self.positions.push(PositionLeg {
                    symbol: leg.symbol.clone(),
                    strike: leg.strike,
                    side: leg.side.clone(),
                    quantity: leg.quantity,
                    delta: leg.delta,
                    theta: leg.theta,
                    price: leg.price,
                    entry_price: fill_price,
                    bid: leg.price,
                    ask: leg.price,
                    current_day_pnl: 0.0,
                });
            }
        }
    }

    pub fn update_pricing(&mut self, grid: &OptionsGrid) {
        for pos in &mut self.positions {
            // Find current contract quote in OptionsGrid
            if let Some(quote) = grid.quotes.get(&ordered_float::OrderedFloat(pos.strike)) {
                let leg_quote = if pos.side == "CALL" {
                    &quote.call
                } else {
                    &quote.put
                };

                if let Some(lq) = leg_quote {
                    pos.bid = lq.bid;
                    pos.ask = lq.ask;
                    pos.price = lq.mid;
                    pos.delta = lq.delta;
                    pos.theta = lq.theta;
                    pos.current_day_pnl = (pos.price - pos.entry_price) * (pos.quantity as f64) * 100.0;
                }
            }
        }
    }

    pub fn snapshot(&self) -> PortfolioSnapshot {
        let recent_trades = self.trades
            .iter()
            .rev()
            .take(50)
            .map(|t| CompletedTrade {
                ts: t.timestamp.clone(),
                purpose: t.purpose.clone(),
                credit: t.credit,
                strategy: t.strategy_id.clone(),
                legs: t.legs
                    .iter()
                    .map(|l| OptionLegSnapshot {
                        symbol: l.symbol.clone(),
                        qty: l.quantity,
                        strike: l.strike,
                        side: l.side.clone(),
                    })
                    .collect(),
            })
            .collect();

        PortfolioSnapshot {
            pnl: self.gross_pnl(),
            fees: self.fees(),
            net_pnl: self.net_pnl(),
            realized: self.realized_pnl(),
            unrealized: self.unrealized_pnl(),
            margin: self.current_margin(),
            trades: self.total_contracts(),
            recent_trades,
            delta: self.total_delta(),
            theta: self.total_theta(),
            positions: self.positions.clone(),
        }
    }
}
