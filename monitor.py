import ctypes
import os
import shutil
import winshell

# Settings
DRIVE = "C:\\"
THRESHOLD_PERCENT = 10


def is_admin():
  """Check if the script is running with administrator privileges."""
  try:
    return ctypes.windll.shell32.IsUserAnAdmin()
  except Exception:
    return False


def get_free_space_percentage(drive):
  """Returns the free space percentage and free gigabytes of the specified drive."""
  total, used, free = shutil.disk_usage(drive)
  free_percent = (free / total) * 100
  free_gb = free / (1024**3)
  return free_percent, free_gb


def clear_temp_folders():
  """Deletes files and subdirectories from Windows temporary folders."""
  temp_paths = [
      os.path.expandvars(r"%TEMP%"),
      os.path.expandvars(r"%SystemRoot%\Temp"),
  ]

  deleted_files = 0
  deleted_dirs = 0
  failed_count = 0

  print("--- Deleting temporary files ---")
  for path in temp_paths:
    if os.path.exists(path):
      # topdown=False allows deleting files before clearing their parent directories
      for root, dirs, files in os.walk(path, topdown=False):
        for file in files:
          file_path = os.path.join(root, file)
          try:
            os.remove(file_path)
            deleted_files += 1
          except Exception:
            failed_count += 1

        for dir_name in dirs:
          dir_path = os.path.join(root, dir_name)
          try:
            os.rmdir(dir_path)
            deleted_dirs += 1
          except Exception:
            pass  # Folder might still contain in-use files

  print(
      f"Successfully deleted: {deleted_files} files and {deleted_dirs} folders."
      f" (Skipped in-use: {failed_count})"
  )


def empty_recycle_bin():
  """Empties the Recycle Bin."""
  print("--- Emptying Recycle Bin ---")
  try:
    winshell.recycle_bin().empty(confirm=False, show_progress=False, sound=False)
    print("Recycle Bin successfully emptied.")
  except Exception as e:
    print(f"Recycle Bin is already empty or an error occurred: {e}")


def main():
  if not is_admin():
    print(
        "NOTE: Script is not running as Administrator. System Temp cleanup"
        " might be limited.\n"
    )

  free_percent, free_gb = get_free_space_percentage(DRIVE)
  print(f"Drive: {DRIVE} | Free space: {free_percent:.2f}% ({free_gb:.2f} GB)")

  if free_percent < THRESHOLD_PERCENT:
    print(f"\nWARNING: Free space is below {THRESHOLD_PERCENT}%! Starting cleanup...")

    clear_temp_folders()
    empty_recycle_bin()

    new_percent, new_gb = get_free_space_percentage(DRIVE)
    print(f"\nCleanup complete! New free space: {new_percent:.2f}% ({new_gb:.2f} GB)")
  else:
    print("Sufficient free space available. No cleanup needed.")


if __name__ == "__main__":
  main()