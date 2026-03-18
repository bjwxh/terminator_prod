import asyncio
import logging
import os
import sys
from datetime import datetime
from fastapi import FastAPI, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn

# Root folder for imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from core.monitor import LiveTradingMonitor
from core.config import CONFIG
from api.routes import router as api_router, get_monitor
from api.ws import router as ws_router, broadcast_state

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("server.log")
    ]
)
logger = logging.getLogger("Terminator_Server")
logging.getLogger("httpx").setLevel(logging.WARNING)

app = FastAPI(title="Terminator Production Server")

# Disable CORS for local development/VPN access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Shared Monitor instance
monitor_instance = LiveTradingMonitor(config=CONFIG)

# Override monitor dependency for routes/WS
app.dependency_overrides[get_monitor] = lambda: monitor_instance

# Include APIRouters
app.include_router(api_router, prefix="/api")
app.include_router(ws_router)

# Mount static files for Web UI (serves index.html at root)
app.mount("/", StaticFiles(directory="static", html=True), name="static")

@app.on_event("startup")
async def startup_event():
    """Start the monitor and broadcast background tasks on server startup"""
    logger.info("Initializing Terminator Server...")
    
    # 1. Start the monitor loop in the background
    asyncio.create_task(monitor_instance.run_live_monitor())
    
    # 2. Start WebSocket state broadcaster (500ms cadence)
    asyncio.create_task(broadcast_state(monitor_instance))
    
    logger.info("Monitor and Broadcast tasks started.")

@app.get("/")
async def root():
    return {"msg": "Terminator Production Server is Running. Accessible via /api/status or /ws"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
