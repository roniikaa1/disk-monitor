from __future__ import annotations
import ctypes
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple


# ============================================================
# CONFIGURATION
# ============================================================

DRIVE = Path("C:\\")
THRESHOLD_PERCENT = 10.0

# Safety settings
DRY_RUN = False

# Cleanup options
CLEAN_RECYCLE_BIN = True
CLEAN_BROWSER_CACHES = True
CLEAN_WINDOWS_UPDATE_CACHE = True
CLEAN_PREFETCH = False

# Only crash dumps older than this many days are deleted.
CRASH_DUMP_MAX_AGE_DAYS = 30

# Log file
LOG_FILE = Path("disk_cleanup.log")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# DATA STRUCTURES
# ============================================================

@dataclass
class CleanupStats:
    files_deleted: int = 0
    directories_deleted: int = 0
    files_failed: int = 0
    directories_failed: int = 0
    bytes_recovered: int = 0

    @property
    def total_items_deleted(self) -> int:
        return self.files_deleted + self.directories_deleted

    @property
    def recovered_gb(self) -> float:
        return self.bytes_recovered / (1024 ** 3)


# ============================================================
# ADMIN CHECK
# ============================================================

def is_admin() -> bool:
    """Return True if the script is running with administrator privileges."""

    if os.name != "nt":
        return False

    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception as exc:
        logger.exception("Administrator check failed: %s", exc)
        return False


# ============================================================
# DISK INFORMATION
# ============================================================

def get_free_space(drive: Path) -> Tuple[float, float]:
    """
    Return:
        free percentage
        free GB
    """

    total, used, free = shutil.disk_usage(drive)

    if total == 0:
        return 0.0, 0.0

    free_percent = (free / total) * 100
    free_gb = free / (1024 ** 3)

    return free_percent, free_gb


def get_disk_usage(drive: Path) -> Tuple[float, float, float]:
    """Return total, used and free disk space in GB."""

    total, used, free = shutil.disk_usage(drive)

    return (
        total / (1024 ** 3),
        used / (1024 ** 3),
        free / (1024 ** 3),
    )


# ============================================================
# DISPLAY HELPERS
# ============================================================

def print_header(title: str) -> None:
    """Print a formatted section header."""

    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


def format_size(size_bytes: int) -> str:
    """Convert bytes to a human-readable size."""

    units = ["B", "KB", "MB", "GB", "TB"]

    size = float(size_bytes)

    for unit in units:
        if size < 1024:
            return f"{size:.2f} {unit}"

        size /= 1024

    return f"{size:.2f} PB"


# ============================================================
# CLEANUP PATHS
# ============================================================

def get_cleanup_paths() -> List[Path]:
    """
    Return Windows folders that are generally safe cleanup targets.

    Some locations are optional because they can have side effects.
    """

    paths = [
        # User temporary files
        r"%TEMP%",

        # Windows temporary files
        r"%SystemRoot%\Temp",

        # Crash dumps
        r"%LOCALAPPDATA%\CrashDumps",

        # Windows Error Reporting
        r"%ProgramData%\Microsoft\Windows\WER\ReportArchive",
        r"%ProgramData%\Microsoft\Windows\WER\ReportQueue",
    ]

    if CLEAN_WINDOWS_UPDATE_CACHE:
        paths.append(
            r"%SystemRoot%\SoftwareDistribution\Download"
        )

    if CLEAN_PREFETCH:
        paths.append(
            r"%SystemRoot%\Prefetch"
        )

    if CLEAN_BROWSER_CACHES:
        paths.extend(
            [
                # Chrome
                r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\Cache",

                # Edge
                r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\Cache",

                # Firefox
                r"%LOCALAPPDATA%\Mozilla\Firefox\Profiles",
            ]
        )

    expanded_paths = []

    for path_string in paths:
        expanded = os.path.expandvars(path_string)
        path = Path(expanded)

        if path not in expanded_paths:
            expanded_paths.append(path)

    return expanded_paths


# ============================================================
# DIRECTORY SIZE
# ============================================================

def calculate_directory_size(directory: Path) -> int:
    """Calculate the total size of a directory."""

    total_size = 0

    if not directory.exists():
        return 0

    try:
        for item in directory.rglob("*"):
            try:
                if item.is_file() and not item.is_symlink():
                    total_size += item.stat().st_size
            except (PermissionError, OSError):
                continue

    except (PermissionError, OSError):
        pass

    return total_size


# ============================================================
# SAFE FILE DELETION
# ============================================================

