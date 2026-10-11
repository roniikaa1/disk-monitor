#!/usr/bin/env python3
"""
Windows Disk Cleanup Utility v2
===============================

Scan -> preview -> confirm -> clean, with an accurate per-category report.

Examples
--------
  python disk_cleanup.py                    # interactive: preview, then confirm
  python disk_cleanup.py --dry-run          # preview only, nothing is deleted
  python disk_cleanup.py --yes              # no prompts (for automation)
  python disk_cleanup.py --analyze          # show biggest folders / files
  python disk_cleanup.py --install-task weekly --task-time 03:00
  python disk_cleanup.py --help

Requires Python 3.9+ and Windows. Run as Administrator for full cleanup.
"""

from __future__ import annotations
import argparse
import ctypes
import glob
import heapq
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterator, List, Optional, Set, Tuple

# ============================================================
# CONFIGURATION (defaults - every one can be overridden by CLI)
# ============================================================

DEFAULT_DRIVE = "C:\\"
THRESHOLD_PERCENT = 10.0

DRY_RUN = False

CLEAN_RECYCLE_BIN = True
CLEAN_BROWSER_CACHES = True
CLEAN_WINDOWS_UPDATE_CACHE = True
CLEAN_PREFETCH = False
CLEAN_DEV_CACHES = False  # pip / npm / yarn / NuGet caches

# Files in temp folders modified within this many days are left alone
# (they may belong to a running installer or application).
TEMP_MIN_AGE_DAYS = 1.0

# Only crash dumps older than this many days are deleted.
CRASH_DUMP_MAX_AGE_DAYS = 30.0

TASK_NAME = "WindowsDiskCleanup"
REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT (junctions, symlinks)

logger = logging.getLogger("disk_cleanup")


# ============================================================
# DATA STRUCTURES
# ============================================================

@dataclass
class Result:
    key: str
    name: str
    files: int = 0
    dirs: int = 0
    bytes: int = 0
    failed: int = 0
    kept_recent: int = 0
    errors: List[str] = field(default_factory=list)


@dataclass
class Category:
    key: str
    name: str
    patterns: Tuple[str, ...]
    enabled: bool = True
    needs_admin: bool = False
    age_days: float = 0.0           # keep files newer than this
    processes: Tuple[str, ...] = ()  # must not be running (locked files)
    services: Tuple[str, ...] = ()   # stopped during cleanup, then restarted
    env_vars: Tuple[str, ...] = ()   # extra directories from env variables


@dataclass
class ScanItem:
    key: str
    name: str
    cat: Optional[Category] = None
    targets: List[str] = field(default_factory=list)
    result: Optional[Result] = None
    skip_reason: str = ""
    selected: bool = True

    @property
    def actionable(self) -> bool:
        return (
            self.selected
            and not self.skip_reason
            and self.result is not None
            and (self.result.files or self.result.dirs)
        )


@dataclass
class Profile:
    name: str
    local: str
    roaming: str


# ============================================================
# BASIC HELPERS
# ============================================================

def format_size(size_bytes: float) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{int(size)} B" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


def print_header(title: str) -> None:
    print()
    print("=" * 66)
    print(title)
    print("=" * 66)


def ask(message: str, default: bool = False) -> bool:
    while True:
        try:
            answer = input(f"{message} ({'Y/n' if default else 'y/N'}): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please enter y or n.")


def is_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        logger.exception("Administrator check failed")
        return False


def relaunch_as_admin() -> bool:
    """Re-run this script elevated (UAC prompt). Returns True if launched."""
    script = os.path.abspath(sys.argv[0])
    args = [script] + [a for a in sys.argv[1:] if a != "--pause"] + ["--pause"]
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable,
            subprocess.list2cmdline(args), os.getcwd(), 1,
        )
        return rc > 32
    except Exception:
        logger.exception("Elevation failed")
        return False


def normalize_drive(drive: str) -> str:
    drive = drive.strip()
    if len(drive) == 2 and drive[1] == ":":
        drive += "\\"
    return drive


@dataclass
class Usage:
    total: int
    used: int
    free: int

    @property
    def free_percent(self) -> float:
        return (self.free / self.total * 100) if self.total else 0.0


def disk_usage(drive: str) -> Usage:
    total, used, free = shutil.disk_usage(drive)
    return Usage(total, used, free)


