
import os
import sys
import json
from datetime import datetime
from typing import List

# Setup path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from server.core.models import OptionLeg, Trade, TradePurpose
from server.core.monitor import LiveTradingMonitor

def print_chunks(title, chunks):
    print(f"\n--- {title} ---")
    print(f"Total Chunks: {len(chunks)}")
    for i, chunk in enumerate(chunks):
        delta = sum(l.delta * l.quantity for l in chunk)
        print(f"Chunk {i+1} ({len(chunk)} legs, Net Delta: {delta:.4f}):")
        for l in chunk:
            print(f"  {l.quantity:2d} {l.side:4s} {l.strike:7.1f} (d={l.delta})")

def test_stable_splitting():
    # Mocking necessary parts of LiveTradingMonitor
    # We only need _get_smart_chunks and its dependencies
    class MockMonitor(LiveTradingMonitor):
        def __init__(self):
            # Bypass full init
            self.logger = type('MockLogger', (), {'debug': print, 'info': print, 'warning': print, 'error': print})
            self.config = {}

    monitor = MockMonitor()

    # Case 1: 8 legs (2 full ICs) - One balanced, one slightly unbalanced
    # We expect them to be split into two 4-leg orders, ranked by delta
    legs_8 = [
        OptionLeg("SP1", 6900, "PUT", -1, delta=-0.25),
        OptionLeg("LP1", 6880, "PUT", 1, delta=-0.15),
        OptionLeg("SC1", 7000, "CALL", -1, delta=0.25),
        OptionLeg("LC1", 7020, "CALL", 1, delta=0.15),
        
        OptionLeg("SP2", 6800, "PUT", -1, delta=-0.05),
        OptionLeg("LP2", 6780, "PUT", 1, delta=-0.02),
        OptionLeg("SC2", 7100, "CALL", -1, delta=0.08),
        OptionLeg("LC2", 7120, "CALL", 1, delta=0.02),
    ]
    # Expected: 
    # IC 1 Delta: -0.25+0.15 + (0.25-0.15) = 0.10 - 0.10 = 0.0
    # IC 2 Delta: -0.05+0.02 + (0.08-0.02) = -0.03 + 0.06 = 0.03
    
    chunks_8 = monitor._get_smart_chunks(legs_8)
    print_chunks("8 LEGS TEST (2 ICs)", chunks_8)

    # Case 2: 6 legs (1 IC + 2 extra legs)
    legs_6 = [
        OptionLeg("SP", 6900, "PUT", -1, delta=-0.25),
        OptionLeg("LP", 6880, "PUT", 1, delta=-0.15),
        OptionLeg("SC", 7000, "CALL", -1, delta=0.25),
        OptionLeg("LC", 7020, "CALL", 1, delta=0.15),
        OptionLeg("Extra1", 6850, "PUT", -1, delta=-0.40),
        OptionLeg("Extra2", 6830, "PUT", 1, delta=-0.30),
    ]
    # The Extra legs (Vertical) should be Chunk 1 because they are Non-IC
    chunks_6 = monitor._get_smart_chunks(legs_6)
    print_chunks("6 LEGS TEST (1 IC + 1 Vertical)", chunks_6)

    # Case 3: Verify Determinism (Shuffle input, expect same chunks)
    import random
    legs_shuffled = list(legs_8)
    random.shuffle(legs_shuffled)
    chunks_shuffled = monitor._get_smart_chunks(legs_shuffled)
    
    # Compare signatures
    sig1 = [sorted([(l.side, l.strike, l.quantity) for l in chunk]) for chunk in chunks_8]
    sig2 = [sorted([(l.side, l.strike, l.quantity) for l in chunk]) for chunk in chunks_shuffled]
    
    print("\n--- DETERMINISM CHECK ---")
    if sig1 == sig2:
        print("PASS: Shuffled input produced identical chunks.")
    else:
        print("FAIL: Shuffled input produced different chunks.")
        print("Sig1:", sig1)
        print("Sig2:", sig2)

if __name__ == "__main__":
    test_stable_splitting()
