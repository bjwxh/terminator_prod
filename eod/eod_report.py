#!/usr/bin/env python3
import os
import sys
import json
import asyncio
import logging
import pandas as pd
import matplotlib.pyplot as plt
from datetime import datetime, date, time, timedelta
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from typing import Optional
import smtplib
import argparse

# Add project root to sys.path
dir_path = os.path.dirname(os.path.realpath(__file__))
root_dir = os.path.abspath(os.path.join(dir_path, "..")) # Back to terminator_prod root
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from server.core.monitor import LiveTradingMonitor
from server.core.config import CONFIG
from server.core.models import TradePurpose

# ReportLab imports
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet

HIST_CSV_PATH = os.path.join(dir_path, "v4_eod_history.csv")
CHART_PATH = "/tmp/v4_equity_curve.png"
INTRADAY_CHART_PATH = "/tmp/v4_intraday_analysis.png"
PDF_PATH = "/tmp/v4_eod_report.pdf"

async def run_simulation(target_date: date, live_trades=None):
    """Run the Terminator logic against target_date's DB data"""
    start_dt = datetime.combine(target_date, time(8, 30))
    end_dt = datetime.combine(target_date, time(15, 0))
    
    # Use the same DB path as config
    db_path = CONFIG['db_path']
    if not os.path.isabs(db_path):
        db_path = os.path.join(root_dir, db_path)
    
    logging.info(f"Running simulation for {target_date} using {db_path}...")
    
    monitor = LiveTradingMonitor()
    # Ensure simulation uses the right DB
    monitor.config['db_path'] = db_path
    
    history = await monitor._run_historical_simulation(start_dt, end_dt, live_trades=live_trades, collect_history=True)
    
    p = monitor.combined_portfolio
    trades_count = sum(abs(l.quantity) for t in p.trades if t.purpose != TradePurpose.EXIT for l in t.legs)
    
    # Calculate Gross and Net PnL
    net_pnl = p.cash
    mv = sum(l.price * l.quantity * 100 for l in p.positions)
    net_pnl += mv
    
    fees = sum(t.commission for t in p.trades)
    gross_pnl = net_pnl + fees
    
    return {
        'sim_trades': trades_count,
        'sim_gross_pnl': round(gross_pnl, 2),
        'sim_net_pnl': round(net_pnl, 2),
        'history': history
    }

def get_live_results(target_date: date, session_path: Optional[str] = None):
    """Load live results from session file"""
    if not session_path:
        session_path = CONFIG['session_file_path']
        
    if not os.path.exists(session_path):
        logging.warning(f"Session file not found at {session_path}. Using zero values.")
        return {'real_trades': 0, 'real_gross_pnl': 0.0, 'real_net_pnl': 0.0}
    
    try:
        with open(session_path, 'r') as f:
            state = json.load(f)
        
        # Check date (only if not a recovery run with explicit session file)
        # Actually, if we pass a session file, we might want to bypass the daily check
        # But let's check it for safety if it's the default file.
        if not session_path.endswith('_0126.json') and state.get('date') != target_date.isoformat():
            logging.warning(f"Session file date ({state.get('date')}) does not match target date ({target_date.isoformat()}).")
            return {'real_trades': 0, 'real_gross_pnl': 0.0, 'real_net_pnl': 0.0}
            
        p = state.get('live_combined_portfolio')
        
        if not p:
            logging.warning("No live_combined_portfolio found in session file.")
            return {'real_trades': 0, 'real_gross_pnl': 0.0, 'real_net_pnl': 0.0}
            
        if not p:
            logging.warning("No portfolio data found in session file.")
            return {'real_trades': 0, 'real_gross_pnl': 0.0, 'real_net_pnl': 0.0}
        
        trades = p.get('trades', [])
        real_trades = 0
        fees = 0.0
        for t in trades:
            fees += t.get('commission', 0)
            if t.get('purpose') == TradePurpose.EXIT.value:
                continue
            for leg in t.get('legs', []):
                real_trades += abs(leg.get('quantity', 0))
        
        net_rely = p.get('cash', 0)
        positions = p.get('positions', [])
        mv = sum(l.get('price', 0) * l.get('quantity', 0) * 100 for l in positions)
        net_pnl = net_rely + mv
        gross_pnl = net_pnl + fees
        
        return {
            'real_trades': real_trades,
            'real_gross_pnl': round(gross_pnl, 2),
            'real_net_pnl': round(net_pnl, 2)
        }
    except Exception as e:
        logging.error(f"Error reading live results: {e}")
        return {'real_trades': 0, 'real_gross_pnl': 0.0, 'real_net_pnl': 0.0}

