#!/usr/bin/env python3
import os
import sys
import json
import asyncio
import logging
import pandas as pd
import matplotlib
matplotlib.use('Agg') # Headless backend for VM
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, date, time, timedelta
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from typing import Optional, List, Dict, Any
import smtplib
import argparse

# ReportLab imports
try:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
    from reportlab.lib.styles import getSampleStyleSheet
except ImportError:
    pass

# Add project root to sys.path
dir_path = os.path.dirname(os.path.realpath(__file__))
root_dir = os.path.abspath(os.path.join(dir_path, ".."))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from server.core.monitor import LiveTradingMonitor, CHICAGO
from server.core.config import CONFIG
from server.core.models import TradePurpose
from server.notifications import send_push

# Paths
HIST_CSV_PATH = os.path.join(dir_path, "terminator_eod_history.csv")
CHART_PATH = "/tmp/terminator_equity_curve.png"
INTRADAY_CHART_PATH = "/tmp/terminator_intraday_analysis.png"
PDF_PATH = "/tmp/terminator_eod_report.pdf"

DEFAULT_DB_PATH = os.path.join(root_dir, "server", "market_data.db")
DEFAULT_SESSION_PATH = os.path.join(root_dir, "server", "session_state.json")

async def run_simulation(target_date: date, live_trades=None, db_path=None):
    """Run simulation and return results + history"""
    start_dt = datetime.combine(target_date, time(8, 30), CHICAGO)
    end_dt = datetime.combine(target_date, time(15, 0), CHICAGO)
    
    if not db_path:
        db_path = DEFAULT_DB_PATH
        
    logging.info(f"Running simulation for {target_date} using {db_path}...")
    
    monitor = LiveTradingMonitor()
    monitor.config['db_path'] = db_path
    
    history = await monitor._run_historical_simulation(start_dt, end_dt, live_trades=live_trades, collect_history=True)
    
    p = monitor.combined_portfolio
    contracts_traded = p.total_contracts
    trades_count = len(p.trades)
    
    return {
        'sim_trades': trades_count,
        'sim_contracts': contracts_traded,
        'sim_gross_pnl': round(p.gross_pnl, 2),
        'sim_net_pnl': round(p.net_pnl, 2),
        'history': history
    }

def get_live_results(target_date: date, session_path: Optional[str] = None):
    """Load live results from session file"""
    if not session_path:
        session_path = DEFAULT_SESSION_PATH
        
    if not os.path.exists(session_path):
        return {'real_trades': 0, 'real_contracts': 0, 'real_gross_pnl': 0.0, 'real_net_pnl': 0.0}
    
    try:
        with open(session_path, 'r') as f:
            state = json.load(f)
            
        p = state.get('live_combined_portfolio', {})
        return {
            'real_trades': len(p.get('trades', [])),
            'real_contracts': p.get('total_contracts', 0),
            'real_gross_pnl': round(p.get('gross_pnl', 0.0), 2),
            'real_net_pnl': round(p.get('net_pnl', 0.0), 2)
        }
    except Exception as e:
        logging.error(f"Error reading live results: {e}")
        return {'real_trades': 0, 'real_contracts': 0, 'real_gross_pnl': 0.0, 'real_net_pnl': 0.0}

def update_history(target_date: date, results: dict):
    """Append results and return cleaned history (no zero trade days)"""
    target_str = target_date.isoformat()
    cols = ['date', 'sim_trades', 'sim_gross_pnl', 'sim_net_pnl', 'real_trades', 'real_gross_pnl', 'real_net_pnl', 'sim_contracts', 'real_contracts']
    
    if os.path.exists(HIST_CSV_PATH):
        df = pd.read_csv(HIST_CSV_PATH)
    else:
        df = pd.DataFrame(columns=cols)
        
    # Remove existing
    df = df[df['date'] != target_str]
    
    # Only append if activity
    if results.get('sim_trades', 0) > 0 or results.get('real_trades', 0) > 0:
        new_row = {'date': target_str, **results}
        # Filter keys to match cols
        row_dict = {k: new_row.get(k, 0.0) for k in cols}
        df = pd.concat([df, pd.DataFrame([row_dict])], ignore_index=True)
        
    # Clean zero-trade days as requested
    df = df[(df['sim_trades'] > 0) | (df['real_trades'] > 0)]
    
    df = df.sort_values('date')
    df.to_csv(HIST_CSV_PATH, index=False)
    return df