# ============================================================
# LOGGING
# ============================================================

def setup_logging(log_file: Optional[str], verbose: bool) -> Path:
    if log_file:
        path = Path(log_file)
    else:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        path = Path(base) / "DiskCleanup" / "disk_cleanup.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
    except OSError:
        path = Path(__file__).with_name("disk_cleanup.log")
        handler = RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    return path


# ============================================================
# FILE-SYSTEM SAFETY
# ============================================================

def is_link(entry: os.DirEntry) -> bool:
    """True for symlinks and directory junctions (never follow these)."""
    try:
        if entry.is_symlink():
            return True
        if entry.is_dir(follow_symlinks=False):
            attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
            return bool(attrs & REPARSE_POINT)
    except OSError:
        pass
    return False


def is_safe_target(path: str) -> bool:
    """Refuse drive roots and well-known top-level system/user folders."""
    p = Path(os.path.abspath(path))
    if len(p.parts) < 3:
        return False
    protected = [
        os.environ.get(v)
        for v in ("USERPROFILE", "SystemRoot", "ProgramFiles", "ProgramFiles(x86)",
                  "ProgramData", "APPDATA", "LOCALAPPDATA")
    ] + [os.path.join(os.environ.get("SystemDrive", "C:") + os.sep, "Users")]
    norm = {os.path.normcase(os.path.abspath(x)) for x in protected if x}
    return os.path.normcase(str(p)) not in norm


# ============================================================
# PURGER - the one code path used for both preview and delete
# ============================================================

class Purger:
    """
    Deletes (or, in dry-run mode, merely counts) the contents of a directory.

    Works file by file so one locked file never aborts a whole folder, never
    follows junctions/symlinks, and keeps files newer than `cutoff`.
    """

    def __init__(self, cutoff: Optional[float], dry_run: bool, result: Result):
        self.cutoff = cutoff
        self.dry_run = dry_run
        self.result = result

    def _fail(self, path: str, exc: Exception) -> None:
        self.result.failed += 1
        if len(self.result.errors) < 25:
            self.result.errors.append(f"{path}: {exc}")
        logger.debug("Could not delete %s: %s", path, exc)

    def _delete_file(self, path: str, size: int) -> bool:
        if not self.dry_run:
            try:
                try:
                    os.unlink(path)
                except PermissionError:
                    os.chmod(path, stat.S_IWRITE)  # clear read-only flag
                    os.unlink(path)
            except FileNotFoundError:
                return True
            except OSError as exc:
                self._fail(path, exc)
                return False
            logger.debug("Deleted file: %s", path)
        self.result.files += 1
        self.result.bytes += size
        return True

    def _remove_link(self, path: str) -> bool:
        """Remove a symlink/junction itself, never its target."""
        if not self.dry_run:
            try:
                try:
                    os.unlink(path)
                except OSError:
                    os.rmdir(path)
            except OSError as exc:
                self._fail(path, exc)
                return False
        self.result.files += 1
        return True

    def _rmdir(self, path: str) -> bool:
        if not self.dry_run:
            try:
                os.rmdir(path)
            except FileNotFoundError:
                return True
            except OSError as exc:
                self._fail(path, exc)
                return False
            logger.debug("Deleted directory: %s", path)
        self.result.dirs += 1
        return True

    def _too_new(self, mtime: float) -> bool:
        return self.cutoff is not None and mtime > self.cutoff

    def purge_dir(self, path: str) -> bool:
        """Clean `path`. Returns True if it is (or would be) empty afterwards."""
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError as exc:
            self._fail(path, exc)
            return False

        empty = True
        for entry in entries:
            try:
                if is_link(entry):
                    empty &= self._remove_link(entry.path)
                elif entry.is_dir(follow_symlinks=False):
                    if self.purge_dir(entry.path):
                        empty &= self._rmdir(entry.path)
                    else:
                        empty = False
                else:
                    st = entry.stat(follow_symlinks=False)
                    if self._too_new(st.st_mtime):
                        self.result.kept_recent += 1
                        empty = False
                    else:
                        empty &= self._delete_file(entry.path, st.st_size)
            except OSError as exc:
                self._fail(entry.path, exc)
                empty = False
        return empty

    def purge_target(self, path: str) -> None:
        if os.path.islink(path):
            return
        if os.path.isdir(path):
            self.purge_dir(path)  # the root folder itself is always kept
        elif os.path.isfile(path):
            try:
                st = os.stat(path)
                if self._too_new(st.st_mtime):
                    self.result.kept_recent += 1
                else:
                    self._delete_file(path, st.st_size)
            except OSError as exc:
                self._fail(path, exc)