def delete_file(
    file_path: Path,
    stats: CleanupStats,
) -> None:
    """Safely delete a single file."""

    try:
        if not file_path.exists() and not file_path.is_symlink():
            return

        try:
            file_size = file_path.stat().st_size
        except (PermissionError, OSError):
            file_size = 0

        if DRY_RUN:
            print(f"[DRY RUN] Would delete: {file_path}")
            return

        file_path.unlink(missing_ok=True)

        stats.files_deleted += 1
        stats.bytes_recovered += file_size

        logger.info("Deleted file: %s", file_path)

    except (PermissionError, OSError) as exc:
        stats.files_failed += 1
        logger.warning(
            "Could not delete file %s: %s",
            file_path,
            exc,
        )


# ============================================================
# SAFE DIRECTORY DELETION
# ============================================================

def delete_directory(
    directory: Path,
    stats: CleanupStats,
) -> None:
    """Safely delete a directory and its contents."""

    try:
        directory_size = calculate_directory_size(directory)

        if DRY_RUN:
            print(
                f"[DRY RUN] Would delete directory: "
                f"{directory} ({format_size(directory_size)})"
            )
            return

        shutil.rmtree(directory)

        stats.directories_deleted += 1
        stats.bytes_recovered += directory_size

        logger.info(
            "Deleted directory: %s (%s)",
            directory,
            format_size(directory_size),
        )

    except (PermissionError, OSError) as exc:
        stats.directories_failed += 1

        logger.warning(
            "Could not delete directory %s: %s",
            directory,
            exc,
        )


# ============================================================
# CLEAN DIRECTORY CONTENTS
# ============================================================

def clean_directory(
    directory: Path,
    stats: CleanupStats,
) -> None:
    """Clean the contents of a directory without deleting the root folder."""

    if not directory.exists():
        return

    if not directory.is_dir():
        return

    print(f"\nCleaning: {directory}")

    try:
        items = list(directory.iterdir())
    except (PermissionError, OSError) as exc:
        print(f"  Access denied: {exc}")
        logger.warning(
            "Cannot access directory %s: %s",
            directory,
            exc,
        )
        return

    for item in items:

        if item.is_symlink():
            delete_file(item, stats)

        elif item.is_file():
            delete_file(item, stats)

        elif item.is_dir():
            delete_directory(item, stats)


# ============================================================
# OLD CRASH DUMP CLEANUP
# ============================================================

def clean_old_crash_dumps(stats: CleanupStats) -> None:
    """
    Delete crash dumps older than CRASH_DUMP_MAX_AGE_DAYS.
    """

    crash_dir = Path(
        os.path.expandvars(
            r"%LOCALAPPDATA%\CrashDumps"
        )
    )

    if not crash_dir.exists():
        return

    print(
        f"\nChecking old crash dumps "
        f"(older than {CRASH_DUMP_MAX_AGE_DAYS} days)..."
    )

    import time

    cutoff_time = time.time() - (
        CRASH_DUMP_MAX_AGE_DAYS * 24 * 60 * 60
    )

    try:
        for item in crash_dir.iterdir():

            if not item.is_file():
                continue

            try:
                modified_time = item.stat().st_mtime

                if modified_time < cutoff_time:
                    delete_file(item, stats)

            except (PermissionError, OSError):
                stats.files_failed += 1

    except (PermissionError, OSError) as exc:
        logger.warning(
            "Unable to scan crash dump directory: %s",
            exc,
        )


# ============================================================
# RECYCLE BIN
# ============================================================

def empty_recycle_bin() -> bool:
    """Empty the Windows Recycle Bin."""

    if not CLEAN_RECYCLE_BIN:
        print("Recycle Bin cleanup disabled.")
        return False

    print("\nEmptying Recycle Bin...")

    if DRY_RUN:
        print("[DRY RUN] Would empty Recycle Bin.")
        return True

    try:
        # SHERB_NOCONFIRMATION = 0x00000001
        # SHERB_NOPROGRESSUI  = 0x00000002
        # SHERB_NOSOUND       = 0x00000004

        flags = 0x00000001 | 0x00000002 | 0x00000004

        result = ctypes.windll.shell32.SHEmptyRecycleBinW(
            None,
            None,
            flags,
        )

        if result == 0:
            print("Recycle Bin emptied successfully.")
            logger.info("Recycle Bin emptied.")
            return True

        print("Recycle Bin could not be emptied.")
        return False

    except Exception as exc:
        print(f"Recycle Bin error: {exc}")
        logger.exception("Recycle Bin cleanup failed.")
        return False


# ============================================================
# CLEANUP ENGINE
# ============================================================

def clear_junk_folders() -> CleanupStats:
    """Clean all configured junk locations."""

    stats = CleanupStats()

    print_header("CLEANUP")

    cleanup_paths = get_cleanup_paths()

    for directory in cleanup_paths:
        clean_directory(directory, stats)

    clean_old_crash_dumps(stats)

    return stats


# ============================================================
# CLEANUP SUMMARY
# ============================================================