def generate_equity_curve(df):
    """Generate Cumulative PnL chart (Blue/Green, no weekend gaps)"""
    if df.empty: return
    
    df = df.copy()
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date')
    
    df['sim_cum'] = df['sim_net_pnl'].cumsum()
    df['real_cum'] = df['real_net_pnl'].cumsum()
    
    plt.figure(figsize=(10, 6))
    x_indices = range(len(df))
    plt.plot(x_indices, df['sim_cum'], label='Sim (Terminator)', color='blue', marker='o', markersize=4)
    plt.plot(x_indices, df['real_cum'], label='Real (Terminator)', color='green', marker='s', markersize=4)
    
    plt.title('SPX 0DTE Terminator: Cumulative Net PnL (Sim vs Real)')
    plt.xlabel('Date')
    plt.ylabel('Cumulative PnL ($)')
    plt.legend()
    plt.grid(True, linestyle='--', alpha=0.7)
    
    # Label x-axis with dates
    num_ticks = min(len(df), 12)
    indices = [int(i) for i in pd.Series(range(len(df))).iloc[::max(1, len(df)//num_ticks)]]
    if len(df)-1 not in indices: indices.append(len(df)-1)
    labels = df['date'].dt.strftime('%m-%d').iloc[indices]
    plt.xticks(indices, labels, rotation=45)
    
    plt.tight_layout()
    plt.savefig(CHART_PATH)
    plt.close()

def generate_intraday_chart(history):
    """4-subplot intraday analysis (Strikes, Call Delta, Put Delta, PnL)"""
    if not history: return
    
    df = pd.DataFrame(history)
    # Align with monitor.py key 'ts'
    t_col = 'ts' if 'ts' in df.columns else 'timestamp'
    df['timestamp'] = pd.to_datetime(df[t_col])
    
    fig, (ax1, ax2, ax3, ax4) = plt.subplots(4, 1, figsize=(12, 16), sharex=True)
    
    # Subplot 1: Strikes
    ax1.plot(df['timestamp'], df['spx'], color='grey', alpha=0.3, label='SPX', linewidth=2)
    ax1.plot(df['timestamp'], df['sim_sc_strike'], color='blue', ls='--', label='Sim SC', lw=2.5, zorder=3)
    ax1.plot(df['timestamp'], df['sim_sp_strike'], color='blue', ls='-', label='Sim SP', lw=2.5, zorder=3)
    ax1.plot(df['timestamp'], df['live_sc_strike'], color='green', ls='--', label='Live SC', lw=1.2, zorder=5)
    ax1.plot(df['timestamp'], df['live_sp_strike'], color='green', ls='-', label='Live SP', lw=1.2, zorder=5)
    ax1.set_title('Strike Prices vs SPX Index')
    ax1.legend(loc='lower right', ncol=2, fontsize='x-small')
    ax1.grid(True, alpha=0.3)
    
    # Subplot 2: Call Delta
    ax2.plot(df['timestamp'], df['sim_sc_delta'], color='blue', label='Sim Call Delta', lw=2.5, zorder=3)
    ax2.plot(df['timestamp'], df['live_sc_delta'], color='green', label='Live Call Delta', lw=1.2, zorder=5)
    ax2.axhline(y=0.175, color='grey', ls=':', alpha=0.5)
    ax2.set_title('Short CALL Delta Drift')
    ax2.set_ylabel('Abs Delta')
    ax2.legend(loc='upper left', fontsize='small')
    ax2.grid(True, alpha=0.3)
    
    # Subplot 3: Put Delta
    ax3.plot(df['timestamp'], df['sim_sp_delta'], color='blue', label='Sim Put Delta', lw=2.5, zorder=3)
    ax3.plot(df['timestamp'], df['live_sp_delta'], color='green', label='Live Put Delta', lw=1.2, zorder=5)
    ax3.axhline(y=0.175, color='grey', ls=':', alpha=0.5)
    ax3.set_title('Short PUT Delta Drift')
    ax3.set_ylabel('Abs Delta')
    ax3.legend(loc='upper left', fontsize='small')
    ax3.grid(True, alpha=0.3)
    
    # Subplot 4: PnL
    ax4.plot(df['timestamp'], df['sim_pnl'], color='blue', label='Sim Net PnL', lw=2, zorder=3)
    ax4.plot(df['timestamp'], df['live_pnl'], color='green', label='Live Net PnL', lw=1.3, zorder=5)
    ax4.axhline(y=0, color='black', lw=1)
    ax4.set_title('Intraday Cumulative Net PnL')
    ax4.set_ylabel('PnL ($)')
    ax4.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax4.legend(loc='upper left')
    ax4.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig(INTRADAY_CHART_PATH)
    plt.close()

def generate_pdf(df, today_data, target_date: date):
    """Build PDF with Terminator theme"""
    try:
        doc = SimpleDocTemplate(PDF_PATH, pagesize=letter)
        story = []
        styles = getSampleStyleSheet()
        
        story.append(Paragraph(f"<b>Terminator EOD Report</b>", styles['Title']))
        story.append(Paragraph(f"Date: {target_date.isoformat()}", styles['Normal']))
        story.append(Spacer(1, 20))
        
        # Today Table
        story.append(Paragraph("<b>Today's Performance</b>", styles['Heading2']))
        table_data = [
            ['Metric', 'Simulation', 'Real (Live)'],
            ['No. Trades', today_data['sim_trades'], today_data['real_trades']],
            ['Contracts', today_data['sim_contracts'], today_data['real_contracts']],
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
            ('GRID', (0,0), (-1,-1), 1, colors.black),
            ('BACKGROUND', (0,1), (-1,-1), colors.beige),
        ]))
        story.append(t)
        story.append(Spacer(1, 30))
        
        # Historical Summary
        story.append(Paragraph("<b>Multi-Day Summary (Net PnL)</b>", styles['Heading2']))
        l5, l22 = df.tail(5), df.tail(22)
        summary_data = [
            ['Period', 'Sim Net PnL', 'Real Net PnL', 'Sim Trades', 'Real Trades'],
            ['Past 5 Days', f"${l5['sim_net_pnl'].sum():,.2f}", f"${l5['real_net_pnl'].sum():,.2f}", 
                            int(l5['sim_trades'].sum()), int(l5['real_trades'].sum())],
            ['Past 22 Days', f"${l22['sim_net_pnl'].sum():,.2f}", f"${l22['real_net_pnl'].sum():,.2f}", 
                             int(l22['sim_trades'].sum()), int(l22['real_trades'].sum())],
            ['All Time', f"${df['sim_net_pnl'].sum():,.2f}", f"${df['real_net_pnl'].sum():,.2f}", 
                         int(df['sim_trades'].sum()), int(df['real_trades'].sum())]
        ]
        st = Table(summary_data, colWidths=[100, 100, 100, 80, 80])
        st.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.darkcyan),
            ('TEXTCOLOR', (0,0), (-1,0), colors.whitesmoke),
            ('ALIGN', (0,0), (-1,-1), 'CENTER'),
            ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
        ]))
        story.append(st)
        story.append(Spacer(1, 30))
        
        story.append(Paragraph("<b>Intraday Strategy Analysis</b>", styles['Heading2']))
        story.append(Image(INTRADAY_CHART_PATH, width=450, height=600))
        story.append(Spacer(1, 20))
        story.append(Paragraph("<b>Historical Cumulative Performance</b>", styles['Heading2']))
        story.append(Image(CHART_PATH, width=450, height=270))
        
        doc.build(story)
        return True
    except Exception as e:
        logging.error(f"Error generating PDF: {e}")
        return False

