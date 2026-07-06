import os
import glob

def replace_in_file(path, old, new):
    with open(path, 'r') as f:
        content = f.read()
    if old in content:
        content = content.replace(old, new)
        with open(path, 'w') as f:
            f.write(content)
        print(f"Updated {path}")

rust_files = glob.glob('terminator_rust/src/**/*.rs', recursive=True)

for path in rust_files:
    replace_in_file(path, 'https://api.schwabapi.com/v1/accounts', 'https://api.schwabapi.com/trader/v1/accounts')
    replace_in_file(path, 'https://api.schwabapi.com/v1/userPreference', 'https://api.schwabapi.com/trader/v1/userPreference')