def print_cleanup_summary(
    stats: CleanupStats,
    old_free_gb: float,
    new_free_gb: float,
) -> None:
    """Print the final cleanup report."""

    print_header("CLEANUP SUMMARY")

    actual_recovered = new_free_gb - old_free_gb

    print(
        f"Files deleted:        {stats.files_deleted}"
    )

    print(
        f"Folders deleted:      {stats.directories_deleted}"
    )

    print(
        f"Failed file deletes:  {stats.files_failed}"
    )

    print(
        f"Failed folder deletes:{stats.directories_failed}"
    )

    print(
        f"Estimated recovered:  "
        f"{format_size(stats.bytes_recovered)}"
    )

    print(
        f"Actual disk recovery: "
        f"{actual_recovered:.2f} GB"
    )

    print(
        f"Free space now:       "
        f"{new_free_gb:.2f} GB"
    )


# ============================================================
# SYSTEM INFORMATION
# ============================================================

def show_system_info() -> None:
    """Display useful disk/system information."""

    print_header("SYSTEM INFORMATION")

    total, used, free = get_disk_usage(DRIVE)

    free_percent, _ = get_free_space(DRIVE)

    print(f"Drive:          {DRIVE}")
    print(f"Total space:    {total:.2f} GB")
    print(f"Used space:     {used:.2f} GB")
    print(f"Free space:     {free:.2f} GB")
    print(f"Free percent:   {free_percent:.2f}%")
    print(f"Administrator:  {'Yes' if is_admin() else 'No'}")
    print(f"Dry-run mode:   {'Yes' if DRY_RUN else 'No'}")


# ============================================================
# USER CONFIRMATION
# ============================================================

def ask_confirmation(message: str) -> bool:
    """Ask the user for confirmation."""

    while True:

        answer = input(
            f"{message} (y/n): "
        ).strip().lower()

        if answer in {"y", "yes"}:
            return True

        if answer in {"n", "no"}:
            return False

        print("Please enter y or n.")


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    """Main application entry point."""

    logger.info("Disk cleanup utility started.")

    print_header("WINDOWS DISK CLEANUP UTILITY")

    if os.name != "nt":
        print(
            "This program is designed for Windows."
        )
        return

    show_system_info()

    if not is_admin():
        print(
            "\nWARNING:"
            "\nThe program is not running as Administrator."
            "\nSome Windows folders may be inaccessible."
        )

    free_percent, old_free_gb = get_free_space(DRIVE)

    print(
        f"\nCurrent free space: "
        f"{free_percent:.2f}% "
        f"({old_free_gb:.2f} GB)"
    )

    # --------------------------------------------------------
    # AUTOMATIC CLEANUP
    # --------------------------------------------------------

    if free_percent < THRESHOLD_PERCENT:

        print(
            f"\nWARNING: Free space is below "
            f"{THRESHOLD_PERCENT}%."
        )

        print(
            "Automatic cleanup has been triggered."
        )

        run_cleanup = True

    # --------------------------------------------------------
    # MANUAL CLEANUP
    # --------------------------------------------------------

    else:

        print(
            f"\nFree space is above the "
            f"{THRESHOLD_PERCENT}% threshold."
        )

        run_cleanup = ask_confirmation(
            "Would you like to run cleanup anyway?"
        )

    if not run_cleanup:

        print("\nCleanup cancelled.")
        logger.info("Cleanup cancelled by user.")
        return

    # --------------------------------------------------------
    # DRY RUN CONFIRMATION
    # --------------------------------------------------------

    if DRY_RUN:
        print(
            "\nDRY-RUN MODE ENABLED."
            "\nNo files will actually be deleted."
        )

    # --------------------------------------------------------
    # RUN CLEANUP
    # --------------------------------------------------------

    stats = clear_junk_folders()

    empty_recycle_bin()

    # --------------------------------------------------------
    # FINAL DISK CHECK
    # --------------------------------------------------------

    new_percent, new_free_gb = get_free_space(DRIVE)

    print_cleanup_summary(
        stats,
        old_free_gb,
        new_free_gb,
    )

    print(
        f"\nFree space percentage: "
        f"{new_percent:.2f}%"
    )

    logger.info(
        "Cleanup completed. Recovered approximately %.2f GB.",
        new_free_gb - old_free_gb,
    )

    print(
        "\nLog file:"
        f"\n{LOG_FILE.resolve()}"
    )

    print("\nDone.")


# ============================================================
# PROGRAM ENTRY POINT
# ============================================================

if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user.")
        logger.info("Program interrupted by user.")

    except Exception as exc:
        print(
            f"\nUnexpected error: {exc}"
        )
        logger.exception(
            "Unhandled application error."
        )

        sys.exit(1)
