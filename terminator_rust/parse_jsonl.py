import re

with open("/tmp/strategy_patches.jsonl") as f:
    text = f.read()

# We can find all instances of replace_file_content and extract the args.
# Since the JSON is truncated ONLY at the end of the line, most lines should be completely valid!
lines = text.split("\n")
for i, line in enumerate(lines):
    if not line.strip(): continue
    try:
        import json
        data = json.loads(line)
        print(f"Line {i} parsed successfully")
    except json.JSONDecodeError as e:
        print(f"Line {i} failed: {e}")
