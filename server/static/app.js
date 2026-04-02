let socket;
let reconnectInterval = 1000;
const maxReconnectInterval = 30000;
let strategyFoldStates = {}; // { sid: boolean (true = expanded) }
let portfolioFoldStates = { sim: true, live: true }; // { sim: boolean, live: boolean } (true = collapsed)

// Trade Confirmation State
let tradeTimer = null;
let tradeTimeLeft = 10;
let isTradeTimerPaused = false;
let pendingDismissStratId = null; // Queued dismiss to retry if WS was not open
let currentTradeOrders = []; // Task #28: Global store for adjustments
const TRADE_TIMEOUT_SEC = 10;
let spxChart, pnlChart;
let lastChartUpdate = 0;
let isMuted = localStorage.getItem('isMuted') === 'true';
let currentVersion = null; // Track backend version for auto-refresh
let latencyHistory = []; // Buffer for SMA
let lastSeenExchangeTs = 0; // Filter sawtooth jitter
const SMA_WINDOW = 10;    // Number of real data updates to average

// Initialize Mute UI
function initMuteUI() {
    const icon = document.getElementById('mute-icon');
    if (icon) icon.textContent = isMuted ? '🔕' : '🔔';
    
    const btn = document.getElementById('mute-toggle-btn');
    if (btn) {
        btn.addEventListener('click', () => {
            isMuted = !isMuted;
            localStorage.setItem('isMuted', isMuted);
            icon.textContent = isMuted ? '🔕' : '🔔';
            console.log(`Sounds ${isMuted ? 'muted' : 'unmuted'}`);
        });
    }
}

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
        if (statusEl) {
            statusEl.textContent = 'CONNECTED';
            statusEl.className = 'value status-connected';
        }
        const banner = document.getElementById('disconnect-banner');
        if (banner) banner.remove();
        reconnectInterval = 1000;
        // Re-sync pause state with backend after reconnect (fixes lost pause message on disconnect)
        if (isTradeTimerPaused) {
            socket.send(JSON.stringify({ action: 'toggle_trade_pause', is_paused: true }));
        }
        // Retry a dismiss that failed to send because WS was not open
        if (pendingDismissStratId) {
            socket.send(JSON.stringify({ action: 'dismiss_trade', strat_id: pendingDismissStratId }));
            console.log(`Retried queued dismiss for ${pendingDismissStratId} on reconnect`);
            pendingDismissStratId = null;
        }
    };

    socket.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'state_update') {
            // Auto-refresh on deployment/restart
            if (data.state.version) {
                if (currentVersion && data.state.version !== currentVersion) {
                    console.log("New version detected. Reloading...");
                    window.location.reload();
                }
                currentVersion = data.state.version;
            }
            updateUI(data.state);
        } else if (data.type === 'history_init') {
            populateCharts(data.history);
            if (data.config) updateConfigTable(data.config);
        } else if (data.type === 'alert') {
            handleAlert(data);
        } else if (data.type === 'trade_signal') {
            // Clear any stale pending dismiss when a fresh trade signal arrives
            pendingDismissStratId = null;
            
            // Task: Force refresh. If a modal is already showing (even if paused), 
            // it's likely stale context for a trade currently being auto-executed or replaced.
            // We clear it to ensure the fresh signal's timer and price context takes precedence.
            closeTradeModal();
            
            // Task: Play HIGH priority alert for trade signals
            playSound('alert');
            showTradeModal(data);
        } else if (data.type === 'trade_action') {
            console.log("Remote trade action received:", data);
            if (data.action === 'close_modal') {
                closeTradeModal();
            } else if (data.action === 'pause_sync') {
                isTradeTimerPaused = data.is_paused;
                const btn = document.getElementById('modal-pause-btn');
                if (btn) btn.textContent = isTradeTimerPaused ? 'Resume Timer' : 'Pause Timer';
                updateTradeTimerUI();
            }
        }
    };

    socket.onclose = () => {
        console.log("Disconnected from Terminal WS");
        const statusEl = document.getElementById('system-status');
        if (statusEl) {
            statusEl.textContent = 'DISCONNECTED - RECONNECTING...';
            statusEl.className = 'value status-disconnected flash-alert';
        }
        
        // Show a temporary banner if not exists
        if (!document.getElementById('disconnect-banner')) {
            const banner = document.createElement('div');
            banner.id = 'disconnect-banner';
            banner.className = 'disconnect-banner';
            banner.textContent = '⚠️ Lost connection to server. Attempting to reconnect...';
            document.body.prepend(banner);
        }

        setTimeout(() => {
            reconnectInterval = Math.min(reconnectInterval * 2, maxReconnectInterval);
            connect();
        }, reconnectInterval);
    };
}

