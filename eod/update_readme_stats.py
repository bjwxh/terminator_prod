#!/usr/bin/env python3
import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
import os
from datetime import datetime

# Paths
REPO_ROOT = "/Users/fw/Git/terminator_prod"
HIST_CSV = os.path.join(REPO_ROOT, "eod/terminator_eod_history.csv")
CHART_OUT = os.path.join(REPO_ROOT, "eod/assets/performance_curve.png")
README_PATH = os.path.join(REPO_ROOT, "README.md")

def calculate_stats(df, label):
    if df.empty:
        return {
            "Label": label, "Days": 0, "Total PnL": "$0.00", "Trades/Day": "0.0",
            "Total Fees": "$0.00", "Net PnL": "$0.00", "Win %": "0%",
            "Profit Factor": "0.00", "Sharpe": "0.00", "Max DD": "$0.00",
            "OverTrade": "0.0%", "PnL Diff": "$0.00"
        }
    
    # Core series
    daily_net = df['real_net_pnl']
    daily_gross = df['real_gross_pnl']
    daily_trades = df['real_trades']
    sim_net = df['sim_net_pnl']
    sim_trades = df['sim_trades']
    
    days = len(df)
    total_pnl = daily_gross.sum()
    total_net = daily_net.sum()
    total_fees = total_pnl - total_net
    trades_per_day = daily_trades.mean()
    
    # Win %
    wins = daily_net[daily_net > 0]
    losses = daily_net[daily_net < 0]
    win_pc = (len(wins) / days) * 100 if days > 0 else 0
    
    # Profit Factor
    sum_wins = wins.sum()
    sum_losses = abs(losses.sum())
    profit_factor = sum_wins / sum_losses if sum_losses > 0 else (np.inf if sum_wins > 0 else 0)
    
    # Sharpe Ratio (annualized, assuming risk-free rate = 0)
    mean_ret = daily_net.mean()
    std_ret = daily_net.std()
    sharpe = (mean_ret / std_ret) * np.sqrt(252) if std_ret > 0 and not np.isnan(std_ret) else 0
    
    # Max Drawdown
    cum_pnl = daily_net.cumsum()
    running_max = cum_pnl.cummax()
    drawdown = cum_pnl - running_max
    max_dd = drawdown.min()
    
    # OverTrade % and PnL Diff
    sum_real_trades = daily_trades.sum()
    sum_sim_trades = sim_trades.sum()
    overtrade_pc = (sum_real_trades / sum_sim_trades - 1) * 100 if sum_sim_trades > 0 else 0
    pnl_diff = total_net - sim_net.sum()
    
    return {
        "Label": label,
        "Days": days,
        "Total PnL": f"${total_pnl:,.2f}",
        "Trades/Day": f"{trades_per_day:.1f}",
        "Total Fees": f"${total_fees:,.2f}",
        "Net PnL": f"${total_net:,.2f}",
        "Win %": f"{win_pc:.1f}%",
        "Profit Factor": f"{profit_factor:.2f}",
        "Sharpe": f"{sharpe:.2f}",
        "Max DD": f"${max_dd:,.2f}",
        "OverTrade": f"{overtrade_pc:.1f}%",
        "PnL Diff": f"${pnl_diff:,.2f}"
    }

def main():
    if not os.path.exists(HIST_CSV):
        print(f"Error: {HIST_CSV} not found.")
        return

    df = pd.read_csv(HIST_CSV)
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date')
    
    # Chart generation
    plt.figure(figsize=(14, 7))
    df['sim_cum'] = df['sim_net_pnl'].cumsum()
    df['real_cum'] = df['real_net_pnl'].cumsum()
    
    plt.plot(df['date'], df['sim_cum'], label='Simulated Cumulative PnL', color='#3498db', linewidth=2, alpha=0.6)
    plt.plot(df['date'], df['real_cum'], label='Live Cumulative PnL', color='#2ecc71', linewidth=2.5)
    
    # Annotate Notes
    notes_df = df[df['notes'].notna() & (df['notes'].str.strip() != "")]
    for _, row in notes_df.iterrows():
        plt.axvline(x=row['date'], color='red', linestyle='--', alpha=0.5, linewidth=1)
        # Add text box for note
        y_pos = row['real_cum']
        plt.annotate(row['notes'], 
                     xy=(row['date'], y_pos),
                     xytext=(10, 15), 
                     textcoords='offset points',
                     bbox=dict(boxstyle='round,pad=0.3', fc='yellow', alpha=0.3),
                     arrowprops=dict(arrowstyle='->', connectionstyle='arc3,rad=.2', color='red', alpha=0.5),
                     fontsize=8)

    plt.title('Terminator Performance: All-Time Cumulative Net PnL', fontsize=14, fontweight='bold')
    plt.ylabel('Net PnL ($)', fontsize=12)
    plt.grid(True, linestyle='--', alpha=0.3)
    plt.legend(loc='upper left')
    plt.savefig(CHART_OUT, dpi=150, bbox_inches='tight')
    plt.close()

    # Stats Calculation
    all_time = calculate_stats(df, "All Time")
    last_5 = calculate_stats(df.tail(5), "Past 5D")
    last_22 = calculate_stats(df.tail(22), "Past 22D")
    
    ytd_df = df[df['date'].dt.year == datetime.now().year]
    ytd = calculate_stats(ytd_df, "YTD")
    
    stats_list = [last_5, last_22, ytd, all_time]
    
    # Build Markdown Table
    md_table = "| Period | Days | Total PnL | Trades/Day | Fees | Net PnL | Win % | Profit Factor | Sharpe | Max DD | Live OverTrade % | Live-Sim Net PnL Diff |\n"
    md_table += "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n"
    for s in stats_list:
        md_table += f"| {s['Label']} | {s['Days']} | {s['Total PnL']} | {s['Trades/Day']} | {s['Total Fees']} | **{s['Net PnL']}** | {s['Win %']} | {s['Profit Factor']} | {s['Sharpe']} | {s['Max DD']} | {s['OverTrade']} | {s['PnL Diff']} |\n"

    # Update README
    with open(README_PATH, 'r') as f:
        content = f.read()

    start_marker = "<!-- STATS_START -->"
    end_marker = "<!-- STATS_END -->"
    
    new_stats_content = f"{start_marker}\n\n## 📈 Performance Statistics\n\n![Performance Curve](eod/assets/performance_curve.png)\n\n{md_table}\n\n*Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n\n{end_marker}"
    
    if start_marker in content and end_marker in content:
        import re
        pattern = re.compile(f"{start_marker}.*?{end_marker}", re.DOTALL)
        updated_content = pattern.sub(new_stats_content, content)
    else:
        updated_content = content + "\n\n---\n" + new_stats_content

    with open(README_PATH, 'w') as f:
        f.write(updated_content)
    
    print("README stats updated successfully.")

if __name__ == "__main__":
    main()
