# live/terminator/utils.py

import os
import json
import datetime as dt
from typing import Optional, Dict

_POWER_LAW_DATA_CACHE = None

def load_power_law_data():
    global _POWER_LAW_DATA_CACHE
    if _POWER_LAW_DATA_CACHE is None:
        try:
            # Try core/data location (production structure)
            paths = [
                os.path.join(os.path.dirname(__file__), "data/spx_0dte_delta_decay_power_law.json"),
                os.path.join(os.path.dirname(__file__), "../config/spx_0dte_delta_decay_power_law.json"),
            ]
            for path in paths:
                if os.path.exists(path):
                    with open(path, 'r') as f:
                        _POWER_LAW_DATA_CACHE = json.load(f)
                    break
            if _POWER_LAW_DATA_CACHE is None:
                _POWER_LAW_DATA_CACHE = {}
        except Exception:
            _POWER_LAW_DATA_CACHE = {}
    return _POWER_LAW_DATA_CACHE

def calculate_delta_decay(timestamp: dt.datetime, side: str, init_leg_delta: float, 
                         start_time: dt.time, end_time: dt.time) -> float:
    base_date = timestamp.date()
    start_dt = dt.datetime.combine(base_date, start_time)
    end_dt = dt.datetime.combine(base_date, end_time)
    total_seconds = (end_dt - start_dt).total_seconds()
    elapsed_seconds = (timestamp - start_dt).total_seconds()
    time_fraction = max(0.0, min(1.0, elapsed_seconds / total_seconds))
    
    if init_leg_delta <= 0.2:
        current_delta = init_leg_delta * (1 - time_fraction)
    else:
        try:
            power_law_data = load_power_law_data()
            available_deltas = [float(k[1:]) / 100.0 for k in power_law_data.keys()]
            if not available_deltas:
                return init_leg_delta * (1 - time_fraction)
            closest_delta = min(available_deltas, key=lambda x: abs(x - init_leg_delta))
            params = power_law_data[f"d{int(closest_delta * 100)}"][side.lower()]
            current_delta = (params["delta_final"] + (params["delta_start"] - params["delta_final"]) * (1 - time_fraction) ** params["k"])
        except Exception:
            current_delta = init_leg_delta * (1 - time_fraction)
    return abs(current_delta)