function playSound(level) {
    if (isMuted) return; // Task: Respect mute state
    const soundPath = level === 'error' ? 'error.mp3' : 'chime.mp3';
    const audio = new Audio(soundPath);
    audio.play().catch(e => console.warn("Audio play blocked (needs user interaction):", e));
}

function handleAlert(data) {
    console.log("System Alert:", data);
    
    // Play sound
    playSound(data.level);
    
    // Show a temporary browser notification if allowed
    if (Notification.permission === "granted") {
        new Notification(data.title || "Terminator Alert", {
            body: data.message || "Action required",
            icon: 'favicon.ico'
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

    const serverEl = document.getElementById('server-name');
    if (serverEl && state.server_name) {
        if (state.server_name === 'production-server') {
            serverEl.textContent = 'MAIN';
        } else {
            const parts = state.server_name.split('-');
            serverEl.textContent = parts[parts.length - 1].toUpperCase();
        }
        serverEl.className = 'value ' + (state.server_name === 'production-server' ? 'status-connected' : 'primary');
    }
    
    const tradingBtn = document.getElementById('toggle-trading-btn');
    document.getElementById('trading-status').textContent = state.trading_enabled ? 'ENABLED' : 'DISABLED';
    document.getElementById('trading-status').className = 'value ' + (state.trading_enabled ? 'status-connected' : 'status-disabled');
    tradingBtn.textContent = state.trading_enabled ? 'Disable trading' : 'Enable trading';

    // Exchange Clock (Chicago)
    if (state.exchange_ts && state.exchange_ts > 0) {
        const d = new Date(state.exchange_ts);
        const timeStr = d.toLocaleTimeString('en-US', { 
            hour12: false, 
            hour: '2-digit', 
            minute: '2-digit', 
            second: '2-digit',
            timeZone: 'America/Chicago'
        });
        const clockEl = document.getElementById('exchange-time');
        if (clockEl) clockEl.textContent = timeStr;
    }

    if (state.latency_ms !== undefined) {
        const latencyEl = document.getElementById('latency-ms');
        if (latencyEl) {
            // Update SMA buffer ONLY when a new exchange update actually arrives
            // This filters out the "sawtooth" jitter caused by heartbeat frequency vs update frequency
            if (state.exchange_ts > lastSeenExchangeTs) {
                lastSeenExchangeTs = state.exchange_ts;
                latencyHistory.push(state.latency_ms);
                if (latencyHistory.length > SMA_WINDOW) {
                    latencyHistory.shift();
                }
            }

            // Always calculate moving average if we have samples
            if (latencyHistory.length > 0) {
                const avgLatency = Math.round(latencyHistory.reduce((a, b) => a + b, 0) / latencyHistory.length);
                latencyEl.textContent = `${avgLatency}ms`;
                
                // Color code latency based on the smoothed value
                if (avgLatency > 1000) {
                    latencyEl.className = 'value status-disconnected';
                } else if (avgLatency > 500) {
                    latencyEl.className = 'value status-disabled'; 
                } else {
                    latencyEl.className = 'value'; // Normal
                }
            }
        }
    }

    // SPX Price
    const spxEl = document.getElementById('spx-price');
    if (spxEl) {
        if (state.spx) {
            spxEl.textContent = state.spx.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        } else {
            spxEl.textContent = '----.--';
        }
    }

    // VIX Price
    const vixEl = document.getElementById('vix-price');
    if (vixEl) {
        if (state.vix) {
            vixEl.textContent = state.vix.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        } else {
            vixEl.textContent = '----.--';
        }
    }

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


    // 9. Config Table
    updateConfigTable(state.config);

    // 10. Logs
    updateLogs(state.logs);

    // 11. Real-time Chart Update (Incremental)
    updateCharts(state);

    // 12. Trade Modal Reconciliation (Resilience Fix)
    try {
        const modal = document.getElementById('trade-modal');
        const modalShowing = modal?.classList.contains('show');
        
        if (state.pending_trade) {
            // Re-open modal if a trade is pending but modal is not showing, 
            // OR the strategy has changed (rare race condition)
            if (!modalShowing || window.currentModalStratId !== state.pending_trade.strat_id) {
                console.log("Resuming pending trade from heartbeat:", state.pending_trade.strat_id);
                pendingDismissStratId = null;
                showTradeModal(state.pending_trade);
            }
        } else {
            // If the server heartbeat says no trade is pending, ensure the modal is closed
            if (modalShowing && window.isTradeTimerPaused === false) {
                 closeTradeModal();
            }
        }
    } catch (err) {
        console.error("Error in modal reconciliation heartbeat:", err);
    }
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
    // FRONT-1 Fix: Destroy existing charts if they exist to prevent memory leaks
    if (spxChart) spxChart.destroy();
    if (pnlChart) pnlChart.destroy();

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
                    tension: 0.1,
                    order: 2 // Higher order = Bottom layer
                },
                { 
                    label: 'Sim PnL', 
                    data: [], 
                    borderColor: '#58a6ff', 
                    borderWidth: 2, 
                    borderDash: [5, 5], 
                    pointRadius: 0, 
                    fill: false, 
                    tension: 0.1,
                    order: 1 // Lower order = Top layer
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
    const cancelAllBtn = document.getElementById('cancel-all-orders-btn');
    tbody.innerHTML = '';
    
    if (!orders || orders.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; color:var(--text-secondary);">No working orders</td></tr>';
        if (cancelAllBtn) {
            cancelAllBtn.disabled = true;
            cancelAllBtn.style.opacity = '0.4';
            cancelAllBtn.style.cursor = 'not-allowed';
        }
        return;
    }
    
    if (cancelAllBtn) {
        cancelAllBtn.disabled = false;
        cancelAllBtn.style.opacity = '1';
        cancelAllBtn.style.cursor = 'pointer';
    }
    orders.forEach(o => {
        const row = document.createElement('tr');
        row.innerHTML = `
            <td>${o.time || '--:--:--'}</td>
            <td title="${o.symbol}">${o.symbol || 'SPX'}</td>
            <td class="${o.side === 'credit' ? 'green' : 'orange'}">${o.side || '---'}</td>
            <td>${o.qty || 0}</td>
            <td>$${(o.price || 0).toFixed(2)}</td>
            <td class="primary">${o.status}</td>
            <td style="text-align: right;">
                <button class="btn btn-danger btn-small" onclick="confirmCancelOrder('${o.id}', '${o.symbol}')">Cancel</button>
            </td>
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


let configRendered = false;
function updateConfigTable(config) {
    if (!config) return;
    const tbody = document.getElementById('config-tbody');
    if (!tbody) return;
    
    tbody.innerHTML = '';
    const sortedKeys = Object.keys(config).sort();
    sortedKeys.forEach(key => {
        const row = document.createElement('tr');
        let val = config[key];
        // Stringify objects for better readability
        if (typeof val === 'object' && val !== null) {
            val = JSON.stringify(val);
        }
        
        row.innerHTML = `
            <td><code>${key}</code></td>
            <td style="word-break: break-all;">${val}</td>
        `;
        tbody.appendChild(row);
    });
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
        const historyArr = s.history || [];
        const posCount = (s.positions && s.positions.length) ? s.positions.length : 0;
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
                    ${(s.positions || []).map(p => `
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
                            ${historyArr.map(t => {
                                const time = new Date(t.ts).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
                                const summary = (t.legs || []).map(l => `${l.qty > 0 ? '+' : ''}${l.qty} ${l.side[0]}${l.strike}`).join(', ');
                                const qtySum = (t.legs || []).reduce((acc, l) => acc + Math.abs(l.qty), 0);
                                return `
                                    <tr>
                                        <td style="padding: 0.5rem; font-size: 0.8rem;">${time}</td>
                                        <td style="padding: 0.5rem; font-size: 0.8rem;">${summary}</td>
                                        <td style="padding: 0.5rem; font-size: 0.8rem;">${qtySum}</td>
                                    </tr>
                                `;
                            }).join('')}
                            ${historyArr.length === 0 ? '<tr><td colspan="4" style="text-align:center; padding: 1rem; color:var(--text-secondary); font-size:0.8rem;">No history</td></tr>' : ''}
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
    if (!tradeData) {
        console.error("showTradeModal called with null data!");
        return;
    }
    console.log("showTradeModal() triggered for:", tradeData.strat_id);
    
    try {
        const titleEl = document.getElementById('modal-strat-title');
        if (titleEl) titleEl.textContent = `Strategy: ${tradeData.strat_id || 'N/A'}`;
        
        const orderListEl = document.getElementById('modal-order-list');
        if (!orderListEl) {
            console.error("Critical: modal-order-list not found in DOM");
            return;
        }
        orderListEl.innerHTML = '';
        
        const orders = tradeData.orders || [];
        currentTradeOrders = JSON.parse(JSON.stringify(orders));
        
        currentTradeOrders.forEach((order, idx) => {
            const orderCard = document.createElement('div');
            const typeClass = (order.type || 'TRADE').toLowerCase();
            orderCard.className = `order-card type-${typeClass}`;
            const price = order.price_ea || 0;
            
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
                        <span class="label">Price (ea)</span>
                        <div class="price-adjust-container">
                            <button class="adjust-btn minus" onclick="adjustPrice(${idx}, -0.05)">-</button>
                            <span id="price-val-${idx}" class="value price-val">${formatOrderPrice(price)}</span>
                            <button class="adjust-btn plus" onclick="adjustPrice(${idx}, 0.05)">+</button>
                        </div>
                    </div>
                </div>
            `;
            orderListEl.appendChild(orderCard);
        });

        const creditEl = document.getElementById('modal-total-credit');
        if (creditEl) creditEl.textContent = tradeData.total_credit || '$0.00';
        
        window.currentTradeMaxTime = tradeData.timeout || TRADE_TIMEOUT_SEC;
        tradeTimeLeft = window.currentTradeMaxTime;
        window.currentModalStratId = tradeData.strat_id;
        
        isTradeTimerPaused = tradeData.is_paused || false;
        const pauseBtn = document.getElementById('modal-pause-btn');
        if (pauseBtn) pauseBtn.textContent = isTradeTimerPaused ? 'Resume Timer' : 'Pause Timer';
        
        updateTradeTimerUI();
        
        const modal = document.getElementById('trade-modal');
        if (modal) modal.classList.add('show');

        if (tradeTimer) clearInterval(tradeTimer);
        tradeTimer = setInterval(updateTradeTimer, 1000);
    } catch (err) {
        console.error("Crash during showTradeModal:", err);
    }
}

function formatOrderPrice(price) {
    const absPrice = Math.abs(price).toFixed(2);
    const suffix = price >= 0 ? 'Cr' : 'Db';
    return `$${absPrice} ${suffix}`;
}

function adjustPrice(idx, delta) {
    if (!currentTradeOrders[idx]) return;
    const order = currentTradeOrders[idx];
    const isCredit = order.is_credit;
    const lockFloor = order.lock_floor === true;
    
    // Task #32: Conditional 0.00 limit price floor based on strategy classification
    if (lockFloor) {
        // Enforce structural intent (cannot flip to other side)
        if (!isCredit && delta > 0 && order.price_ea >= -0.001) {
            alert("Structural Constraint: We cannot be more passive because the price for a " + order.order_type.toUpperCase() + " must stay non-negative (max 0.00 debit).");
            return;
        }
        if (isCredit && delta < 0 && order.price_ea <= 0.001) {
            alert("Structural Constraint: We cannot be more aggressive because the price for a " + order.order_type.toUpperCase() + " must stay non-negative (min 0.00 credit).");
            return;
        }
    }

    // Apply adjustment
    let newPrice = Number((order.price_ea + delta).toFixed(2));
    
    if (lockFloor) {
        if (!isCredit) {
            order.price_ea = Math.min(0.00, newPrice);
        } else {
            order.price_ea = Math.max(0.00, newPrice);
        }
    } else {
        // Unknown structure: Allow crossing zero
        order.price_ea = newPrice;
    }
    
    const priceEl = document.getElementById(`price-val-${idx}`);
    if (priceEl) {
        priceEl.textContent = formatOrderPrice(order.price_ea);
        priceEl.classList.add('modified');
    }
    
    updateTotalCreditDisplay();
}

function updateTotalCreditDisplay() {
    let total = 0;
    currentTradeOrders.forEach(order => {
        if (order.type === 'TRADE') {
            total += (order.price_ea || 0) * (order.qty || 1);
        }
    });
    
    const totalEl = document.getElementById('modal-total-credit');
    if (totalEl) {
        // Show unit-based summary style matching backend ($1.10 Credit / $1.10 Debit)
        totalEl.textContent = `${formatUSD(Math.abs(total))} ${total >= 0 ? 'Credit' : 'Debit'}`;
        totalEl.classList.add('modified');
    }
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
    const maxTime = window.currentTradeMaxTime || TRADE_TIMEOUT_SEC;
    const pct = Math.max(0, (tradeTimeLeft / maxTime) * 100);
    
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
    console.log("Toggle Timer Clicked");
    isTradeTimerPaused = !isTradeTimerPaused;
    const btn = document.getElementById('modal-pause-btn');
    if (btn) btn.textContent = isTradeTimerPaused ? 'Resume Timer' : 'Pause Timer';
    
    // Notify server to pause/resume auto-confirm timer (Issue 20 Fix)
    if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({
            action: 'toggle_trade_pause',
            is_paused: isTradeTimerPaused
        }));
    }
    
    updateTradeTimerUI();
}

function closeTradeModal() {
    console.log("closeTradeModal() triggered");
    if (tradeTimer) {
        console.log("Clearing tradeTimer:", tradeTimer);
        clearInterval(tradeTimer);
        tradeTimer = null;
    }
    isTradeTimerPaused = false;
    window.currentModalStratId = null;
    const modal = document.getElementById('trade-modal');
    console.log("Found modal element:", modal);
    if (modal) {
        modal.classList.remove('show');
        console.log("Removed 'show' class from modal");
    }
    const pauseBtn = document.getElementById('modal-pause-btn');
    if (pauseBtn) pauseBtn.textContent = 'Pause Timer';
}

function dismissTrade() {
    console.log("Dismiss Trade Clicked");
    const stratId = window.currentModalStratId;
    if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ action: 'dismiss_trade', strat_id: stratId }));
    } else {
        // WS not open — queue dismiss to retry on reconnect so backend doesn't auto-execute
        pendingDismissStratId = stratId;
        console.warn(`WS not open — dismiss for ${stratId} queued for reconnect`);
    }
    closeTradeModal();
}

function disableTradingFromModal() {
    if (confirm("⚠️ Are you sure you want to STOP ALL TRADING?\n\nThis will disable the trading flag and dismiss the current order. No further trades will be processed until re-enabled.")) {
        // 1. Disable trading flag via API
        fetch('/api/trading/toggle', { method: 'POST' })
            .then(() => {
                // 2. Dismiss the current modal
                dismissTrade();
                console.log("Trading disabled from modal.");
            })
            .catch(err => {
                console.error("Failed to disable trading:", err);
                alert("Error disabling trading. Please check console.");
            });
    }
}

function confirmLiveTrade() {
    console.log("Confirm Trade Clicked");
    if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({
            action: 'confirm_trade',
            strat_id: window.currentModalStratId,
            overrides: currentTradeOrders.map(o => ({
                idx: o.idx,
                price_ea: o.price_ea
            }))
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
let lastLogMsg = null;
function updateLogs(logs) {
    const consoleEl = document.getElementById('log-console');
    if (!logs || logs.length === 0) return;
    
    let newLogs = [];

    if (lastLogCount === 0 || logs[logs.length - 1] !== lastLogMsg) {
        // Find where the new logs start by searching from the end for the last known message
        let splitIdx = -1;
        if (lastLogMsg) {
            for (let i = logs.length - 1; i >= 0; i--) {
                if (logs[i] === lastLogMsg) {
                    splitIdx = i;
                    break;
                }
            }
        }
        
        newLogs = logs.slice(splitIdx + 1);
        
        // If we found NO overlap and it's not the first time, or it's a server reset
        if (splitIdx === -1 && lastLogCount > 0) {
            consoleEl.innerHTML = ''; // Start fresh if we lost sync
            newLogs = logs;
        }
    } else {
        return; // No new logs
    }

    newLogs.forEach(msg => {
        const entry = document.createElement('div');
        entry.className = 'log-entry';
        
        let levelClass = '';
        if (msg.includes('ERROR')) {
            levelClass = 'red';
            playSound('error');
        }
        else if (msg.includes('WARNING')) {
            levelClass = 'orange';
            playSound('info');
        }
        else if (msg.includes('INFO')) levelClass = 'primary';
        
        entry.innerHTML = `<span class="${levelClass}">${msg}</span>`;
        consoleEl.prepend(entry); // Prepend to keep NEWEST at top
    });
    
    lastLogCount = logs.length;
    lastLogMsg = logs[logs.length - 1];
}

function clearLogs() {
    document.getElementById('log-console').innerHTML = '';
    // We leave lastLogCount/Msg as-is so only NEW messages appear after clear
}

// Action Buttons
document.getElementById('toggle-trading-btn').addEventListener('click', () => {
    const statusEl = document.getElementById('trading-status');
    const isEnabled = statusEl.textContent === 'ENABLED';
    const action = isEnabled ? 'DISABLE' : 'ENABLE';
    
    if (confirm(`⚠️ Are you sure you want to ${action} live trading?\n\n${isEnabled ? 'This will stop all live execution.' : 'This will allow the system to send live orders to Schwab.'}`)) {
        fetch('/api/trading/toggle', { method: 'POST' });
    }
});

document.getElementById('reconnect-broker-btn').addEventListener('click', () => {
    const statusEl = document.getElementById('broker-status');
    const isLive = statusEl.textContent === 'LIVE';
    const msg = isLive 
        ? '⚠️ Broker is currently LIVE. Reconnecting will reset the session and potentially lose real-time sync for a few seconds. Proceed?'
        : 'Reconnect to Schwab API?';
        
    if (confirm(msg)) {
        fetch('/api/trading/reconnect', { method: 'POST' });
    }
});

// Order Cancellation
function confirmCancelOrder(orderId, symbol) {
    showConfirmModal(
        "Cancel Working Order",
        `Are you sure you want to cancel order <strong>#${orderId}</strong> (${symbol})?<br><br>This action cannot be undone.`,
        () => {
            fetch(`/api/orders/${orderId}/cancel`, { method: 'POST' })
                .then(resp => {
                    if (!resp.ok) throw new Error("Cancel failed");
                    console.log(`Cancel requested for order ${orderId}`);
                    closeConfirmModal();
                })
                .catch(err => {
                    alert("Error cancelling order: " + err.message);
                });
        }
    );
}

function confirmCancelAll() {
    showConfirmModal(
        "⚠️ Cancel ALL Orders",
        "Are you sure you want to cancel <strong>ALL</strong> currently working orders?<br><br>This will attempt to cancel every open order at the broker.",
        () => {
            fetch('/api/orders/cancel_all', { method: 'POST' })
                .then(resp => {
                    if (!resp.ok) throw new Error("Cancel all failed");
                    return resp.json();
                })
                .then(data => {
                    console.log("Cancel all result:", data);
                    closeConfirmModal();
                })
                .catch(err => {
                    alert("Error cancelling all orders: " + err.message);
                });
        }
    );
}

// Confirmation Modal Helpers
function showConfirmModal(title, message, onConfirm) {
    const modal = document.getElementById('confirm-modal');
    const titleEl = document.getElementById('confirm-modal-title');
    const msgEl = document.getElementById('confirm-modal-message');
    const confirmBtn = document.getElementById('confirm-modal-btn');
    
    titleEl.innerHTML = title;
    msgEl.innerHTML = message;
    
    // Use a fresh listener to avoid accumulation
    const newBtn = confirmBtn.cloneNode(true);
    confirmBtn.parentNode.replaceChild(newBtn, confirmBtn);
    newBtn.addEventListener('click', onConfirm);
    
    modal.classList.add('show');
}

function closeConfirmModal() {
    const modal = document.getElementById('confirm-modal');
    if (modal) modal.classList.remove('show');
}

initCharts();
initMuteUI();
connect();
