import os
import shutil
import winshell  

# Settings
DRIVE = "C:\\"  
THRESHOLD_PERCENT = 10  

def get_free_space_percentage(drive):
    """Returns the free space percentage and free gigabytes of the specified drive."""
    total, used, free = shutil.disk_usage(drive)
    free_percent = (free / total) * 100
    free_gb = free / (1024 ** 3)
    return free_percent, free_gb

def clear_temp_folders():
    """Deletes the contents of Windows temporary (Temp) folders where possible."""
    temp_paths = [
        os.path.expandvars(r'%TEMP%'),
        os.path.expandvars(r'%SystemRoot%\Temp')
    ]
    
    deleted_count = 0
    failed_count = 0

    print("--- Deleting temporary files ---")
    for path in temp_paths:
        if os.path.exists(path):
            for root, dirs, files in os.walk(path):
                for file in files:
                    file_path = os.path.join(root, file)
                    try:
                        os.remove(file_path)
                        deleted_count += 1
                    except Exception:
                        # Skip files that are currently in use by a program
                        failed_count += 1
                        pass
                        
    print(f"Successfully deleted: {deleted_count} files. (Skipped in-use: {failed_count})")

def empty_recycle_bin():
    """Empties the Recycle Bin."""
    print("--- Emptying Recycle Bin ---")
    try:
        # Empty the recycle bin without confirmation, progress bar, or sound
        winshell.recycle_bin().empty(confirm=False, show_progress=False, sound=False)
        print("Recycle Bin successfully emptied.")
    except Exception as e:
        print(f"Recycle Bin is already empty or an error occurred: {e}")

def main():
    free_percent, free_gb = get_free_space_percentage(DRIVE)
    print(f"Drive: {DRIVE} | Free space: {free_percent:.2f}% ({free_gb:.2f} GB)")

    if free_percent < THRESHOLD_PERCENT:
        print(f"\nWARNING: Free space is below {THRESHOLD_PERCENT}%! Starting cleanup...")
        
        # Cleanup process
        clear_temp_folders()
        empty_recycle_bin()
        
        # Re-check the situation
        new_percent, new_gb = get_free_space_percentage(DRIVE)
        print(f"\nCleanup complete! New free space: {new_percent:.2f}% ({new_gb:.2f} GB)")
    else:
        print("Sufficient free space available. No cleanup needed.")

if __name__ == "__main__":
    main()