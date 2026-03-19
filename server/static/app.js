let socket;
let reconnectInterval = 1000;
const maxReconnectInterval = 30000;

// Tab Management
function openTab(evt, tabName) {
    let i, tabcontent, tablinks;
    tabcontent = document.getElementsByClassName("tab-content");
    for (i = 0; i < tabcontent.length; i++) {
        tabcontent[i].classList.remove("active");
    }
    tablinks = document.getElementsByClassName("tab-link");
    for (i = 0; i < tablinks.length; i++) {
        tablinks[i].classList.remove("active");
    }
    document.getElementById(tabName).classList.add("active");
    evt.currentTarget.classList.add("active");
}

// WebSocket Connection
function connect() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host;
    socket = new WebSocket(`${protocol}//${host}/ws`);

    socket.onopen = () => {
        console.log("Connected to Terminal WS");
        document.getElementById('system-status').textContent = 'CONNECTED';
        document.getElementById('system-status').className = 'value status-connected';
        reconnectInterval = 1000;
    };

    socket.onmessage = (event) => {
        const state = JSON.parse(event.data);
        updateUI(state);
    };

    socket.onclose = () => {
        console.log("Disconnected from Terminal WS");
        document.getElementById('system-status').textContent = 'RECONNECTING...';
        document.getElementById('system-status').className = 'value status-connecting';
        
        setTimeout(() => {
            reconnectInterval = Math.min(reconnectInterval * 2, maxReconnectInterval);
            connect();
        }, reconnectInterval);
    };
}

