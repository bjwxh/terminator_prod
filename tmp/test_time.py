import json

with open('/Users/fw/Git/terminator_prod/terminator_rust/logs/terminator.log.2026-06-22', 'r') as f:
    for line in f:
        if 'enteredTime' in line:
            print(line)
