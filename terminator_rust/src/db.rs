use rusqlite::Connection;
use std::collections::BTreeMap;

#[derive(Debug, Clone)]
pub struct OptionQuoteRow {
    pub strike: f64,
    pub side: String, // "CALL" or "PUT"
    pub bid: f64,
    pub ask: f64,
    pub delta: f64,
    pub theta: f64,
    pub symbol: String,
}

#[derive(Debug, Clone)]
pub struct OptionsSnapshot {
    pub datetime: String,
    pub quotes: Vec<OptionQuoteRow>,
}

pub fn load_historical_snapshots(
    db_path: &str,
    start_ts: &str,
    end_ts: &str,
) -> anyhow::Result<Vec<OptionsSnapshot>> {
    let conn = Connection::open(db_path)?;
    conn.busy_timeout(std::time::Duration::from_millis(1000))?;
    let mut stmt = conn.prepare(
        "SELECT datetime, strike_price, side, bidprice, askprice, delta, theta, symbol \
         FROM stock_options \
         WHERE root_symbol = '$SPX' AND dte = 0 \
         AND datetime BETWEEN ?1 AND ?2 \
         ORDER BY datetime"
    )?;

    let rows = stmt.query_map([start_ts, end_ts], |row| {
        Ok((
            row.get::<_, String>(0)?,
            OptionQuoteRow {
                strike: row.get(1)?,
                side: row.get(2)?,
                bid: row.get(3)?,
                ask: row.get(4)?,
                delta: row.get(5)?,
                theta: row.get(6)?,
                symbol: row.get(7)?,
            }
        ))
    })?;

    // Group by datetime, maintaining chronological order via BTreeMap
    let mut grouped: BTreeMap<String, Vec<OptionQuoteRow>> = BTreeMap::new();
    for row_res in rows {
        let (dt, quote) = row_res?;
        grouped.entry(dt).or_default().push(quote);
    }

    let snapshots = grouped
        .into_iter()
        .map(|(datetime, quotes)| OptionsSnapshot { datetime, quotes })
        .collect();

    Ok(snapshots)
}

pub fn get_latest_db_timestamp(db_path: &str) -> anyhow::Result<String> {
    let conn = Connection::open(db_path)?;
    conn.busy_timeout(std::time::Duration::from_millis(500))?;
    let mut stmt = conn.prepare("SELECT MAX(datetime) FROM stock_options")?;
    let mut rows = stmt.query([])?;
    
    if let Some(row) = rows.next()? {
        let dt: String = row.get(0)?;
        Ok(dt)
    } else {
        Err(anyhow::anyhow!("No records found in stock_options"))
    }
}

pub fn estimate_spx_from_snapshot(quotes: &[OptionQuoteRow]) -> Option<f64> {
    // Find the ATM call (delta closest to 0.50) and ATM put (delta closest to -0.50)
    let mut closest_call = None;
    let mut min_call_diff = f64::MAX;

    let mut closest_put = None;
    let mut min_put_diff = f64::MAX;

    for q in quotes {
        if q.side == "CALL" {
            let diff = (q.delta.abs() - 0.50).abs();
            if diff < min_call_diff {
                min_call_diff = diff;
                closest_call = Some(q.strike);
            }
        } else if q.side == "PUT" {
            let diff = (q.delta.abs() - 0.50).abs();
            if diff < min_put_diff {
                min_put_diff = diff;
                closest_put = Some(q.strike);
            }
        }
    }

    match (closest_call, closest_put) {
        (Some(c), Some(p)) => Some((c + p) / 2.0),
        (Some(c), None) => Some(c),
        (None, Some(p)) => Some(p),
        (None, None) => None,
    }
}