# ============================================================
# CATEGORIES
# ============================================================

def build_categories(args: argparse.Namespace) -> List[Category]:
    def chromium(key: str, name: str, vendor: str, exe: str) -> Category:
        base = "{LOCAL}\\" + vendor + "\\User Data\\*\\"
        return Category(
            key, name,
            tuple(base + sub for sub in ("Cache", "Code Cache", "GPUCache")),
            enabled=args.browsers, processes=(exe,),
        )

    return [
        Category("user_temp", "User temp files", ("{LOCAL}\\Temp",),
                 age_days=args.min_age_days, env_vars=("TEMP", "TMP")),
        Category("win_temp", "Windows temp files", ("{WINDIR}\\Temp",),
                 needs_admin=True, age_days=args.min_age_days),
        Category("crash", f"Crash dumps (>{args.crash_age_days:g} days old)",
                 ("{LOCAL}\\CrashDumps", "{WINDIR}\\Minidump", "{WINDIR}\\MEMORY.DMP"),
                 age_days=args.crash_age_days),
        Category("wer", "Windows Error Reporting",
                 ("{LOCAL}\\Microsoft\\Windows\\WER\\ReportArchive",
                  "{LOCAL}\\Microsoft\\Windows\\WER\\ReportQueue",
                  "{PROGRAMDATA}\\Microsoft\\Windows\\WER\\ReportArchive",
                  "{PROGRAMDATA}\\Microsoft\\Windows\\WER\\ReportQueue")),
        Category("gpu", "GPU shader caches",
                 ("{LOCAL}\\D3DSCache", "{LOCAL}\\NVIDIA\\DXCache",
                  "{LOCAL}\\NVIDIA\\GLCache", "{LOCAL}\\AMD\\DxCache")),
        Category("wu", "Windows Update download cache",
                 ("{WINDIR}\\SoftwareDistribution\\Download",),
                 enabled=args.wu_cache, needs_admin=True, services=("wuauserv",)),
        Category("do", "Delivery Optimization cache",
                 ("{WINDIR}\\ServiceProfiles\\NetworkService\\AppData\\Local"
                  "\\Microsoft\\Windows\\DeliveryOptimization\\Cache",),
                 enabled=args.wu_cache, needs_admin=True, services=("dosvc",)),
        chromium("chrome", "Chrome cache", "Google\\Chrome", "chrome.exe"),
        chromium("edge", "Edge cache", "Microsoft\\Edge", "msedge.exe"),
        chromium("brave", "Brave cache", "BraveSoftware\\Brave-Browser", "brave.exe"),
        Category("firefox", "Firefox cache",
                 ("{LOCAL}\\Mozilla\\Firefox\\Profiles\\*\\cache2",),
                 enabled=args.browsers, processes=("firefox.exe",)),
        Category("prefetch", "Prefetch (slower next launches)",
                 ("{WINDIR}\\Prefetch",), enabled=args.prefetch, needs_admin=True),
        Category("dev", "Developer caches (pip/npm/yarn/NuGet)",
                 ("{LOCAL}\\pip\\Cache", "{LOCAL}\\npm-cache",
                  "{LOCAL}\\Yarn\\Cache", "{LOCAL}\\NuGet\\v3-cache"),
                 enabled=args.dev_caches),
    ]


def get_profiles(all_users: bool) -> List[Profile]:
    local = os.environ.get("LOCALAPPDATA", "")
    current = Profile(os.environ.get("USERNAME", "current"), local,
                      os.environ.get("APPDATA", ""))
    profiles = [current] if local else []
    if not all_users:
        return profiles

    users_dir = Path(os.environ.get("SystemDrive", "C:") + "\\Users")
    skip = {"public", "default", "default user", "all users"}
    try:
        for d in users_dir.iterdir():
            if d.name.lower() in skip or not d.is_dir():
                continue
            loc = d / "AppData" / "Local"
            if loc.is_dir() and os.path.normcase(str(loc)) != os.path.normcase(local):
                profiles.append(Profile(d.name, str(loc), str(d / "AppData" / "Roaming")))
    except OSError as exc:
        logger.warning("Cannot enumerate user profiles: %s", exc)
    return profiles


