let socket;
let reconnectInterval = 1000;
const maxReconnectInterval = 30000;

// Elements
const sim_pnl_val = document.getElementById('sim-pnl');
const live_pnl_val = document.getElementById('live-pnl');
const system_btn = document.getElementById('system-action');
const trading_btn = document.getElementById('toggle-trading');
const conn_pill = document.getElementById('connection-status');
const broker_pill = document.getElementById('broker-status');
const last_update_ts = document.getElementById('last-update');
const server_status_text = document.getElementById('server-status');

const live_tbody = document.getElementById('live-positions-body');
const sim_tbody = document.getElementById('sim-positions-body');
const strategy_accordion = document.getElementById('strategies-accordion');
const history_list = document.getElementById('trade-history-list');

// Init
function init() {
    connectWebSocket();
    setupEventHandlers();
    refreshInitialData();
}

function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws`;
    
    socket = new WebSocket(wsUrl);

    socket.onopen = () => {
        console.log('WS Connected');
        conn_pill.className = 'status-pill online';
        conn_pill.querySelector('.label').innerText = 'Server: Connected';
        reconnectInterval = 1000;
    };

    socket.onmessage = (event) => {
        const data = JSON.parse(event.data);
        updateUI(data);
    };

    socket.onclose = () => {
        console.log('WS Closed, reconnecting...');
        conn_pill.className = 'status-pill offline';
        conn_pill.querySelector('.label').innerText = 'Server: Reconnecting...';
        
        setTimeout(connectWebSocket, reconnectInterval);
        reconnectInterval = Math.min(reconnectInterval * 2, maxReconnectInterval);
    };

    socket.onerror = (err) => {
        console.error('WS Error:', err);
    };
}

function updateUI(data) {
    // Top Bar
    updatePnL(sim_pnl_val, data.sim_pnl);
    updatePnL(live_pnl_val, data.live_pnl);
    
    // Status
    server_status_text.innerText = `Server Status: ${data.status}`;
    broker_pill.className = `status-pill ${data.broker_connected ? 'connected' : 'disconnected'}`;
    broker_pill.querySelector('.label').innerText = `Broker: ${data.broker_connected ? 'Online' : 'Offline'}`;
    
    // Buttons
    system_btn.innerText = data.status === 'Stopped' ? 'Start Monitor' : 'Stop Monitor';
    system_btn.className = `btn ${data.status === 'Stopped' ? 'btn-primary' : 'btn-secondary'}`;
    
    trading_btn.innerText = data.trading_enabled ? 'Disable Trading' : 'Enable Trading';
    trading_btn.className = `btn ${data.trading_enabled ? 'btn-danger' : 'btn-primary'}`;
    
    // Footer
    const now = new Date();
    last_update_ts.innerText = `Last update: ${now.toLocaleTimeString()}`;
    
    // We only refresh positions/strategies on data change if needed, 
    // or just every few seconds via REST. For now, let's trigger a REST pull.
    // (In full implementation, positions could be pushed via WS too for 500ms smoothness)
}

function updatePnL(el, val) {
    const color = val >= 0 ? 'pnl-positive' : 'pnl-negative';
    el.innerText = `$${val.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
    el.className = `pnl-value ${color}`;
}

async function setupEventHandlers() {
    // Tabs
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const target = btn.dataset.tab;
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            btn.classList.add('active');
            document.getElementById(target).classList.add('active');
        });
    });

    // Start/Stop Monitor
    system_btn.addEventListener('click', async () => {
        const action = system_btn.innerText === 'Start Monitor' ? 'start' : 'stop';
        try {
            await fetch(`/api/monitor/${action}`, { method: 'POST' });
        } catch (err) {
            console.error('Action failed:', err);
        }
    });

    // Toggle Trading
    trading_btn.addEventListener('click', async () => {
        const action = trading_btn.innerText === 'Enable Trading' ? 'enable' : 'disable';
        try {
            await fetch(`/api/trading/${action}`, { method: 'POST' });
        } catch (err) {
            console.error('Action failed:', err);
        }
    });
}

async function refreshInitialData() {
    // Periodically pull positions and trades (every 10s)
    async function pull() {
        try {
            const [port, trades, strats] = await Promise.all([
                fetch('/api/portfolio').then(r => r.json()),
                fetch('/api/trades').then(r => r.json()),
                fetch('/api/strategies').then(r => r.json())
            ]);
            
            renderPositions(live_tbody, port.live);
            renderPositions(sim_tbody, port.sim);
            renderTrades(trades);
            renderStrategies(strats);
            
        } catch (err) {
            console.error('Data pull failed:', err);
        }
        setTimeout(pull, 10000);
    }
    pull();
}

function renderPositions(tbody, positions) {
    if (!positions || positions.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#666;padding:20px;">No active positions</td></tr>';
        return;
    }
    
    tbody.innerHTML = positions.map(p => `
        <tr>
            <td>${p.strike}</td>
            <td style="color:${p.side === 'CALL' ? '#58a6ff' : '#3fb950'}">${p.side}</td>
            <td style="color:${p.quantity >= 0 ? '#3fb950' : '#f85149'}">${p.quantity}</td>
            <td>${p.delta.toFixed(2)}</td>
            <td>$${p.price.toFixed(2)}</td>
        </tr>
    `).join('');
}

function renderTrades(trades) {
    if (!trades) return;
    history_list.innerHTML = trades.slice(0, 20).map(t => `
        <div class="history-item ${t.purpose}">
            <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
                <span style="font-weight:600;color:var(--primary);">${t.purpose.toUpperCase()}</span>
                <span style="color:var(--text-secondary);">${t.ts.split('T')[1].split('.')[0]}</span>
            </div>
            <div style="font-family:var(--font-mono);font-size:11px;">
                ${t.legs.map(l => `${l.qty}x ${l.side} ${l.strike}`).join(' | ')}
            </div>
            <div style="margin-top:4px;font-weight:700;color:${t.credit >= 0 ? 'var(--success)' : 'var(--danger)'}">
                ${t.credit >= 0 ? '+' : ''}${(t.credit/100).toFixed(2)}
            </div>
        </div>
    `).join('');
}

function renderStrategies(strats) {
    if (!strats) return;
    // Basic accordion implementation
    strategy_accordion.innerHTML = strats.sort((a,b) => a.sid.localeCompare(b.sid)).map(s => `
        <div class="accordion-item" style="border-bottom:1px solid var(--border-color);padding:12px 16px;">
            <div style="display:flex;justify-content:space-between;cursor:pointer;">
                <span style="font-weight:600;">${s.sid}</span>
                <span style="color:${s.has_traded ? 'var(--success)' : 'var(--text-secondary)'}">
                    ${s.has_traded ? 'ACTIVE' : 'READY'}
                </span>
            </div>
            ${s.has_traded ? `
                <div style="padding-top:8px;font-size:12px;color:var(--text-secondary);">
                    PnL: <span style="font-family:var(--font-mono);color:var(--text-primary);">$${s.cash.toFixed(2)}</span>
                </div>
            ` : ''}
        </div>
    `).join('');
}

init();
