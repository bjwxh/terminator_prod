
import itertools
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple
from collections import defaultdict

@dataclass
class OptionLeg:
    symbol: str
    strike: float
    side: str
    quantity: int
    delta: float = 0.0
    instruction: Optional[str] = None

class MockMonitor:
    def _unroll_legs(self, legs: List[OptionLeg]) -> List[OptionLeg]:
        unrolled = []
        for leg in legs:
            qty = int(abs(leg.quantity))
            direction = 1 if leg.quantity > 0 else -1
            for _ in range(qty):
                unit_leg = OptionLeg(
                    symbol=leg.symbol, strike=leg.strike, side=leg.side,
                    quantity=direction, delta=leg.delta, instruction=leg.instruction
                )
                unrolled.append(unit_leg)
        return unrolled

    def _roll_legs(self, legs: List[OptionLeg]) -> List[OptionLeg]:
        agg = {}
        for l in legs:
            k = (l.symbol, l.instruction)
            if k not in agg:
                agg[k] = OptionLeg(
                    symbol=l.symbol, strike=l.strike, side=l.side,
                    quantity=l.quantity, delta=l.delta, instruction=l.instruction
                )
            else:
                agg[k].quantity += l.quantity
        return list(agg.values())

    def _get_smart_chunks(self, legs: List[OptionLeg]) -> List[List[OptionLeg]]:
        unrolled = self._unroll_legs(legs)
        if not unrolled: return []
        
        remaining = list(unrolled)
        found_combos = []

        def extract_chunk(num_legs, constraint_func):
            nonlocal remaining
            for indices in itertools.combinations(range(len(remaining)), num_legs):
                combo = [remaining[i] for i in indices]
                strike_keys = {(l.strike, l.side) for l in combo}
                if len(strike_keys) != num_legs: continue
                if constraint_func(combo):
                    for i in sorted(indices, reverse=True):
                        remaining.pop(i)
                    return combo
            return None

        while len(remaining) >= 4:
            ic = extract_chunk(4, lambda c: 
                sum(1 for l in c if l.side == 'CALL' and l.quantity > 0) == 1 and
                sum(1 for l in c if l.side == 'CALL' and l.quantity < 0) == 1 and
                sum(1 for l in c if l.side == 'PUT' and l.quantity > 0) == 1 and
                sum(1 for l in c if l.side == 'PUT' and l.quantity < 0) == 1
            )
            if not ic: break
            found_combos.append(ic)

        while len(remaining) >= 4:
            roll = extract_chunk(4, lambda c:
                len({l.side for l in c}) == 1 and
                sum(1 for l in c if l.quantity > 0) == 2 and
                sum(1 for l in c if l.quantity < 0) == 2
            )
            if not roll: break
            found_combos.append(roll)

        while len(remaining) >= 2:
            vs = extract_chunk(2, lambda c:
                len({l.side for l in c}) == 1 and
                sum(1 for l in c if l.quantity > 0) == 1 and
                sum(1 for l in c if l.quantity < 0) == 1
            )
            if not vs: break
            found_combos.append(vs)

        while len(remaining) >= 2:
            generic_vs = extract_chunk(2, lambda c:
                sum(1 for l in c if l.quantity > 0) == 1 and
                sum(1 for l in c if l.quantity < 0) == 1
            )
            if not generic_vs: break
            found_combos.append(generic_vs)

        leftover_rolled = self._roll_legs(remaining)
        
        grouped = defaultdict(list)
        for combo in found_combos:
            sig = tuple(sorted([(l.symbol, l.instruction) for l in combo]))
            grouped[sig].append(combo)
            
        final_chunks = []
        for sig, combos in grouped.items():
            all_unit_legs = []
            for combo in combos:
                all_unit_legs.extend(combo)
            final_chunks.append(self._roll_legs(all_unit_legs))

        if leftover_rolled:
            for i in range(0, len(leftover_rolled), 4):
                final_chunks.append(leftover_rolled[i:i+4])

        return final_chunks

def run_case_study_test():
    monitor = MockMonitor()
    
    # CASE STUDY SCENARIO:
    # +1 C6500 (BTC)
    # +1 C6500 (BTO)
    # -1 C6520 (STO)
    # -1 C6550 (STC)
    legs = [
        OptionLeg("SPXW 6500", 6500.0, "CALL", 1, instruction="BUY_TO_CLOSE"),
        OptionLeg("SPXW 6500", 6500.0, "CALL", 1, instruction="BUY_TO_OPEN"),
        OptionLeg("SPXW 6520", 6520.0, "CALL", -1, instruction="SELL_TO_OPEN"),
        OptionLeg("SPXW 6550", 6550.0, "CALL", -1, instruction="SELL_TO_CLOSE"),
    ]

    chunks = monitor._get_smart_chunks(legs)
    
    print(f"Total Chunks: {len(chunks)}")
    for i, chunk in enumerate(chunks):
        print(f"Chunk {i+1}:")
        for l in chunk:
            print(f"  {l.quantity:2} {l.side:4} {l.strike:7} ({l.instruction})")

if __name__ == "__main__":
    run_case_study_test()
