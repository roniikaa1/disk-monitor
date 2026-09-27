# Import necessary modules
import ctypes     # Allows us to call core Windows C-level functions (like checking admin or emptying recycle bin)
import os         # Used for interacting with the operating system, like reading environment variables
import shutil     # High-level file operations, used here for checking disk space and deleting whole folders
from pathlib import Path # Modern, cleaner way to handle file paths in Python
from typing import Tuple, List # Used for type hinting, which helps IDEs understand what functions return

# --- Configuration Section ---
DRIVE: str = "C:\\"              # The main hard drive we want to monitor and clean
THRESHOLD_PERCENT: float = 10.0  # If free space drops below this percentage, trigger automatic cleanup


def is_admin() -> bool:
    """Check if the script is running with administrator privileges."""
    try:
        # Calls the Windows Shell API to check if the current user has admin rights.
        # Returns a non-zero value if true, so we check if it is not equal to (!=) 0.
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        # If the check fails for any reason, safely assume we are not admin.
        return False


def get_free_space(drive: str) -> Tuple[float, float]:
    """Returns the free space percentage and free gigabytes of the specified drive."""
    # shutil.disk_usage returns a tuple of (total_bytes, used_bytes, free_bytes)
    total, used, free = shutil.disk_usage(drive)
    
    # Calculate percentage: (Free bytes divided by Total bytes) times 100
    free_percent = (free / total) * 100
    
    # Convert bytes to Gigabytes. 
    # 1 Kilobyte = 1024 bytes, 1 Megabyte = 1024^2 bytes, 1 Gigabyte = 1024^3 bytes.
    free_gb = free / (1024**3)
    
    return free_percent, free_gb


def get_cleanup_paths() -> List[Path]:
    """Returns a list of high-yield junk folder paths to clean."""
    # We use environment variables (like %TEMP% or %LOCALAPPDATA%) because 
    # these folders might be in different places on different computers.
    paths = [
        # Standard Temporary Files
        r"%TEMP%",                                     # Current user's temp folder
        r"%SystemRoot%\Temp",                          # Windows system temp folder (usually C:\Windows\Temp)
        
        # System Caches (Requires Admin)
        r"%SystemRoot%\SoftwareDistribution\Download", # Windows Update downloaded files cache
        r"%SystemRoot%\Prefetch",                      # App launch prefetch data
        
        # Application Crash Dumps
        r"%LOCALAPPDATA%\CrashDumps",                  # Where apps save memory dumps when they crash
        r"%ProgramData%\Microsoft\Windows\WER\ReportArchive", # Windows Error Reporting archive
        
        # Browser Caches
        r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\Cache\Cache_Data", # Chrome internet cache
        r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\Cache\Cache_Data", # Edge internet cache
    ]
    
    # os.path.expandvars translates variables like %TEMP% into actual paths (e.g., C:\Users\name\AppData\Local\Temp)
    # We wrap them in Path() objects to make them easier to work with later.
    return [Path(os.path.expandvars(p)) for p in paths]


def clear_junk_folders() -> None:
    """Deletes files and subdirectories from all targeted junk folders."""
    cleanup_paths = get_cleanup_paths()

    # Trackers to see how much we accomplished
    deleted_files = 0
    deleted_dirs = 0
    failed_count = 0

    print("--- Deleting system junk and cache files ---")
    
    # Loop through every folder in our list
    for cache_dir in cleanup_paths:
        # If the folder doesn't exist on this PC, skip it
        if not cache_dir.exists():
            continue

        print(f"Cleaning: {cache_dir}")
        
        # OUTER TRY-EXCEPT: Checks if we have permission to LOOK inside the folder
        try:
            # iterdir() gives us a list of everything inside the folder
            for item in cache_dir.iterdir():
                
                # INNER TRY-EXCEPT: Checks if we have permission to DELETE this specific file
                try:
                    # If the item is a file or a shortcut (symlink), delete it using unlink()
                    if item.is_file() or item.is_symlink():
                        item.unlink(missing_ok=True)
                        deleted_files += 1
                        
                    # If the item is a folder, delete it and everything inside it using rmtree()
                    elif item.is_dir():
                        shutil.rmtree(item)
                        deleted_dirs += 1
                        
                except (PermissionError, OSError):
                    # It's totally normal for some files to fail. 
                    # If Windows or an app is currently using a file, it gets "locked" and cannot be deleted.
                    failed_count += 1
                    
        except PermissionError:
            # This triggers if we don't have Admin rights to read folders like C:\Windows\Prefetch
            print("  -> Skipped: Access Denied (Run terminal as Administrator to clean this folder)")

    # Print final summary
    print(
        f"\nSuccessfully deleted: {deleted_files} files and {deleted_dirs} folders. "
        f"(Skipped in-use/locked: {failed_count})"
    )


def empty_recycle_bin() -> None:
    """Empties the Recycle Bin using the native Windows API."""
    print("--- Emptying Recycle Bin ---")
    
    # We are calling a C-function built into Windows called SHEmptyRecycleBinW
    # It takes a flag (a number) that tells Windows how to behave:
    # 1 = No confirmation popups asking "Are you sure?"
    # 2 = No progress bar UI
    # 4 = No sound effects
    # By adding them together (1 + 2 + 4 = 7), we tell Windows to do it completely silently.
    try:
        result = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 7)
        # If the function returns 0, it means success.
        if result == 0:
            print("Recycle Bin successfully emptied.")
        else:
            print("Recycle Bin is already empty.")
    except Exception as e:
        print(f"Error emptying Recycle Bin: {e}")


def main() -> None:
    # 1. Warn the user if they forgot to run as Admin
    if not is_admin():
        print(
            "NOTE: Script is not running as Administrator. Cleanup of Windows Update "
            "caches and Prefetch folders will be skipped.\n"
        )

    # 2. Check current drive space
    free_percent, free_gb = get_free_space(DRIVE)
    print(f"Drive: {DRIVE} | Free space: {free_percent:.2f}% ({free_gb:.2f} GB)")

    # This variable determines if we actually run the cleaning functions
    run_cleanup = False

    # 3. Check if we are below the threshold (automatic trigger)
    if free_percent < THRESHOLD_PERCENT:
        print(f"\nWARNING: Free space is below {THRESHOLD_PERCENT}%! Starting cleanup...\n")
        run_cleanup = True
    else:
        # 4. If space is fine, ask the user if they want to clean anyway (manual trigger)
        print(f"\nSufficient free space available (Threshold: {THRESHOLD_PERCENT}%).")
        
        # input() pauses the script and waits for the user to type something.
        # .strip() removes accidental spaces, .lower() makes 'Y' into 'y'
        user_input = input("Would you like to force the cleanup anyway? (y/n): ").strip().lower()
        
        if user_input == 'y':
            print("\nStarting manual cleanup...\n")
            run_cleanup = True
        else:
            print("Cleanup skipped. Exiting script.")

    # 5. Execute the cleanup if it was triggered (either automatically or manually)
    if run_cleanup:
        clear_junk_folders()
        print("") # Prints a blank line for readability
        empty_recycle_bin()

        # 6. Recalculate space to show the user how much was saved
        new_percent, new_gb = get_free_space(DRIVE)
        print(f"\nCleanup complete! New free space: {new_percent:.2f}% ({new_gb:.2f} GB)")
        
        # Simple math: New free space minus old free space = amount recovered
        print(f"Total space recovered: {(new_gb - free_gb):.2f} GB")


# This is the standard Python entry point. It ensures main() only runs 
# if you execute this file directly, rather than importing it into another script.
if __name__ == "__main__":
    main()