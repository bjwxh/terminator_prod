
import sys
import os
from datetime import datetime
from typing import List

# Setup path so we can import modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../server/core')))
from models import OptionLeg, Trade, TradePurpose, Portfolio
from monitor import TerminatorMonitor

def test_case_study():
    # Case Study Scenario:
    # Live: (-1 C6500, -1 C6520, +2 C6550)
    # Sim:  (+1 C6500, -2 C6520, +1 C6550)
    #
    # Needed adjustments:
    # 6500C: -1 -> +1 (Split into +1 BTC and +1 BTO)
    # 6520C: -1 -> -2 (Needs -1 STO)
    # 6550C: +2 -> +1 (Needs -1 STC)

    monitor = TerminatorMonitor(config={}, db_path="") # Minimal mock
    
    legs = [
        OptionLeg("SPXW 6500", 6500.0, "CALL", 1),  # BTC portion
        OptionLeg("SPXW 6500", 6500.0, "CALL", 1),  # BTO portion
        OptionLeg("SPXW 6520", 6520.0, "CALL", -1), # STO
        OptionLeg("SPXW 6550", 6550.0, "CALL", -1), # STC
    ]
    
    # Manually set instructions for clarity as _check_reconciliation would
    legs[0].instruction = "BUY_TO_CLOSE"
    legs[1].instruction = "BUY_TO_OPEN"
    legs[2].instruction = "SELL_TO_OPEN"
    legs[3].instruction = "SELL_TO_CLOSE"

    chunks = monitor._get_smart_chunks(legs)
    
    print(f"Total Chunks: {len(chunks)}")
    for i, chunk in enumerate(chunks):
        print(f"Chunk {i+1} ({len(chunk)} legs):")
        for l in chunk:
            inst = getattr(l, 'instruction', 'N/A')
            print(f"   {l.quantity:2} {l.side:4} {l.strike:7} ({inst})")

    # Expectations:
    # Chunks are created based on combinations.
    # Vertical priority: Should find combinations of (1L, 1S) and enforce unique strike rule.
    # 6500C BTC and 6500C BTO cannot be in the same chunk.
    # Possible combinations:
    # (6500 BTC, 6520 STO) and (6500 BTO, 6550 STC)
    # OR 
    # (6500 BTC, 6550 STC) and (6500 BTO, 6520 STO)
    
    # Let's count them
    assert len(chunks) == 2, f"Expected 2 chunks, got {len(chunks)}"
    for chunk in chunks:
        assert len(chunk) == 2, f"Expected each chunk to have 2 legs, got {len(chunk)}"

if __name__ == "__main__":
    try:
        test_case_study()
        print("\nCASE STUDY TEST PASSED")
    except Exception as e:
        print(f"\nCASE STUDY TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