def update_history(target_date: date, results: dict):
    """Append results to CSV and return full history"""
    target_str = target_date.isoformat()
    
    if os.path.exists(HIST_CSV_PATH):
        df = pd.read_csv(HIST_CSV_PATH)
    else:
        df = pd.DataFrame(columns=['date', 'sim_trades', 'sim_gross_pnl', 'sim_net_pnl', 'real_trades', 'real_gross_pnl', 'real_net_pnl'])
    
    # Remove existing entry for target date to avoid duplicates or to update
    df = df[df['date'] != target_str]
    
    # Only append if there was trading activity
    if results.get('sim_trades', 0) > 0 or results.get('real_trades', 0) > 0:
        new_row = {'date': target_str, **results}
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
    
    df = df.sort_values('date')
    df.to_csv(HIST_CSV_PATH, index=False)
    return df

def generate_equity_curve(df):
    """Generate and save the equity curve chart"""
    if df.empty:
        logging.warning("No data for equity curve.")
        return

    df = df.copy()
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date')
    
    df['sim_cum_pnl'] = df['sim_net_pnl'].cumsum()
    df['real_cum_pnl'] = df['real_net_pnl'].cumsum()
    
    plt.figure(figsize=(10, 6))
    
    # Use index to avoid gaps for weekends/holidays on x-axis
    x_indices = range(len(df))
    plt.plot(x_indices, df['sim_cum_pnl'], label='Sim (v4)', color='blue', marker='o', markersize=4)
    plt.plot(x_indices, df['real_cum_pnl'], label='Real (v4)', color='green', marker='s', markersize=4)
    
    plt.title('SPX 0DTE v4: Cumulative Net PnL (Sim vs Real)')
    plt.xlabel('Date')
    plt.ylabel('Cumulative PnL ($)')
    plt.legend()
    plt.grid(True, linestyle='--', alpha=0.7)
    
    # Format x-ticks to show dates without gaps
    num_ticks = min(len(df), 12)
    indices = [int(i) for i in pd.Series(range(len(df))).iloc[::max(1, len(df)//num_ticks)]]
    if len(df) - 1 not in indices:
        indices.append(len(df) - 1)
    
    labels = df['date'].dt.strftime('%m-%d').iloc[indices]
    plt.xticks(indices, labels, rotation=45)
    
    plt.tight_layout()
    plt.savefig(CHART_PATH)
    plt.close()

def generate_intraday_chart(history):
    """Generate the 4-subplot intraday analysis chart"""
    if not history:
        logging.warning("No history data for intraday chart.")
        return

    df = pd.DataFrame(history)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    
    fig, (ax1, ax2, ax3, ax4) = plt.subplots(4, 1, figsize=(12, 16), sharex=True)
    
    # Colors: Sim = Blue, Live = Green
    # Styles: Call = Dash, Put = Solid
    
    # 1. Strikes and Index
    ax1.plot(df['timestamp'], df['spx'], color='grey', alpha=0.3, label='SPX Index', linewidth=2)
    
    # Sim (Blue) - Wider line behind
    ax1.plot(df['timestamp'], df['sim_sc_strike'], color='blue', linestyle='--', label='Sim Short Call', linewidth=2.5, zorder=3)
    ax1.plot(df['timestamp'], df['sim_sp_strike'], color='blue', linestyle='-', label='Sim Short Put', linewidth=2.5, zorder=3)
    
    # Live (Green) - Thinner line on top
    ax1.plot(df['timestamp'], df['live_sc_strike'], color='green', linestyle='--', label='Live Short Call', linewidth=1.2, zorder=5)
    ax1.plot(df['timestamp'], df['live_sp_strike'], color='green', linestyle='-', label='Live Short Put', linewidth=1.2, zorder=5)
    
    ax1.set_title('Strike Prices vs SPX Index')
    ax1.set_ylabel('Price / Strike')
    ax1.legend(loc='lower right', fontsize='x-small', ncol=2)
    ax1.grid(True, alpha=0.3)

    # 2. Short CALL Deltas
    ax2.plot(df['timestamp'], df['sim_sc_delta'], color='blue', label='Sim Call Delta', linewidth=2.5, zorder=3)
    ax2.plot(df['timestamp'], df['live_sc_delta'], color='green', label='Live Call Delta', linewidth=1.2, zorder=5)
    ax2.axhline(y=0.175, color='grey', linestyle=':', alpha=0.5)
    ax2.set_title('Short CALL Delta Drift')
    ax2.set_ylabel('Abs Delta')
    ax2.legend(loc='upper left', fontsize='small')
    ax2.grid(True, alpha=0.3)

    # 3. Short PUT Deltas
    ax3.plot(df['timestamp'], df['sim_sp_delta'], color='blue', label='Sim Put Delta', linewidth=2.5, zorder=3)
    ax3.plot(df['timestamp'], df['live_sp_delta'], color='green', label='Live Put Delta', linewidth=1.2, zorder=5)
    ax3.axhline(y=0.175, color='grey', linestyle=':', alpha=0.5)
    ax3.set_title('Short PUT Delta Drift')
    ax3.set_ylabel('Abs Delta')
    ax3.legend(loc='upper left', fontsize='small')
    ax3.grid(True, alpha=0.3)

    # 4. Net PnL
    ax4.plot(df['timestamp'], df['sim_pnl'], color='blue', label='Sim Net PnL', linewidth=2, zorder=3)
    ax4.plot(df['timestamp'], df['live_pnl'], color='green', label='Live Net PnL', linewidth=1.3, zorder=5)
    ax4.axhline(y=0, color='black', linewidth=1)
    ax4.set_title('Intraday Cumulative Net PnL')
    ax4.set_ylabel('PnL ($)')
    ax4.set_xlabel('Time')
    ax4.legend(loc='upper left')
    ax4.grid(True, alpha=0.3)

    # Formatting X-axis
    import matplotlib.dates as mdates
    ax4.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    
    plt.tight_layout()
    plt.savefig(INTRADAY_CHART_PATH)
    plt.close()

def generate_pdf(df, today_data, target_date: date):
    """Build the PDF report"""
    doc = SimpleDocTemplate(PDF_PATH, pagesize=letter)
    story = []
    styles = getSampleStyleSheet()
    
    # Title
    story.append(Paragraph(f"<b>Terminator Terminator EOD Report</b>", styles['Title']))
    story.append(Paragraph(f"Date: {target_date.isoformat()}", styles['Normal']))
    story.append(Spacer(1, 20))
    
    # Today's Table
    story.append(Paragraph("<b>Today's Performance</b>", styles['Heading2']))
    
    table_data = [
        ['Metric', 'Simulation', 'Real (Live)'],
        ['No. Trades', today_data['sim_trades'], today_data['real_trades']],
        ['Gross PnL', f"${today_data['sim_gross_pnl']:,.2f}", f"${today_data['real_gross_pnl']:,.2f}"],
        ['Fees', f"${(today_data['sim_gross_pnl'] - today_data['sim_net_pnl']):,.2f}", 
                  f"${(today_data['real_gross_pnl'] - today_data['real_net_pnl']):,.2f}"],
        ['Net PnL', f"${today_data['sim_net_pnl']:,.2f}", f"${today_data['real_net_pnl']:,.2f}"]
    ]
    
    t = Table(table_data, colWidths=[120, 100, 100])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.grey),
        ('TEXTCOLOR', (0,0), (-1,0), colors.whitesmoke),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0,0), (-1,0), 12),
        ('GRID', (0,0), (-1,-1), 1, colors.black),
        ('BACKGROUND', (0,1), (-1,-1), colors.beige),
    ]))
    story.append(t)
    story.append(Spacer(1, 30))
    
    # Historical Summary Stats
    story.append(Paragraph("<b>Multi-Day Summary (Net PnL)</b>", styles['Heading2']))
    
    last_5 = df.tail(5)
    last_22 = df.tail(22)
    
    summary_data = [
        ['Period', 'Sim Net PnL', 'Real Net PnL', 'Sim Trades', 'Real Trades'],
        ['Past 5 Days', f"${last_5['sim_net_pnl'].sum():,.2f}", f"${last_5['real_net_pnl'].sum():,.2f}", 
                        int(last_5['sim_trades'].sum()), int(last_5['real_trades'].sum())],
        ['Past 22 Days', f"${last_22['sim_net_pnl'].sum():,.2f}", f"${last_22['real_net_pnl'].sum():,.2f}", 
                         int(last_22['sim_trades'].sum()), int(last_22['real_trades'].sum())],
        ['All Time', f"${df['sim_net_pnl'].sum():,.2f}", f"${df['real_net_pnl'].sum():,.2f}", 
                     int(df['sim_trades'].sum()), int(df['real_trades'].sum())]
    ]
    
    st = Table(summary_data, colWidths=[100, 100, 100, 80, 80])
    st.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.darkcyan),
        ('TEXTCOLOR', (0,0), (-1,0), colors.whitesmoke),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
    ]))
    story.append(st)
    story.append(Spacer(1, 30))
    
    # Intraday Analysis Curve
    story.append(Paragraph("<b>Intraday Strategy Analysis</b>", styles['Heading2']))
    story.append(Image(INTRADAY_CHART_PATH, width=450, height=600))
    story.append(Spacer(1, 20))

    # Equity Curve Image
    story.append(Paragraph("<b>Historical Cumulative Performance</b>", styles['Heading2']))
    story.append(Image(CHART_PATH, width=450, height=270))
    
    doc.build(story)

