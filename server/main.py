import asyncio
import logging
import os
import sys
from datetime import datetime
from fastapi import FastAPI, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn
from contextlib import asynccontextmanager

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
        logging.FileHandler("server.log", mode='a')
    ]
)
logger = logging.getLogger("Terminator_Server")
logging.getLogger("httpx").setLevel(logging.WARNING)

# Shared Monitor instance
monitor_instance = LiveTradingMonitor(config=CONFIG)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan handler for startup and shutdown (Robustness-2 fix)"""
    # Startup
    logger.info("Initializing Terminator Server (Lifespan)...")
    
    # 1. Start the monitor loop in the background
    monitor_task = asyncio.create_task(monitor_instance.run_live_monitor())
    
    # 2. Start WebSocket state broadcaster (500ms cadence)
    broadcast_task = asyncio.create_task(broadcast_state(monitor_instance))
    
    logger.info("Monitor and Broadcast tasks started.")
    
    yield
    
    # Shutdown
    logger.info("Shutting down Terminator Server...")
    # Stop the loops
    monitor_instance.is_running = False 
    
    # Cancel tasks
    monitor_task.cancel()
    broadcast_task.cancel()
    
    try:
        await asyncio.gather(monitor_task, broadcast_task, return_exceptions=True)
        # Gracefully stop news fetcher client
        if hasattr(monitor_instance, 'news_fetcher'):
            await monitor_instance.news_fetcher.stop()
    except Exception as e:
        logger.error(f"Error during task cancellation: {e}")

        
    # Flush session state
    if hasattr(monitor_instance, 'session_manager'):
        monitor_instance.session_manager.save_session(monitor_instance)
        logger.info("Session state flushed to disk.")
    
    logger.info("Shutdown complete.")

app = FastAPI(title="Terminator Production Server", lifespan=lifespan)

# Disable CORS for local development/VPN access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Override monitor dependency for routes/WS
app.dependency_overrides[get_monitor] = lambda: monitor_instance

# Include APIRouters
app.include_router(api_router, prefix="/api")
app.include_router(ws_router)

# Mount static files for Web UI (serves index.html at root)
static_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/", StaticFiles(directory=static_path, html=True), name="static")

@app.get("/")
async def root():
    return {"msg": "Terminator Production Server is Running. Accessible via /api/status or /ws"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
