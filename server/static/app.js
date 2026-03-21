let socket;
let reconnectInterval = 1000;
const maxReconnectInterval = 30000;
let strategyFoldStates = {}; // { sid: boolean (true = expanded) }
let portfolioFoldStates = { sim: true, live: true }; // { sim: boolean, live: boolean } (true = collapsed)

// Trade Confirmation State
let tradeTimer = null;
let tradeTimeLeft = 10;
let isTradeTimerPaused = false;
const TRADE_TIMEOUT_SEC = 10;
let spxChart, pnlChart;
let lastChartUpdate = 0;

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
        const statusEl = document.getElementById('system-status');
        statusEl.textContent = 'CONNECTED';
        statusEl.className = 'value status-connected';
        reconnectInterval = 1000;
    };

    socket.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'state_update') {
            updateUI(data.state);
        } else if (data.type === 'history_init') {
            populateCharts(data.history);
        } else if (data.type === 'alert') {
            handleAlert(data);
        } else if (data.type === 'trade_signal') {
            showTradeModal(data);
        }
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

function handleAlert(data) {
    console.log("System Alert:", data);
    
    // Play sound
    const soundPath = data.level === 'error' ? '/static/error.mp3' : '/static/chime.mp3';
    const audio = new Audio(soundPath);
    audio.play().catch(e => console.error("Audio play failed:", e));
    
    // Show a temporary browser notification if allowed
    if (Notification.permission === "granted") {
        new Notification(data.title || "Terminator Alert", {
            body: data.message || "Action required",
            icon: '/static/favicon.ico'
        });
    } else if (Notification.permission !== "denied") {
        Notification.requestPermission();
    }
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
    const simNetPnl = formatUSD(state.sim.net_pnl);
    const simPnlClass = (state.sim.net_pnl >= 0 ? 'green' : 'red');
    
    document.getElementById('sim-margin').textContent = formatUSD(state.sim.margin);
    document.getElementById('sim-trades-count').textContent = state.sim.trades;
    document.getElementById('sim-net-pnl').textContent = simNetPnl;
    document.getElementById('sim-net-pnl').className = 'value ' + simPnlClass;
    document.getElementById('sim-net-pnl-summary').textContent = simNetPnl;
    document.getElementById('sim-net-pnl-summary').className = 'value ' + simPnlClass;
    
    document.getElementById('sim-total-delta').textContent = state.sim.delta.toFixed(3);
    document.getElementById('sim-fees').textContent = formatUSD(state.sim.fees);
    document.getElementById('sim-gross-pnl').textContent = formatUSD(state.sim.pnl);
    document.getElementById('sim-realized-pnl').textContent = formatUSD(state.sim.realized);
    document.getElementById('sim-unrealized-pnl').textContent = formatUSD(state.sim.unrealized);
    document.getElementById('sim-total-theta').textContent = state.sim.theta.toFixed(2);

    // 3. Metrics - Live
    const liveNetPnl = formatUSD(state.live.net_pnl);
    const livePnlClass = (state.live.net_pnl >= 0 ? 'green' : 'red');

    document.getElementById('live-margin').textContent = formatUSD(state.live.margin);
    document.getElementById('live-trades-count').textContent = state.live.trades;
    document.getElementById('live-net-pnl').textContent = liveNetPnl;
    document.getElementById('live-net-pnl').className = 'value ' + livePnlClass;
    document.getElementById('live-net-pnl-summary').textContent = liveNetPnl;
    document.getElementById('live-net-pnl-summary').className = 'value ' + livePnlClass;
    
    document.getElementById('live-total-delta').textContent = state.live.delta.toFixed(3);
    document.getElementById('live-fees').textContent = formatUSD(state.live.fees);
    document.getElementById('live-gross-pnl').textContent = formatUSD(state.live.pnl);
    document.getElementById('live-realized-pnl').textContent = formatUSD(state.live.realized);
    document.getElementById('live-unrealized-pnl').textContent = formatUSD(state.live.unrealized);
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

    // 11. Real-time Chart Update (Incremental)
    updateCharts(state);
}