def send_email(target_date: date):
    """Email the report"""
    email_config_path = CONFIG.get('email_config_path')
    if not os.path.exists(email_config_path):
        logging.error(f"Email config not found at {email_config_path}")
        return

    with open(email_config_path, 'r') as f:
        email_config = json.load(f)
    
    recipients = CONFIG.get('email_recipients', ['frankwang.alert@gmail.com'])
    subject = f"Terminator Terminator EOD Report - {target_date.isoformat()}"
    
    msg = MIMEMultipart()
    msg['From'] = email_config['from_email']
    msg['To'] = ", ".join(recipients)
    msg['Subject'] = subject
    
    body = f"Attached is the SPX 0DTE Terminator End-of-Day report for {date.today().isoformat()}."
    msg.attach(MIMEText(body, 'plain'))
    
    # Attach PDF
    with open(PDF_PATH, "rb") as attachment:
        part = MIMEBase('application', 'octet-stream')
        part.set_payload(attachment.read())
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f"attachment; filename= v4_eod_report_{target_date.isoformat()}.pdf")
        msg.attach(part)
        
    try:
        with smtplib.SMTP(email_config['smtp_server'], email_config['smtp_port']) as server:
            server.starttls()
            server.login(email_config['from_email'], email_config['password'])
            server.send_message(msg)
        logging.info("EOD Report email sent successfully.")
    except Exception as e:
        logging.error(f"Failed to send EOD Report email: {e}")