function formatUSD(val) {
    const sign = val >= 0 ? '$' : '-$';
    return sign + Math.abs(val).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function updateUI(state) {
    // 1. Status Bar
    document.getElementById('broker-status').textContent = state.broker_connected ? 'LIVE' : 'OFFLINE';
    document.getElementById('broker-status').className = 'value ' + (state.broker_connected ? 'status-connected' : 'status-disconnected');
    
    const tradingBtn = document.getElementById('toggle-trading-btn');
    document.getElementById('trading-status').textContent = state.trading_enabled ? 'ENABLED' : 'DISABLED';
    document.getElementById('trading-status').className = 'value ' + (state.trading_enabled ? 'status-connected' : 'status-disabled');
    tradingBtn.textContent = state.trading_enabled ? 'Disable trading' : 'Enable trading';

    // 2. Metrics - Sim
    document.getElementById('sim-net-pnl').textContent = formatUSD(state.sim.net_pnl);
    document.getElementById('sim-net-pnl').className = 'value ' + (state.sim.net_pnl >= 0 ? 'green' : 'red');
    document.getElementById('sim-gross-pnl').textContent = formatUSD(state.sim.pnl);
    document.getElementById('sim-fees').textContent = formatUSD(state.sim.fees);
    document.getElementById('sim-trades-count').textContent = state.sim.trades;
    document.getElementById('sim-total-delta').textContent = state.sim.delta.toFixed(3);
    document.getElementById('sim-total-theta').textContent = state.sim.theta.toFixed(2);

    // 3. Metrics - Live
    document.getElementById('live-net-pnl').textContent = formatUSD(state.live.net_pnl);
    document.getElementById('live-net-pnl').className = 'value ' + (state.live.net_pnl >= 0 ? 'green' : 'red');
    document.getElementById('live-gross-pnl').textContent = formatUSD(state.live.pnl);
    document.getElementById('live-fees').textContent = formatUSD(state.live.fees);
    document.getElementById('live-trades-count').textContent = state.live.trades;
    document.getElementById('live-total-delta').textContent = state.live.delta.toFixed(3);
    document.getElementById('live-total-theta').textContent = state.live.theta.toFixed(2);

    // 4. Position Table (Aggregated)
    updatePositionTable(state.live.positions, state.sim.positions);

    // 5. Working Orders
    updateOrdersTable(state.working_orders);

    // 6. Recent Trades
    updateTradesTable('live-trades-tbody', state.live.recent_trades);
    updateTradesTable('sim-trades-tbody', state.sim.recent_trades);

    // 7. Strategies Grid
    updateStrategies(state.strategies);

    // 8. Session Stats
    updateStats(state.stats);

    // 9. Config Table
    updateConfigTable(state.config);

    // 10. Logs
    updateLogs(state.logs);
}

function updatePositionTable(livePos, simPos) {
    const tbody = document.getElementById('positions-tbody');
    tbody.innerHTML = '';
    
    const merged = {};
    simPos.forEach(p => {
        const key = `${p.symbol}-${p.strike}-${p.side}`;
        merged[key] = { ...p, account: 'SIM' };
    });
    livePos.forEach(p => {
        const key = `${p.symbol}-${p.strike}-${p.side}`;
        if (merged[key]) {
             merged[key].qty_sim = merged[key].qty;
             merged[key].qty = p.qty;
             merged[key].account = 'BOTH';
             merged[key].pnl = p.pnl; // Use live PnL for live position
        } else {
             merged[key] = { ...p, account: 'LIVE' };
        }
    });

    Object.values(merged).forEach(p => {
        const row = document.createElement('tr');
        const simQty = p.account === 'SIM' || p.account === 'BOTH' ? (p.qty_sim || p.qty) : '-';
        const liveQty = p.account === 'LIVE' || p.account === 'BOTH' ? (p.qty) : '-';
        
        row.innerHTML = `
            <td>${p.symbol}</td>
            <td class="${p.side === 'CALL' ? 'primary' : 'orange'}">${p.side}</td>
            <td>${p.strike}</td>
            <td class="${simQty === '-' ? '' : 'primary'}">${simQty}</td>
            <td class="${liveQty === '-' ? '' : 'green'}">${liveQty}</td>
            <td class="${p.pnl >= 0 ? 'green' : 'red'}">${formatUSD(p.pnl)}</td>
        `;
        tbody.appendChild(row);
    });
}

function updateOrdersTable(orders) {
    const tbody = document.getElementById('orders-tbody');
    tbody.innerHTML = '';
    if (!orders || orders.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color:var(--text-secondary);">No working orders</td></tr>';
        return;
    }
    orders.forEach(o => {
        const row = document.createElement('tr');
        row.innerHTML = `
            <td>${o.orderId}</td>
            <td>${o.symbol || 'SPX'}</td>
            <td>${o.side || '---'}</td>
            <td>${o.quantity || o.requestedQuantity || 0}</td>
            <td>${o.price || 'MKT'}</td>
            <td class="primary">${o.status}</td>
        `;
        tbody.appendChild(row);
    });
}

function updateTradesTable(tbodyId, trades) {
    const tbody = document.getElementById(tbodyId);
    tbody.innerHTML = '';
    if (!trades || trades.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" style="text-align:center; color:var(--text-secondary);">No recent trades</td></tr>';
        return;
    }
    // Sort by timestamp descending
    const sorted = [...trades].sort((a,b) => new Date(b.ts) - new Date(a.ts));
    sorted.forEach(t => {
        const row = document.createElement('tr');
        const time = new Date(t.ts).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit', second:'2-digit'});
        const summary = t.legs.map(l => `${l.qty > 0 ? '+' : ''}${l.qty} ${l.side[0]}${l.strike}`).join(', ');
        row.innerHTML = `
            <td title="${t.ts}">${time}</td>
            <td>${summary}</td>
            <td>${t.legs.reduce((acc, l) => acc + Math.abs(l.qty), 0)}</td>
            <td class="${t.credit >= 0 ? 'green' : 'red'}">${formatUSD(t.credit / 100)}</td>
        `;
        tbody.appendChild(row);
    });
}

function updateStats(stats) {
    if (!stats) return;
    document.getElementById('stats-total').textContent = stats.total_trades;
    document.getElementById('stats-winners').textContent = stats.winners;
    document.getElementById('stats-losers').textContent = stats.losers;
    document.getElementById('stats-winrate').textContent = (stats.win_rate * 100).toFixed(1) + '%';
    document.getElementById('stats-pnl').textContent = formatUSD(stats.total_pnl);
    document.getElementById('stats-pnl').className = 'value ' + (stats.total_pnl >= 0 ? 'green' : 'red');
    document.getElementById('stats-drawdown').textContent = formatUSD(stats.max_drawdown);
    document.getElementById('stats-duration').textContent = stats.avg_duration.toFixed(1) + ' mins';
}

let configRendered = false;
function updateConfigTable(config) {
    if (!config || configRendered) return;
    const tbody = document.getElementById('config-tbody');
    tbody.innerHTML = '';
    
    const sortedKeys = Object.keys(config).sort();
    sortedKeys.forEach(key => {
        const row = document.createElement('tr');
        let val = config[key];
        if (typeof val === 'object') val = JSON.stringify(val);
        
        row.innerHTML = `
            <td><code>${key}</code></td>
            <td style="word-break: break-all;">${val}</td>
        `;
        tbody.appendChild(row);
    });
    configRendered = true; // Only render once or on significant change if you wish
}

function updateStrategies(strategies) {
    const grid = document.getElementById('strategy-grid');
    // We want to avoid full re-render to keep fold states
    for (let sid in strategies) {
        let s = strategies[sid];
        let card = document.getElementById(`strat-card-${sid}`);
        
        if (!card) {
            card = document.createElement('div');
            card.id = `strat-card-${sid}`;
            card.className = 'strategy-card';
            grid.appendChild(card);
        }

        const pnlClass = s.pnl >= 0 ? 'green' : 'red';
        const posCount = s.positions.length;
        
        card.innerHTML = `
            <div class="strat-header">
                <div>
                    <span class="strat-id">${sid}</span>
                    <span class="status-dot ${s.traded ? 'active' : ''}"></span>
                </div>
                <span class="strat-pnl ${pnlClass}">${formatUSD(s.pnl)}</span>
            </div>
            <div class="metrics-grid" style="grid-template-columns: repeat(4, 1fr);">
                <div class="metric-card">
                    <span class="label">Positions</span>
                    <span class="value">${posCount}</span>
                </div>
                <div class="metric-card">
                    <span class="label">Status</span>
                    <span class="value" style="font-size: 0.9rem;">${s.traded ? 'TRADED' : 'WAITING'}</span>
                </div>
            </div>
            
            <div class="strat-legs">
                <div class="foldable" onclick="toggleFold(this)">Leg Details (${posCount})</div>
                <div class="fold-content">
                    ${s.positions.map(p => `
                        <div class="leg-row">
                            <span class="side ${p.side === 'CALL' ? 'primary' : 'orange'}">${p.side} ${p.strike}</span>
                            <span>Qty: ${p.qty}</span>
                            <span class="${p.pnl >= 0 ? 'green' : 'red'}">${formatUSD(p.pnl)}</span>
                        </div>
                    `).join('')}
                    ${posCount === 0 ? '<div style="color:var(--text-secondary); font-size:0.8rem;">Flat</div>' : ''}
                </div>

                <div class="foldable" onclick="toggleFold(this)" style="margin-top:0.5rem;">Trade History</div>
                <div class="fold-content">
                    ${s.history.map(t => {
                        const time = new Date(t.ts).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
                        return `
                            <div class="history-item">
                                <span class="ts">${time}</span>
                                <span class="purpose">${t.purpose.toUpperCase()}</span>
                                <span class="${t.credit >= 0 ? 'green' : 'red'}" style="float:right;">${formatUSD(t.credit/100)}</span>
                                <div style="color:var(--text-secondary); font-size:0.7rem; margin-top:0.2rem;">
                                    ${t.legs.map(l => `${l.qty > 0 ? '+' : ''}${l.qty} ${l.side[0]}${l.strike}`).join(', ')}
                                </div>
                            </div>
                        `;
                    }).join('')}
                    ${s.history.length === 0 ? '<div style="color:var(--text-secondary); font-size:0.8rem;">No history</div>' : ''}
                </div>
            </div>
        `;
    }
}

function toggleFold(el) {
    el.classList.toggle('open');
}

let lastLogCount = 0;
function updateLogs(logs) {
    const consoleEl = document.getElementById('log-console');
    if (!logs || logs.length === 0) return;
    
    // If fewer logs than before, user likely cleared or we reconnected
    if (logs.length < lastLogCount) {
        consoleEl.innerHTML = '';
        lastLogCount = 0;
    }

    // Only append new logs
    const newLogs = logs.slice(lastLogCount);
    newLogs.forEach(msg => {
        const entry = document.createElement('div');
        entry.className = 'log-entry';
        
        let levelClass = '';
        if (msg.includes('ERROR')) levelClass = 'red';
        else if (msg.includes('WARNING')) levelClass = 'orange';
        else if (msg.includes('INFO')) levelClass = 'primary';
        
        entry.innerHTML = `<span class="${levelClass}">${msg}</span>`;
        consoleEl.prepend(entry); // Newest at top
    });
    
    lastLogCount = logs.length;
}

function clearLogs() {
    document.getElementById('log-console').innerHTML = '';
    lastLogCount = 0;
}

// Action Buttons
document.getElementById('toggle-trading-btn').addEventListener('click', () => {
    fetch('/api/trading/toggle', { method: 'POST' });
});

document.getElementById('reconnect-broker-btn').addEventListener('click', () => {
    fetch('/api/trading/reconnect', { method: 'POST' });
});

// Init
window.onclick = function(event) {
    // Close dropdowns if any
}

connect();