function updateCharts(state) {
    if (!spxChart || !pnlChart) return;
    
    const now = new Date(state.ts).getTime();
    // Only push to chart every 30 seconds to keep performance smooth
    if (now - lastChartUpdate >= 30000) { 
        lastChartUpdate = now;
        
        if (state.spx) {
            spxChart.data.datasets[0].data.push({ x: now, y: state.spx });
            spxChart.update('none');
        }
        
        pnlChart.data.datasets[0].data.push({ x: now, y: state.live.net_pnl });
        pnlChart.data.datasets[1].data.push({ x: now, y: state.sim.net_pnl });
        pnlChart.update('none');
    }
}

function populateCharts(history) {
    if (!history || !spxChart || !pnlChart) return;
    
    const spxData = history.filter(p => p.spx).map(p => ({ x: new Date(p.ts).getTime(), y: p.spx }));
    const livePnlData = history.map(p => ({ x: new Date(p.ts).getTime(), y: p.live_pnl }));
    const simPnlData = history.map(p => ({ x: new Date(p.ts).getTime(), y: p.sim_pnl }));
    
    spxChart.data.datasets[0].data = spxData;
    pnlChart.data.datasets[0].data = livePnlData;
    pnlChart.data.datasets[1].data = simPnlData;
    
    spxChart.update();
    pnlChart.update();
    
    if (history.length > 0) {
        lastChartUpdate = new Date(history[history.length - 1].ts).getTime();
    }
}

function initCharts() {
    const commonOptions = {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        scales: {
            x: {
                type: 'time',
                time: { unit: 'minute', displayFormats: { minute: 'HH:mm' } },
                grid: { color: 'rgba(255,255,255,0.05)' },
                ticks: { color: '#8b949e', font: { size: 10 } }
            },
            y: { 
                grid: { color: 'rgba(255,255,255,0.05)' },
                ticks: { color: '#8b949e', font: { size: 10 } }
            }
        },
        plugins: { 
            legend: { 
                display: true,
                position: 'top',
                labels: { color: '#8b949e', boxWidth: 12, font: { size: 11 } } 
            },
            tooltip: { mode: 'index', intersect: false }
        }
    };

    const spxCtx = document.getElementById('spx-chart').getContext('2d');
    spxChart = new Chart(spxCtx, {
        type: 'line',
        data: { datasets: [{ 
            label: 'SPX', 
            data: [], 
            borderColor: '#58a6ff', 
            borderWidth: 2, 
            pointRadius: 0, 
            fill: false, 
            tension: 0.1 
        }] },
        options: commonOptions
    });

    const pnlCtx = document.getElementById('pnl-chart').getContext('2d');
    pnlChart = new Chart(pnlCtx, {
        type: 'line',
        data: {
            datasets: [
                { 
                    label: 'Live PnL', 
                    data: [], 
                    borderColor: '#2ea043', 
                    borderWidth: 2, 
                    pointRadius: 0, 
                    fill: false, 
                    tension: 0.1 
                },
                { 
                    label: 'Sim PnL', 
                    data: [], 
                    borderColor: '#58a6ff', 
                    borderWidth: 2, 
                    borderDash: [5, 5], 
                    pointRadius: 0, 
                    fill: false, 
                    tension: 0.1 
                }
            ]
        },
        options: commonOptions
    });
}