async def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    parser = argparse.ArgumentParser(description="Generate Terminator EOD Report")
    parser.add_argument("--date", type=str, help="Target date (YYYY-MM-DD)", default=date.today().isoformat())
    parser.add_argument("--session", type=str, help="Path to session state JSON", default=None)
    args = parser.parse_args()
    
    target_date = date.fromisoformat(args.date)
    
    # Exit if target date is a weekend
    if target_date.weekday() >= 5:
        logging.info(f"Skipping EOD report for {target_date} as it is a weekend.")
        return
    
    # 1. Extract Live Trades for Reconstruction
    live_trades = []
    if args.session:
        session_path = args.session
    else:
        session_path = CONFIG['session_file_path']
        
    if os.path.exists(session_path):
        try:
            with open(session_path, 'r') as f:
                state = json.load(f)
            p_data = state.get('live_combined_portfolio')
            
            if p_data and p_data.get('trades'):
                monitor = LiveTradingMonitor()
                for t_json in p_data['trades']:
                    trade = monitor.session_manager._restore_trade(t_json)
                    live_trades.append(trade)
                logging.info(f"Successfully restored {len(live_trades)} live trades for reconstruction.")
        except Exception as e:
            logging.error(f"Failed to restore live trades for chart: {e}")

    # 2. Run Simulation
    sim_data = await run_simulation(target_date, live_trades=live_trades)
    
    # 3. Get Live results
    live_data = get_live_results(target_date, args.session)
    
    # Remove history from results dict before updating history CSV
    results_for_csv = {k: v for k, v in sim_data.items() if k != 'history'}
    results_for_csv.update(live_data)
    
    # Debug: Check values in history for first point with data
    if sim_data['history']:
        h_with_sim = next((h for h in sim_data['history'] if h['sim_sc_strike']), sim_data['history'][0])
        h_with_live = next((h for h in sim_data['history'] if h['live_sc_strike']), sim_data['history'][0])
        h_last = sim_data['history'][-1]
        logging.info(f"DEBUG: First Sim Strike Point  - Time: {h_with_sim['timestamp'].time()}, Sim SC: {h_with_sim['sim_sc_strike']}")
        logging.info(f"DEBUG: First Live Strike Point - Time: {h_with_live['timestamp'].time()}, Live SC: {h_with_live['live_sc_strike']}")
        logging.info(f"DEBUG: Final Strike Point     - Time: {h_last['timestamp'].time()}, Sim SC: {h_last['sim_sc_strike']}, Live SC: {h_last['live_sc_strike']}")

    # 4. Update History
    df = update_history(target_date, results_for_csv)
    
    # Filter for trading days only for charts and stats
    # (Back-compatible with existing 0-trade rows in CSV)
    df_trading = df[(df['sim_trades'] > 0) | (df['real_trades'] > 0)].copy()

    # 5. Generate Charts
    generate_equity_curve(df_trading)
    generate_intraday_chart(sim_data.get('history', []))
    
    # 6. Generate PDF
    generate_pdf(df_trading, results_for_csv, target_date)
    
    # 6. Send Email
    send_email(target_date)

if __name__ == "__main__":
    asyncio.run(main())