def resolve_targets(cat: Category, profiles: List[Profile]) -> List[str]:
    windir = os.environ.get("SystemRoot", "C:\\Windows")
    progdata = os.environ.get("ProgramData", "C:\\ProgramData")

    def fill(tpl: str, **kw: str) -> str:
        for k, v in kw.items():
            tpl = tpl.replace("{" + k + "}", v)
        return tpl

    candidates: List[str] = []
    for tpl in cat.patterns:
        if "{LOCAL}" in tpl or "{ROAMING}" in tpl:
            for p in profiles:
                candidates.append(fill(tpl, LOCAL=p.local, ROAMING=p.roaming,
                                       WINDIR=windir, PROGRAMDATA=progdata))
        else:
            candidates.append(fill(tpl, WINDIR=windir, PROGRAMDATA=progdata))
    for var in cat.env_vars:
        if os.environ.get(var):
            candidates.append(os.environ[var])

    out: List[str] = []
    seen: Set[str] = set()
    for c in candidates:
        for path in (glob.glob(c) if "*" in c else [c]):
            key = os.path.normcase(os.path.abspath(path))
            if key not in seen and os.path.exists(path):
                seen.add(key)
                out.append(path)
    return out


def process_category(cat: Category, targets: List[str], dry_run: bool) -> Result:
    result = Result(cat.key, cat.name)
    cutoff = time.time() - cat.age_days * 86400 if cat.age_days > 0 else None
    purger = Purger(cutoff, dry_run, result)
    for target in targets:
        if not is_safe_target(target):
            result.errors.append(f"refused unsafe path: {target}")
            logger.warning("Refused unsafe path: %s", target)
            continue
        purger.purge_target(target)
    return result


# ============================================================
# PROCESSES & SERVICES
# ============================================================