function updatePositionTable(livePos, simPos) {
    const tbody = document.getElementById('positions-tbody');
    tbody.innerHTML = '';
    
    const merged = {};
    simPos.forEach(p => {
        const key = `${p.symbol}-${p.strike}-${p.side}`;
        merged[key] = { 
            strike: p.strike, side: p.side, symbol: p.symbol,
            qty_sim: p.qty, qty_live: 0,
            delta: p.delta, bid: p.bid, ask: p.ask
        };
    });
    livePos.forEach(p => {
        const key = `${p.symbol}-${p.strike}-${p.side}`;
        if (merged[key]) {
            merged[key].qty_live = p.qty;
            merged[key].delta = p.delta;
            merged[key].bid = p.bid;
            merged[key].ask = p.ask;
        } else {
            merged[key] = { 
                strike: p.strike, side: p.side, symbol: p.symbol,
                qty_sim: 0, qty_live: p.qty,
                delta: p.delta, bid: p.bid, ask: p.ask
            };
        }
    });

    // Sort by strike ascending
    const sortedPositions = Object.values(merged).sort((a, b) => a.strike - b.strike);
    
    sortedPositions.forEach(p => {
        const row = document.createElement('tr');
        const sQty = p.qty_sim || 0;
        const lQty = p.qty_live || 0;
        const diff = sQty - lQty;
        
        row.innerHTML = `
            <td>${p.strike}</td>
            <td class="${p.side === 'CALL' ? 'primary' : 'orange'}">${p.side}</td>
            <td class="${sQty !== 0 ? 'primary' : ''}">${sQty || '-'}</td>
            <td class="${lQty !== 0 ? 'green' : ''}">${lQty || '-'}</td>
            <td class="${diff === 0 ? '' : 'red'}">${diff || '-'}</td>
            <td>${(typeof p.delta === 'number') ? p.delta.toFixed(3) : '-'}</td>
            <td>${(typeof p.bid === 'number') ? p.bid.toFixed(2) : '-'}</td>
            <td>${(typeof p.ask === 'number') ? p.ask.toFixed(2) : '-'}</td>
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
        const qtySum = t.legs.reduce((acc, l) => acc + Math.abs(l.qty), 0);
        row.innerHTML = `
            <td title="${t.ts}">${time}</td>
            <td>${summary}</td>
            <td>${qtySum}</td>
            <td class="${t.credit >= 0 ? 'green' : 'red'}">${formatUSD(t.credit / 100)}</td>
        `;
        tbody.appendChild(row);
    });
}

function updateStats(stats) {
    if (!stats) return;
    document.getElementById('stats-total').textContent = stats.total_trades || 0;
    document.getElementById('stats-winners').textContent = stats.winners || 0;
    document.getElementById('stats-losers').textContent = stats.losers || 0;
    document.getElementById('stats-winrate').textContent = (stats.win_rate ? (stats.win_rate * 100).toFixed(1) : '0.0') + '%';
    
    const pnl = stats.total_pnl || 0;
    document.getElementById('stats-pnl').textContent = formatUSD(pnl);
    document.getElementById('stats-pnl').className = 'value ' + (pnl >= 0 ? 'green' : 'red');
    
    document.getElementById('stats-drawdown').textContent = formatUSD(stats.max_drawdown || 0);
    document.getElementById('stats-duration').textContent = (stats.avg_duration ? stats.avg_duration.toFixed(1) : '0.0') + ' mins';
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
        const isExpanded = strategyFoldStates[sid] || false;
        
        card.innerHTML = `
            <div class="strat-header foldable-header ${isExpanded ? 'active' : ''}" onclick="toggleStrategyFold('${sid}')">
                <div class="strat-title-group">
                    <span class="fold-icon">${isExpanded ? '▼' : '▶'}</span>
                    <span class="strat-id">${sid}</span>
                    <span class="status-dot ${s.traded ? 'active' : ''}"></span>
                </div>
                <div class="strat-pnl-group">
                    <span class="strat-pnl ${pnlClass}">${formatUSD(s.pnl)}</span>
                </div>
            </div>
            
            <div class="strat-details-body ${isExpanded ? 'show' : 'hide'}">
                <div class="metrics-grid" style="grid-template-columns: repeat(2, 1fr); margin-top: 1rem; margin-bottom: 1rem;">
                    <div class="metric-card">
                        <span class="label">Positions</span>
                        <span class="value">${posCount}</span>
                    </div>
                    <div class="metric-card">
                        <span class="label">Status Today</span>
                        <span class="value" style="font-size: 0.9rem;">${s.traded ? 'TRADED' : 'WAITING'}</span>
                    </div>
                </div>
                
                <h4 style="margin: 0.8rem 0 0.4rem 0; font-size: 0.8rem; color: var(--text-secondary); text-transform: uppercase;">Current Positions</h4>
                <div class="strat-legs">
                    ${s.positions.map(p => `
                        <div class="leg-row">
                            <span class="side ${p.side === 'CALL' ? 'primary' : 'orange'}">${p.side} ${p.strike}</span>
                            <span>Qty: ${p.qty}</span>
                            <span class="${p.pnl >= 0 ? 'green' : 'red'}">${formatUSD(p.pnl)}</span>
                        </div>
                    `).join('')}
                    ${posCount === 0 ? '<div style="color:var(--text-secondary); font-size:0.8rem;">Flat</div>' : ''}
                </div>

                <h4 style="margin: 1.2rem 0 0.4rem 0; font-size: 0.8rem; color: var(--text-secondary); text-transform: uppercase;">Trade History</h4>
                <div class="table-container" style="background: rgba(0,0,0,0.1); margin-top: 0.5rem; border-color: rgba(255,255,255,0.05);">
                    <table>
                        <thead>
                            <tr>
                                <th style="padding: 0.5rem; font-size: 0.75rem;">Time</th>
                                <th style="padding: 0.5rem; font-size: 0.75rem;">Trade</th>
                                <th style="padding: 0.5rem; font-size: 0.75rem;">Qty</th>
                                <th style="padding: 0.5rem; font-size: 0.75rem;">Credit</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${s.history.map(t => {
                                const time = new Date(t.ts).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
                                const summary = t.legs.map(l => `${l.qty > 0 ? '+' : ''}${l.qty} ${l.side[0]}${l.strike}`).join(', ');
                                const qtySum = t.legs.reduce((acc, l) => acc + Math.abs(l.qty), 0);
                                return `
                                    <tr>
                                        <td style="padding: 0.5rem; font-size: 0.8rem;">${time}</td>
                                        <td style="padding: 0.5rem; font-size: 0.8rem;">${summary}</td>
                                        <td style="padding: 0.5rem; font-size: 0.8rem;">${qtySum}</td>
                                        <td style="padding: 0.5rem; font-size: 0.8rem;" class="${t.credit >= 0 ? 'green' : 'red'}">${formatUSD(t.credit/100)}</td>
                                    </tr>
                                `;
                            }).join('')}
                            ${s.history.length === 0 ? '<tr><td colspan="4" style="text-align:center; padding: 1rem; color:var(--text-secondary); font-size:0.8rem;">No history</td></tr>' : ''}
                        </tbody>
                    </table>
                </div>
            </div>
        `;
    }
}

function toggleStrategyFold(sid) {
    strategyFoldStates[sid] = !strategyFoldStates[sid];
}

function togglePortfolioFold(type) {
    portfolioFoldStates[type] = !portfolioFoldStates[type];
    const card = document.getElementById(`${type}-portfolio-card`);
    if (portfolioFoldStates[type]) {
        card.classList.add('collapsed');
    } else {
        card.classList.remove('collapsed');
    }
}

function showTradeModal(tradeData) {
    if (!tradeData) return;
    
    document.getElementById('modal-strat-title').textContent = `Strategy: ${tradeData.strat_id || 'N/A'}`;
    
    const orderListEl = document.getElementById('modal-order-list');
    orderListEl.innerHTML = ''; // Clear existing
    
    // Support either a single trade object or an array of orders
    const orders = tradeData.orders || [];
    if (orders.length === 0 && tradeData.legs) {
        // Fallback for single legacy trade
        orders.push({
            type: 'TRADE',
            qty: 1,
            desc: tradeData.legs.map(l => `${l.side} ${l.strike} x${l.qty}`).join(' | '),
            credit: tradeData.credit
        });
    }

    orders.forEach((order, idx) => {
        const orderCard = document.createElement('div');
        const typeClass = (order.type || 'TRADE').toLowerCase();
        orderCard.className = `order-card type-${typeClass}`;
        orderCard.innerHTML = `
            <div class="order-header">
                <span class="order-title">Order #${idx + 1}: ${order.type || 'TRADE'}</span>
                <span class="order-qty">Qty: ${order.qty || 1}</span>
            </div>
            <div class="order-details-mini">
                <div class="modal-detail">
                    <span class="label">Structure</span>
                    <span class="value">${order.desc || 'N/A'}</span>
                </div>
                <div class="modal-detail">
                    <span class="label">Target Price</span>
                    <span class="value">${order.credit || '$0.00'}</span>
                </div>
            </div>
        `;
        orderListEl.appendChild(orderCard);
    });

    document.getElementById('modal-total-credit').textContent = tradeData.total_credit || '$0.00';
    
    // Current Strategy ID for confirmation
    window.currentModalStratId = tradeData.strat_id;
    
    // Reset Timer
    tradeTimeLeft = tradeData.timeout || TRADE_TIMEOUT_SEC;
    isTradeTimerPaused = false;
    document.getElementById('modal-pause-btn').textContent = 'Pause Timer';
    updateTradeTimerUI();
    
    const modal = document.getElementById('trade-modal');
    modal.classList.add('show');

    // Start Timer
    if (tradeTimer) clearInterval(tradeTimer);
    tradeTimer = setInterval(updateTradeTimer, 1000);
}

function updateTradeTimer() {
    if (isTradeTimerPaused) return;
    
    tradeTimeLeft--;
    updateTradeTimerUI();
    
    if (tradeTimeLeft <= 0) {
        console.log("Timer expired, auto-confirming trade...");
        confirmLiveTrade();
    }
}

function updateTradeTimerUI() {
    const bar = document.getElementById('modal-timer-bar');
    const stat = document.getElementById('modal-timer-stat');
    const pct = Math.max(0, (tradeTimeLeft / TRADE_TIMEOUT_SEC) * 100);
    
    if (bar) bar.style.width = pct + '%';
    if (stat) stat.textContent = isTradeTimerPaused ? 'Timer Paused' : `Auto-sending in ${tradeTimeLeft}s`;
    
    // Visual feedback for urgency
    if (bar) {
        bar.className = 'timer-progress';
        if (tradeTimeLeft <= 3) bar.classList.add('danger');
        else if (tradeTimeLeft <= 6) bar.classList.add('warning');
    }
}

function toggleTradeTimer() {
    isTradeTimerPaused = !isTradeTimerPaused;
    const btn = document.getElementById('modal-pause-btn');
    if (btn) btn.textContent = isTradeTimerPaused ? 'Resume Timer' : 'Pause Timer';
    updateTradeTimerUI();
}

function closeTradeModal() {
    if (tradeTimer) {
        clearInterval(tradeTimer);
        tradeTimer = null;
    }
    const modal = document.getElementById('trade-modal');
    if (modal) modal.classList.remove('show');
}

function dismissTrade() {
    console.log("Trade dismissed");
    if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({
            action: 'dismiss_trade',
            strat_id: window.currentModalStratId
        }));
    }
    closeTradeModal();
}

function confirmLiveTrade() {
    console.log("Order confirmed");
    if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({
            action: 'confirm_trade',
            strat_id: window.currentModalStratId
        }));
    }
    closeTradeModal();
}

// For demonstration purposes
window.demoTradeConfirmation = function() {
    showTradeModal({
        strat_id: 'SPX-20260319-V4',
        total_credit: '$1,850.50',
        orders: [
            { type: 'SELL TO OPEN', desc: '0DTE 5000 Call (Leg 1-4)', qty: 10, credit: '$1,200.00' },
            { type: 'SELL TO OPEN', desc: '0DTE 5050 Put (Leg 5-6)', qty: 10, credit: '$650.50' }
        ]
    });
};

let lastLogCount = 0;
function updateLogs(logs) {
    const consoleEl = document.getElementById('log-console');
    if (!logs || logs.length === 0) return;
    
    if (logs.length < lastLogCount) {
        consoleEl.innerHTML = '';
        lastLogCount = 0;
    }

    const newLogs = logs.slice(lastLogCount);
    newLogs.forEach(msg => {
        const entry = document.createElement('div');
        entry.className = 'log-entry';
        
        let levelClass = '';
        if (msg.includes('ERROR')) levelClass = 'red';
        else if (msg.includes('WARNING')) levelClass = 'orange';
        else if (msg.includes('INFO')) levelClass = 'primary';
        
        entry.innerHTML = `<span class="${levelClass}">${msg}</span>`;
        consoleEl.prepend(entry);
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

initCharts();
connect();
