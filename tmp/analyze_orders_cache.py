import json
from datetime import datetime, timezone

def analyze():
    with open("tmp/today_orders.json", "r") as f:
        orders = json.load(f)
    
    print(f"Total orders: {len(orders)}")
    for o in orders:
        oid = o.get('orderId')
        entered_str = o.get('enteredTime', '')
        status = o.get('status')
        filled_qty = o.get('filledQuantity', 0)
        qty = o.get('quantity', 0)
        
        # Parse enteredTime
        # Example: '2026-05-21T17:17:53+0000'
        # Let's check if it's within the timeframe 17:15:00 to 17:25:00 UTC
        time_part = entered_str.split('T')[1].split('+')[0] if 'T' in entered_str else ''
        if time_part and "17:15:00" <= time_part <= "17:25:00":
            print(f"\nOrder ID: {oid} | Status: {status} | Entered: {entered_str}")
            print(f"Quantity: {qty} | Filled Qty: {filled_qty} | Price: {o.get('price')}")
            legs = o.get('orderLegCollection', [])
            for l in legs:
                print(f"  Leg: {l.get('instruction')} | Qty: {l.get('quantity')} | {l.get('instrument', {}).get('symbol')}")
            
            activities = o.get('orderActivityCollection', [])
            for act in activities:
                print(f"  Activity: {act.get('activityType')} | Qty: {act.get('quantity')} | Status: {act.get('executionType')}")
                for el in act.get('executionLegs', []):
                    print(f"    Exec Leg: ID {el.get('legId')} | Price {el.get('price')} | Qty {el.get('quantity')} | Time {el.get('time')}")

if __name__ == "__main__":
    analyze()