def running_processes() -> Set[str]:
    try:
        out = subprocess.run(
            ["tasklist", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    names = set()
    for line in out.splitlines():
        if line.startswith('"'):
            names.add(line.split('","')[0].strip('"').lower())
    return names


def service_running(name: str) -> bool:
    try:
        out = subprocess.run(["sc", "query", name], capture_output=True,
                             text=True, timeout=30).stdout
        return "RUNNING" in out
    except (OSError, subprocess.SubprocessError):
        return False


@contextmanager
def stopped_services(names: Tuple[str, ...], active: bool) -> Iterator[None]:
    """Stop services while their cache is cleaned, always restart afterwards."""
    stopped: List[str] = []
    try:
        if active:
            for name in names:
                if service_running(name):
                    print(f"  Stopping service {name}...")
                    subprocess.run(["net", "stop", name, "/y"], capture_output=True)
                    stopped.append(name)
        yield
    finally:
        for name in reversed(stopped):
            print(f"  Restarting service {name}...")
            subprocess.run(["net", "start", name], capture_output=True)


# ============================================================
# RECYCLE BIN
# ============================================================

class _SHQUERYRBINFO(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("cbSize", ctypes.c_uint32),
                ("i64Size", ctypes.c_int64),
                ("i64NumItems", ctypes.c_int64)]


def query_recycle_bin(drive: str) -> Tuple[int, int]:
    """Return (item_count, bytes) for the drive's Recycle Bin."""
    try:
        info = _SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(info)
        hr = ctypes.windll.shell32.SHQueryRecycleBinW(drive, ctypes.byref(info))
        return (int(info.i64NumItems), int(info.i64Size)) if hr == 0 else (0, 0)
    except Exception:
        logger.exception("Recycle Bin query failed")
        return 0, 0


def empty_recycle_bin(drive: str) -> bool:
    # NOCONFIRMATION | NOPROGRESSUI | NOSOUND
    flags = 0x1 | 0x2 | 0x4
    try:
        hr = ctypes.windll.shell32.SHEmptyRecycleBinW(None, drive, flags)
        ok = (hr & 0xFFFFFFFF) == 0
        (logger.info if ok else logger.warning)("Recycle Bin empty result: 0x%08X", hr & 0xFFFFFFFF)
        return ok
    except Exception:
        logger.exception("Recycle Bin cleanup failed")
        return False


# ============================================================
# SCAN / CLEAN PIPELINE
# ============================================================

def scan_all(args, categories, profiles, admin) -> List[ScanItem]:
    need_proc = any(c.enabled and c.processes for c in categories)
    running = running_processes() if need_proc else set()

    items: List[ScanItem] = []
    for cat in categories:
        if not cat.enabled:
            continue
        item = ScanItem(cat.key, cat.name, cat=cat)
        busy = [p for p in cat.processes if p.lower() in running]

        if cat.needs_admin and not admin:
            item.skip_reason = "needs administrator"
        elif busy and not args.ignore_running:
            item.skip_reason = f"{', '.join(busy)} is running"
        else:
            print(f"  Scanning: {cat.name:<45}", end="\r", flush=True)
            item.targets = resolve_targets(cat, profiles)
            item.result = process_category(cat, item.targets, dry_run=True)
        items.append(item)

    if args.recycle_bin:
        count, size = query_recycle_bin(args.drive)
        items.append(ScanItem(
            "recycle", "Recycle Bin",
            result=Result("recycle", "Recycle Bin", files=count, bytes=size),
        ))
    print(" " * 70, end="\r")
    return items


def print_scan_table(items: List[ScanItem]) -> int:
    print(f"\n {'#':>2}  {'Category':<40} {'Items':>8} {'Size':>12}  Note")
    print(" " + "-" * 82)
    total = 0
    for i, it in enumerate(items, 1):
        r = it.result
        if it.skip_reason:
            print(f" {i:>2}  {it.name:<40} {'-':>8} {'-':>12}  skipped: {it.skip_reason}")
            continue
        n = (r.files + r.dirs) if r else 0
        notes = []
        if r and r.kept_recent:
            notes.append(f"{r.kept_recent} recent kept")
        if not it.selected:
            notes.append("excluded")
        else:
            total += r.bytes if r else 0
        print(f" {i:>2}  {it.name:<40} {n:>8,} {format_size(r.bytes if r else 0):>12}  {', '.join(notes)}")
    print(" " + "-" * 82)
    print(f"     {'Reclaimable (selected)':<40} {'':>8} {format_size(total):>12}")
    return total


def choose_categories(items: List[ScanItem]) -> None:
    raw = input("Numbers to EXCLUDE (comma separated, blank = none): ").strip()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= len(items):
            items[int(part) - 1].selected = False


def clean_items(items: List[ScanItem], args, admin: bool) -> List[Result]:
    results: List[Result] = []
    for it in items:
        if not it.actionable:
            continue
        print(f"\nCleaning: {it.name}")
        if it.key == "recycle":
            res = Result("recycle", it.name)
            if args.dry_run:
                res.files, res.bytes = it.result.files, it.result.bytes
                print("  [DRY RUN] Would empty the Recycle Bin")
            elif empty_recycle_bin(args.drive):
                res.files, res.bytes = it.result.files, it.result.bytes
                print("  Recycle Bin emptied.")
            else:
                res.failed = 1
                print("  Recycle Bin could not be emptied.")
        else:
            with stopped_services(it.cat.services, active=not args.dry_run and admin):
                res = process_category(it.cat, it.targets, args.dry_run)
            verb = "Would free" if args.dry_run else "Freed"
            print(f"  {verb} {format_size(res.bytes)} "
                  f"({res.files:,} files, {res.dirs:,} folders"
                  f"{f', {res.failed:,} in use/denied' if res.failed else ''})")
        logger.info("%s: files=%d dirs=%d bytes=%d failed=%d dry=%s",
                    res.name, res.files, res.dirs, res.bytes, res.failed, args.dry_run)
        results.append(res)
    return results


def print_summary(results: List[Result], before: Usage, after: Usage, dry: bool) -> None:
    print_header("CLEANUP SUMMARY" + ("  (DRY RUN - nothing deleted)" if dry else ""))
    print(f" {'Category':<40} {'Items':>8} {'Size':>12} {'Failed':>7}")
    print(" " + "-" * 70)
    for r in results:
        print(f" {r.name:<40} {r.files + r.dirs:>8,} {format_size(r.bytes):>12} {r.failed:>7,}")
    est = sum(r.bytes for r in results)
    print(" " + "-" * 70)
    print(f" {'Total ' + ('reclaimable' if dry else 'removed'):<40} "
          f"{sum(r.files + r.dirs for r in results):>8,} {format_size(est):>12} "
          f"{sum(r.failed for r in results):>7,}")
    if not dry:
        print(f"\n Free before : {format_size(before.free)} ({before.free_percent:.2f}%)")
        print(f" Free after  : {format_size(after.free)} ({after.free_percent:.2f}%)")
        print(f" Actual gain : {format_size(max(after.free - before.free, 0))}")


# ============================================================
# ANALYSIS (read-only)
# ============================================================

def scan_size(root: str, top_n: int) -> Tuple[int, List[Tuple[int, str]]]:
    """Total size of a tree plus its `top_n` largest files. Never follows links."""
    total = 0
    heap: List[Tuple[int, str]] = []
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if is_link(e):
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            sz = e.stat(follow_symlinks=False).st_size
                            total += sz
                            if top_n:
                                if len(heap) < top_n:
                                    heapq.heappush(heap, (sz, e.path))
                                elif sz > heap[0][0]:
                                    heapq.heapreplace(heap, (sz, e.path))
                    except OSError:
                        continue
        except OSError:
            continue
    return total, heap


def analyze_drive(drive: str, top_n: int) -> None:
    print_header("DISK USAGE ANALYSIS (read-only)")
    print("Scanning... this can take several minutes on a large drive.\n")

    folders: List[Tuple[int, str]] = []
    all_files: List[Tuple[int, str]] = []
    root_files = 0
    try:
        top_entries = list(os.scandir(drive))
    except OSError as exc:
        print(f"Cannot read {drive}: {exc}")
        return

    dirs = []
    for e in top_entries:
        try:
            if is_link(e):
                continue
            if e.is_dir(follow_symlinks=False):
                dirs.append(e)
            elif e.is_file(follow_symlinks=False):
                sz = e.stat(follow_symlinks=False).st_size
                root_files += sz
                all_files.append((sz, e.path))
        except OSError:
            continue

    workers = min(8, (os.cpu_count() or 2) * 2)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(scan_size, d.path, top_n): d.name for d in dirs}
        done = 0
        for fut in as_completed(futures):
            done += 1
            print(f"  {done}/{len(futures)} folders scanned", end="\r", flush=True)
            size, heap = fut.result()
            folders.append((size, futures[fut]))
            all_files.extend(heap)
    print(" " * 40, end="\r")

    folders.append((root_files, "(files in drive root)"))
    folders.sort(reverse=True)
    grand = sum(s for s, _ in folders) or 1

    print("Largest top-level folders:\n")
    for size, name in folders[:15]:
        pct = size / grand * 100
        bar = "#" * int(pct / 100 * 30)
        print(f"  {name:<28} {format_size(size):>11} {pct:5.1f}%  {bar}")

    print(f"\nLargest {top_n} files:\n")
    for size, path in heapq.nlargest(top_n, all_files):
        print(f"  {format_size(size):>11}  {path}")
    print("\nNote: folders you lack permission to read are not counted.")


def print_tips(drive: str) -> None:
    tips = []
    wo = os.path.join(drive, "Windows.old")
    if os.path.isdir(wo):
        tips.append(f"Windows.old exists ({format_size(scan_size(wo, 0)[0])}). Remove it via "
                    "Settings > System > Storage > Temporary files > 'Previous Windows installation(s)'.")
    hib = os.path.join(drive, "hiberfil.sys")
    try:
        tips.append(f"hiberfil.sys is {format_size(os.path.getsize(hib))}. If you never "
                    "hibernate, 'powercfg /h off' (admin) reclaims it.")
    except OSError:
        pass
    if tips:
        print_header("FURTHER SAVINGS (manual)")
        for t in tips:
            print(f" - {t}")


def run_component_cleanup(dry_run: bool) -> None:
    cmd = ["Dism.exe", "/Online", "/Cleanup-Image", "/StartComponentCleanup"]
    print_header("WINDOWS COMPONENT STORE CLEANUP (DISM)")
    if dry_run:
        print("[DRY RUN] Would run:", " ".join(cmd))
        return
    print("Running DISM - this can take a while...")
    rc = subprocess.run(cmd).returncode
    logger.info("DISM component cleanup exit code %s", rc)


# ============================================================
# SCHEDULED TASK
# ============================================================

def install_task(args) -> int:
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")  # no console window for scheduled runs
    parts = [str(pyw if pyw.exists() else exe), os.path.abspath(__file__),
             "--yes", "--only-if-low",
             "--drive", args.drive.rstrip("\\"),
             "--threshold", f"{args.threshold:g}",
             "--min-age-days", f"{args.min_age_days:g}",
             "--crash-age-days", f"{args.crash_age_days:g}"]
    for flag, val in (("browsers", args.browsers), ("recycle-bin", args.recycle_bin),
                      ("wu-cache", args.wu_cache), ("prefetch", args.prefetch),
                      ("dev-caches", args.dev_caches)):
        parts.append(f"--{'' if val else 'no-'}{flag}")

    cmd = ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", subprocess.list2cmdline(parts),
           "/SC", args.install_task.upper(), "/ST", args.task_time, "/RL", "HIGHEST", "/F"]
    if args.install_task == "weekly":
        cmd += ["/D", "SUN"]
    rc = subprocess.run(cmd).returncode
    if rc == 0:
        print(f"\nScheduled task '{TASK_NAME}' created ({args.install_task} at {args.task_time}).")
        print("It only cleans when free space is below the threshold.")
    return rc


