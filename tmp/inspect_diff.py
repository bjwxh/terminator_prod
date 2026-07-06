with open("tmp/trades_py.txt") as f:
    py = f.read().splitlines()
with open("tmp/trades_rust.txt") as f:
    rust = f.read().splitlines()

print(f"Python trades: {len(py)}")
print(f"Rust trades:   {len(rust)}")

def normalize_action(action):
    action = action.strip()
    if action in ["IRON_CONDOR", "ENTRY"]:
        return "ENTRY"
    if action in ["REBALANCE_SHORT", "REBALANCE_LONG", "REBAL"]:
        return "REBAL"
    return action

py_norm = []
for line in py:
    parts = line.split(" | ")
    parts[2] = normalize_action(parts[2])
    py_norm.append(" | ".join(parts))

rust_norm = []
for line in rust:
    parts = line.split(" | ")
    parts[2] = normalize_action(parts[2])
    rust_norm.append(" | ".join(parts))

py_set = set(py_norm)
rust_set = set(rust_norm)

only_py = py_set - rust_set
only_rust = rust_set - py_set

print(f"Only in Python: {len(only_py)}")
print(f"Only in Rust:   {len(only_rust)}")

if only_py:
    print("\n--- ONLY IN PYTHON (NORMALIZED) ---")
    for x in sorted(only_py)[:10]:
        print(x)
        
if only_rust:
    print("\n--- ONLY IN RUST (NORMALIZED) ---")
    for x in sorted(only_rust)[:10]:
        print(x)
