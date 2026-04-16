import pandas as pd
import sys
import os

def smart_merge(local_path, remote_temp_path):
    if not os.path.exists(local_path):
        print(f"Error: Local file {local_path} not found.")
        sys.exit(1)
    if not os.path.exists(remote_temp_path):
        print(f"Error: Remote temp file {remote_temp_path} not found.")
        sys.exit(1)

    try:
        local_df = pd.read_csv(local_path)
        remote_df = pd.read_csv(remote_temp_path)

        # Ensure dates are comparable
        local_df['date'] = pd.to_datetime(local_df['date'])
        remote_df['date'] = pd.to_datetime(remote_df['date'])

        # Find the latest date in the local ledger
        if local_df.empty:
            last_local_date = pd.Timestamp('1970-01-01')
        else:
            last_local_date = local_df['date'].max()
        
        print(f"Last local record date: {last_local_date.date()}")

        # Find rows in the VM file that are NEWER than the local last date
        new_rows = remote_df[remote_df['date'] > last_local_date].copy()

        if not new_rows.empty:
            print(f"Found {len(new_rows)} new session(s) to append:")
            for d in new_rows['date']:
                print(f"  - {d.date()}")
            
            # Format dates back to string for CSV consistency
            new_rows['date'] = new_rows['date'].dt.strftime('%Y-%m-%d')
            local_df['date'] = local_df['date'].dt.strftime('%Y-%m-%d')
            
            # Append and save
            updated_df = pd.concat([local_df, new_rows], ignore_index=True)
            updated_df.to_csv(local_path, index=False)
            print("Local ledger updated successfully.")
        else:
            print("No new data found on the VM. Local ledger is already up-to-date.")
    except Exception as e:
        print(f"Error during smart merge: {e}")
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python smart_merge_history.py <local_path> <remote_temp_path>")
        sys.exit(1)
    smart_merge(sys.argv[1], sys.argv[2])
