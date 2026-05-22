use std::io;
use std::sync::Arc;
use std::time::Duration;
use crossterm::{
    event::{self, Event, KeyCode},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{
    backend::CrosstermBackend,
    layout::{Constraint, Direction, Layout},
    style::{Color, Modifier, Style},
    widgets::{Block, Borders, Cell, Row, Table, Paragraph},
    Terminal,
};

use crate::grid::OptionsGrid;

pub async fn run_tui_loop(
    grid: Arc<OptionsGrid>,
    token_manager: Arc<crate::token::TokenManager>,
) -> anyhow::Result<()> {
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen)?;
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;

    let tick_rate = Duration::from_millis(200);
    let mut last_tick = std::time::Instant::now();

    loop {
        // Draw terminal
        terminal.draw(|f| {
            draw_dashboard(f, &grid, &token_manager);
        })?;

        // Handle keys
        let timeout = tick_rate
            .checked_sub(last_tick.elapsed())
            .unwrap_or(Duration::from_secs(0));
            
        if crossterm::event::poll(timeout)? {
            if let Event::Key(key) = event::read()? {
                if key.code == KeyCode::Char('q') || key.code == KeyCode::Esc {
                    break;
                }
            }
        }
        
        if last_tick.elapsed() >= tick_rate {
            last_tick = std::time::Instant::now();
        }
    }

    // Restore terminal
    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen)?;
    terminal.show_cursor()?;
    Ok(())
}

fn draw_dashboard(
    f: &mut ratatui::Frame,
    grid: &OptionsGrid,
    token_manager: &crate::token::TokenManager,
) {
    // Layout
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(3), // Header
            Constraint::Length(4), // Status Info
            Constraint::Min(10),   // Options Chain Table
            Constraint::Length(1), // Footer
        ])
        .split(f.size());

    // 1. Draw Header
    let header = Paragraph::new("🦀  TERMINATOR RUST: ULTRA-LOW-LATENCY 0DTE SPX ENGINE  🦀")
        .style(Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD))
        .alignment(ratatui::layout::Alignment::Center)
        .block(Block::default().borders(Borders::ALL).border_style(Style::default().fg(Color::DarkGray)));
    f.render_widget(header, chunks[0]);

    // 2. Draw Status
    let spx = grid.get_underlying_price();
    let token = token_manager.get_token();
    let now_sec = chrono::Utc::now().timestamp();
    let time_to_expiry = token.token.expires_at - now_sec;
    
    let status_text = format!(
        " UNDERLYING SPX: {:.2}  |  ACTIVE ACCOUNT: {}  |  TOKEN HEALTH: {}s remaining  |  ACTIVE SYMBOLS: {}",
        spx,
        token_manager.get_account_id(),
        time_to_expiry,
        grid.quotes.len()
    );
    
    let status = Paragraph::new(status_text)
        .style(Style::default().fg(Color::Yellow))
        .block(Block::default().title(" System Status ").borders(Borders::ALL).border_style(Style::default().fg(Color::DarkGray)));
    f.render_widget(status, chunks[1]);

    // 3. Draw Options Chain Table
    let mut strikes: Vec<f64> = grid.quotes.iter().map(|e| e.key().0).collect();
    strikes.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));

    let atm_idx = strikes.iter()
        .position(|&s| s >= spx)
        .unwrap_or(strikes.len().saturating_sub(1) / 2);

    let start = atm_idx.saturating_sub(6);
    let end = std::cmp::min(strikes.len(), atm_idx + 7);
    let display_strikes = &strikes[start..end];

    let header_cells = vec![
        "C. Delta", "C. Bid", "C. Ask", "C. Mid", "   STRIKE   ", "P. Mid", "P. Ask", "P. Bid", "P. Delta"
    ];
    let header_row = Row::new(header_cells)
        .style(Style::default().fg(Color::DarkGray).add_modifier(Modifier::BOLD))
        .bottom_margin(1);

    let mut rows = Vec::new();
    for &strike in display_strikes {
        if let Some(quote) = grid.quotes.get(&ordered_float::OrderedFloat(strike)) {
            let is_atm = (strike - spx).abs() <= 5.0;
            
            let strike_style = if is_atm {
                Style::default().fg(Color::Magenta).add_modifier(Modifier::BOLD)
            } else {
                Style::default().fg(Color::White)
            };

            let call_cells = match &quote.call {
                Some(call) => vec![
                    Cell::new(format!("{:.3}", call.delta)).style(Style::default().fg(Color::Green)),
                    Cell::new(format!("{:.2}", call.bid)).style(Style::default().fg(Color::Gray)),
                    Cell::new(format!("{:.2}", call.ask)).style(Style::default().fg(Color::Gray)),
                    Cell::new(format!("{:.2}", call.mid)).style(Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)),
                ],
                None => vec![Cell::new("-"), Cell::new("-"), Cell::new("-"), Cell::new("-")],
            };

            let put_cells = match &quote.put {
                Some(put) => vec![
                    Cell::new(format!("{:.2}", put.mid)).style(Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)),
                    Cell::new(format!("{:.2}", put.ask)).style(Style::default().fg(Color::Gray)),
                    Cell::new(format!("{:.2}", put.bid)).style(Style::default().fg(Color::Gray)),
                    Cell::new(format!("{:.3}", put.delta)).style(Style::default().fg(Color::Red)),
                ],
                None => vec![Cell::new("-"), Cell::new("-"), Cell::new("-"), Cell::new("-")],
            };

            let mut final_cells = Vec::new();
            final_cells.extend(call_cells);
            final_cells.push(Cell::new(format!("  {:.1}  ", strike)).style(strike_style));
            final_cells.extend(put_cells);

            rows.push(Row::new(final_cells));
        }
    }

    let widths = [
        Constraint::Percentage(10), // C Delta
        Constraint::Percentage(10), // C Bid
        Constraint::Percentage(10), // C Ask
        Constraint::Percentage(11), // C Mid
        Constraint::Percentage(16), // Strike
        Constraint::Percentage(11), // P Mid
        Constraint::Percentage(10), // P Ask
        Constraint::Percentage(10), // P Bid
        Constraint::Percentage(10), // P Delta
    ];

    let table = Table::new(rows, widths)
        .header(header_row)
        .block(Block::default().title(" Live Options Grid (SPXW 0DTE) ").borders(Borders::ALL).border_style(Style::default().fg(Color::DarkGray)));
    f.render_widget(table, chunks[2]);

    // 4. Draw Footer
    let footer = Paragraph::new(" Press 'q' or 'ESC' to safely exit ")
        .style(Style::default().fg(Color::DarkGray).add_modifier(Modifier::ITALIC))
        .alignment(ratatui::layout::Alignment::Center);
    f.render_widget(footer, chunks[3]);
}