def remove_task() -> int:
    return subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]).returncode


# ============================================================
# CLI
# ============================================================

def parse_args(argv=None) -> argparse.Namespace:
    B = argparse.BooleanOptionalAction
    p = argparse.ArgumentParser(
        description="Windows disk cleanup: scan, preview, confirm, clean.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--drive", default=DEFAULT_DRIVE, help="Drive to check and clean")
    p.add_argument("--threshold", type=float, default=THRESHOLD_PERCENT,
                   help="Free-space %% below which the drive counts as low")
    p.add_argument("--dry-run", action="store_true", default=DRY_RUN,
                   help="Preview only; delete nothing")
    p.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompts")
    p.add_argument("--only-if-low", action="store_true",
                   help="Exit unless free space is below the threshold")
    p.add_argument("--analyze", action="store_true",
                   help="Show biggest folders/files instead of cleaning")
    p.add_argument("--top", type=int, default=15, help="Files to list with --analyze")

    g = p.add_argument_group("what to clean")
    g.add_argument("--browsers", action=B, default=CLEAN_BROWSER_CACHES)
    g.add_argument("--recycle-bin", action=B, default=CLEAN_RECYCLE_BIN)
    g.add_argument("--wu-cache", action=B, default=CLEAN_WINDOWS_UPDATE_CACHE,
                   help="Windows Update + Delivery Optimization caches")
    g.add_argument("--prefetch", action=B, default=CLEAN_PREFETCH)
    g.add_argument("--dev-caches", action=B, default=CLEAN_DEV_CACHES)
    g.add_argument("--component-cleanup", action="store_true",
                   help="Also run DISM /StartComponentCleanup (admin, slow)")
    g.add_argument("--all-users", action="store_true",
                   help="Clean every user profile (admin)")
    g.add_argument("--min-age-days", type=float, default=TEMP_MIN_AGE_DAYS,
                   help="Keep temp files modified more recently than this")
    g.add_argument("--crash-age-days", type=float, default=CRASH_DUMP_MAX_AGE_DAYS)
    g.add_argument("--ignore-running", action="store_true",
                   help="Clean browser caches even if the browser is running")

    o = p.add_argument_group("output / automation")
    o.add_argument("--report", metavar="FILE", help="Write a JSON report")
    o.add_argument("--log-file", metavar="FILE")
    o.add_argument("--verbose", action="store_true", help="Log every deleted path")
    o.add_argument("--install-task", nargs="?", const="weekly",
                   choices=["daily", "weekly", "monthly"],
                   help="Create a scheduled task")
    o.add_argument("--task-time", default="03:00", help="HH:MM for the scheduled task")
    o.add_argument("--remove-task", action="store_true")
    o.add_argument("--no-elevate", action="store_true", help="Don't offer to relaunch as admin")
    o.add_argument("--pause", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    args.drive = normalize_drive(args.drive)
    return args


def write_report(path: str, args, before: Usage, after: Usage, results: List[Result]) -> None:
    data = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "drive": args.drive,
        "dry_run": args.dry_run,
        "free_before_bytes": before.free,
        "free_after_bytes": after.free,
        "bytes_removed_estimate": sum(r.bytes for r in results),
        "results": [asdict(r) for r in results],
    }
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Report written to {path}")


