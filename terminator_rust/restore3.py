import json

with open("/tmp/strategy_patches.jsonl") as f:
    lines = f.readlines()

def apply_patch(filename, chunk):
    with open(filename, "r") as f:
        file_lines = f.readlines()
    
    start = int(chunk["StartLine"]) - 1
    end = int(chunk["EndLine"])
    content = chunk["ReplacementContent"]
    if not content.endswith("\n"):
        content += "\n"
        
    file_lines = file_lines[:start] + [content] + file_lines[end:]
    with open(filename, "w") as f:
        f.writelines(file_lines)

for line in lines:
    if not line.strip(): continue
    data = json.loads(line)
    if "tool_calls" in data:
        for tc in data["tool_calls"]:
            if tc["name"] in ("replace_file_content", "multi_replace_file_content"):
                args = tc.get("args", {})
                if isinstance(args, str):
                    args = json.loads(args)
                if "strategy.rs" in args.get("TargetFile", ""):
                    print(f"Applying {tc['name']}...")
                    if "ReplacementChunks" in args:
                        chunks = args["ReplacementChunks"]
                        if isinstance(chunks, str):
                            chunks = json.loads(chunks, strict=False)
                        chunks = sorted(chunks, key=lambda c: int(c["StartLine"]), reverse=True)
                        for chunk in chunks:
                            apply_patch("src/strategy.rs", chunk)
                    elif "ReplacementContent" in args:
                        apply_patch("src/strategy.rs", args)

print("Restoration complete")