def send_email(target_date: date):
    """Email report to recipients"""
    if not CONFIG.get('email_alerts_enabled'): return
    
    try:
        with open(CONFIG['email_config_path'], 'r') as f:
            ec = json.load(f)
            
        from_email = ec.get('from_email') or ec.get('sender_email')
        password = ec.get('password') or ec.get('sender_password')
            
        msg = MIMEMultipart()
        msg['Subject'] = f"Terminator EOD Report - {target_date.isoformat()}"
        msg['From'] = from_email
        msg['To'] = ", ".join(CONFIG.get('email_recipients', []))
        
        body = f"Attached is the SPX 0DTE Terminator End-of-Day report for {target_date.isoformat()}."
        msg.attach(MIMEText(body, 'plain'))
        
        if os.path.exists(PDF_PATH):
            with open(PDF_PATH, "rb") as f_pdf:
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(f_pdf.read())
                encoders.encode_base64(part)
                part.add_header('Content-Disposition', f'attachment; filename="terminator_report_{target_date.isoformat()}.pdf"')
                msg.attach(part)
                
        with smtplib.SMTP(ec['smtp_server'], ec['smtp_port']) as server:
            server.starttls()
            server.login(from_email, password)
            server.send_message(msg)
        logging.info("Email sent.")
    except Exception as e:
        logging.error(f"Email failed: {e}")

async def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--session", default=None)
    parser.add_argument("--email", action="store_true")
    args = parser.parse_args()
    
    target_date = date.fromisoformat(args.date)
    if target_date.weekday() >= 5: return
    
    # 1. Restore Trades for Reconstruction
    live_trades = []
    session_path = args.session or DEFAULT_SESSION_PATH
    if os.path.exists(session_path):
        try:
            with open(session_path, 'r') as f:
                state = json.load(f)
            p_data = state.get('live_combined_portfolio', {})
            if p_data.get('trades'):
                monitor = LiveTradingMonitor()
                for t_json in p_data['trades']:
                    trade = monitor.session_manager._restore_trade(t_json)
                    live_trades.append(trade)
        except Exception as e:
            logging.error(f"Trade restoration failed: {e}")

    # 2. Run simulation
    sim_data = await run_simulation(target_date, live_trades=live_trades)
    
    # 3. Get Live
    live_data = get_live_results(target_date, args.session)
    
    # 4. Update History
    combined_res = {**{k:v for k,v in sim_data.items() if k!='history'}, **live_data}
    df = update_history(target_date, combined_res)
    
    # 5. Charts and PDF
    generate_equity_curve(df)
    generate_intraday_chart(sim_data['history'])
    
    # 6. Generate PDF and Email
    if generate_pdf(df, combined_res, target_date) and args.email:
        send_email(target_date)
        # Push notification for completion
        if CONFIG.get('ntfy_enabled', True):
            msg = f"Terminator EOD Report for {target_date.isoformat()} has been generated and emailed."
            await send_push(CONFIG.get('ntfy_topic'), msg, title="EOD Report Ready")

if __name__ == "__main__":
    asyncio.run(main())