# ============================================================
# MAIN
# ============================================================

def run(args) -> int:
    started = time.time()
    log_path = setup_logging(args.log_file, args.verbose)
    logger.info("Disk cleanup started: %s", vars(args))

    print_header("WINDOWS DISK CLEANUP UTILITY")
    if os.name != "nt":
        print("This program is designed for Windows.")
        return 1

    if args.remove_task:
        return remove_task()
    if args.install_task:
        return install_task(args)

    admin = is_admin()
    usage = disk_usage(args.drive)
    print(f"Drive:          {args.drive}")
    print(f"Total / Used:   {format_size(usage.total)} / {format_size(usage.used)}")
    print(f"Free:           {format_size(usage.free)} ({usage.free_percent:.2f}%)")
    print(f"Administrator:  {'Yes' if admin else 'No'}")
    print(f"Dry-run mode:   {'Yes' if args.dry_run else 'No'}")

    if args.analyze:
        analyze_drive(args.drive, args.top)
        print_tips(args.drive)
        return 0

    low = usage.free_percent < args.threshold
    if args.only_if_low and not low:
        print(f"\nFree space is above {args.threshold:g}% - nothing to do.")
        return 0

    # Offer elevation (interactive sessions only)
    if not admin and not args.no_elevate and not args.yes and sys.stdin and sys.stdin.isatty():
        print("\nWithout administrator rights, Windows temp, update caches and "
              "Prefetch are skipped.")
        if ask("Relaunch as Administrator now?"):
            if relaunch_as_admin():
                return 0
            print("Could not elevate; continuing without admin rights.")

    if low:
        print(f"\nWARNING: free space is below {args.threshold:g}%.")
    elif not args.yes and not ask(
        f"\nFree space is above the {args.threshold:g}% threshold. Run cleanup anyway?"
    ):
        print("Cleanup cancelled.")
        return 0

    if args.all_users and not admin:
        print("Note: --all-users needs administrator rights; other profiles may be skipped.")

    # ---- Phase 1: scan ----
    print_header("SCANNING")
    profiles = get_profiles(args.all_users)
    items = scan_all(args, build_categories(args), profiles, admin)
    total = print_scan_table(items)

    if not any(i.actionable for i in items):
        print("\nNothing to clean.")
        return 0

    # ---- Phase 2: confirm ----
    if not args.dry_run and not args.yes:
        while True:
            try:
                answer = input("\nProceed? [y]es all / [n]o / [s]elect categories: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "n"
            if answer in {"y", "yes"}:
                break
            if answer in {"n", "no"}:
                print("Cleanup cancelled.")
                logger.info("Cancelled by user.")
                return 0
            if answer in {"s", "select"}:
                choose_categories(items)
                total = print_scan_table(items)
                if not any(i.actionable for i in items):
                    print("\nNothing selected.")
                    return 0

    # ---- Phase 3: clean ----
    print_header("CLEANING" + (" (DRY RUN)" if args.dry_run else ""))
    before = disk_usage(args.drive)
    results = clean_items(items, args, admin)

    if args.component_cleanup:
        if admin:
            run_component_cleanup(args.dry_run)
        else:
            print("\nSkipping DISM component cleanup: needs administrator.")

    after = disk_usage(args.drive)
    print_summary(results, before, after, args.dry_run)

    failures = [e for r in results for e in r.errors]
    if failures:
        print(f"\n{len(failures)}+ items could not be removed (usually in use or "
              "access denied). Details are in the log.")
        for e in failures[:25]:
            logger.info("Failure detail: %s", e)

    print_tips(args.drive)
    if args.report:
        write_report(args.report, args, before, after, results)

    logger.info("Finished in %.1fs. Removed ~%s.", time.time() - started,
                format_size(sum(r.bytes for r in results)))
    print(f"\nFinished in {time.time() - started:.1f}s.  Log: {log_path}")
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # never crash on odd characters
        except Exception:
            pass

    args = parse_args(argv)
    code = 1
    try:
        code = run(args)
    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user.")
        logger.info("Interrupted by user.")
    except Exception as exc:
        print(f"\nUnexpected error: {exc}")
        logger.exception("Unhandled application error.")
    finally:
        if args.pause:
            try:
                input("\nPress Enter to close...")
            except (EOFError, KeyboardInterrupt):
                pass
    return code


if __name__ == "__main__":
    sys.exit(main())