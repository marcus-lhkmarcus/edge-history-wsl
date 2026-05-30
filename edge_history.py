#!/usr/bin/env python3
"""
Edge History — Read Microsoft Edge browser history from WSL2.

Uses Volume Shadow Copy (VSS) to bypass Edge's exclusive file lock,
then queries the SQLite database.

Requires a helper that runs an elevated PowerShell command on Windows, to
create the VSS shadow copy. By default it looks for `win_admin.py` from the
companion project:  https://github.com/marcusice/wsl-win-admin-bridge
Point at it with the WIN_ADMIN env var if it isn't auto-detected.

Usage:
    python3 edge_history.py recent [N]              # Last N visited URLs (default 30)
    python3 edge_history.py search <query>           # Search history by keyword
    python3 edge_history.py today                    # Today's history
    python3 edge_history.py date <YYYY-MM-DD>        # History for a specific date
    python3 edge_history.py range <from> <to>        # Date range (inclusive)
    python3 edge_history.py top [N]                  # Most visited sites (default 20)
    python3 edge_history.py domains [N]              # Top domains by visit count
    python3 edge_history.py stats                    # Summary statistics
    python3 edge_history.py dump [--json]            # Full dump (all URLs, for piping)

All commands support:
    --profile <name>     Edge profile (Default, Profile 2, etc.)
    --json               Output as JSON
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse


def detect_win_user():
    """Resolve the Windows username as seen from WSL2.

    Order: WIN_USER env override -> `cmd.exe echo %USERNAME%` -> a single
    real profile dir under /mnt/c/Users.
    """
    env_user = os.environ.get("WIN_USER")
    if env_user:
        return env_user
    try:
        out = subprocess.run(
            ["cmd.exe", "/c", "echo %USERNAME%"],
            capture_output=True, text=True, timeout=10,
        )
        name = out.stdout.strip()
        if name and "%USERNAME%" not in name:
            return name
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    skip = {"Public", "Default", "Default User", "All Users", "defaultuser0"}
    candidates = [
        p.name for p in Path("/mnt/c/Users").glob("*")
        if p.is_dir() and p.name not in skip
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def locate_win_admin():
    """Find the win_admin.py helper (from wsl-win-admin-bridge).

    Order: WIN_ADMIN env override -> a copy next to this script -> a sibling
    checkout -> one under $HOME. Returns a path string, or None if not found.
    """
    env = os.environ.get("WIN_ADMIN")
    if env and Path(env).exists():
        return env
    here = Path(__file__).resolve().parent
    for cand in [
        here / "win_admin.py",
        here.parent / "wsl-win-admin-bridge" / "win_admin.py",
        Path.home() / "wsl-win-admin-bridge" / "win_admin.py",
    ]:
        if cand.exists():
            return str(cand)
    return None


WIN_USER = detect_win_user()
WIN_ADMIN = locate_win_admin()

# Log next to the script by default; override with EDGE_HISTORY_LOG.
LOG_FILE = Path(os.environ.get("EDGE_HISTORY_LOG", Path(__file__).resolve().parent / "edge_history.log"))

# Edge history location (Windows path, used in the VSS script)
EDGE_BASE = rf"C:\Users\{WIN_USER}\AppData\Local\Microsoft\Edge\User Data"
DEFAULT_PROFILE = "Default"

# Local cache for the copied DB
CACHE_DIR = Path(tempfile.gettempdir())
CACHE_DB = CACHE_DIR / "edge_history_vss.db"
WIN_TEMP_DB = rf"C:\Users\{WIN_USER}\AppData\Local\Temp\edge_history_vss.db"
WSL_TEMP_DB = Path(f"/mnt/c/Users/{WIN_USER}/AppData/Local/Temp/edge_history_vss.db")

# Chromium timestamp epoch: microseconds since 1601-01-01
CHROMIUM_EPOCH = datetime(1601, 1, 1)


def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{timestamp}] {message}"
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(log_msg + "\n")
    except Exception:
        pass


def chromium_to_datetime(ts):
    """Convert Chromium timestamp (microseconds since 1601-01-01) to datetime."""
    if not ts:
        return None
    try:
        return CHROMIUM_EPOCH + timedelta(microseconds=ts)
    except (OverflowError, OSError):
        return None


def datetime_to_chromium(dt):
    """Convert datetime to Chromium timestamp."""
    delta = dt - CHROMIUM_EPOCH
    return int(delta.total_seconds() * 1_000_000)


def format_time(dt):
    """Format datetime for display."""
    if not dt:
        return "-"
    now = datetime.now()
    if dt.date() == now.date():
        return dt.strftime("Today %H:%M")
    elif dt.date() == (now - timedelta(days=1)).date():
        return dt.strftime("Yesterday %H:%M")
    elif (now - dt).days < 7:
        return dt.strftime("%a %H:%M")
    else:
        return dt.strftime("%Y-%m-%d %H:%M")


def extract_domain(url):
    """Extract domain from URL."""
    try:
        parsed = urlparse(url)
        return parsed.netloc or url[:50]
    except Exception:
        return url[:50]


def vss_copy(profile=DEFAULT_PROFILE):
    """Copy Edge History file using VSS shadow copy via the win-admin helper."""
    if not WIN_ADMIN:
        print(
            "ERROR: win_admin.py helper not found. Install wsl-win-admin-bridge\n"
            "  (https://github.com/marcusice/wsl-win-admin-bridge) and either place\n"
            "  win_admin.py next to this script or set the WIN_ADMIN env var to its path.",
            file=sys.stderr,
        )
        return False
    if not WIN_USER:
        print(
            "ERROR: could not detect the Windows username. Set WIN_USER explicitly.",
            file=sys.stderr,
        )
        return False

    history_path = f"{EDGE_BASE}\\{profile}\\History"

    ps_script = f'''$ErrorActionPreference = "Stop"
$src = "{history_path}"
$dst = "{WIN_TEMP_DB}"
$drive = "C:\\"
$shadow = (Get-WmiObject -List Win32_ShadowCopy).Create($drive, "ClientAccessible")
$shadowObj = Get-WmiObject Win32_ShadowCopy | Where-Object {{ $_.ID -eq $shadow.ShadowID }}
$snapRoot = $shadowObj.DeviceObject + "\\"
$relPath = $src.Substring(3)
$snapFile = $snapRoot + $relPath
cmd /c copy "$snapFile" "$dst" /Y >$null
$size = (Get-Item $dst).Length
$shadowObj.Delete()
Write-Output "$size"
'''

    # Write PS1 to temp
    ps_path = CACHE_DIR / "vss_edge.ps1"
    ps_path.write_text(ps_script)
    win_ps = subprocess.check_output(["wslpath", "-w", str(ps_path)], text=True).strip()

    # Run via win-admin
    result = subprocess.run(
        [sys.executable, str(WIN_ADMIN), "run",
         f'powershell -NoProfile -ExecutionPolicy Bypass -File "{win_ps}"'],
        capture_output=True, text=True, timeout=30,
    )

    output = result.stdout.strip()
    lines = output.strip().split("\n")
    size_line = [l for l in lines if l.strip().isdigit()]

    if not size_line:
        print(f"VSS copy failed: {output}", file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        return False

    size = int(size_line[-1])
    if size == 0:
        print("VSS copy produced 0-byte file", file=sys.stderr)
        return False

    # Copy from Windows temp to WSL temp
    shutil.copy2(str(WSL_TEMP_DB), str(CACHE_DB))
    log(f"VSS copy: {size} bytes from {profile}")
    return True


def get_db(profile=DEFAULT_PROFILE, force_refresh=False):
    """Get a connection to the Edge history database."""
    # Use cache if recent (< 2 min old)
    if not force_refresh and CACHE_DB.exists():
        age = datetime.now().timestamp() - CACHE_DB.stat().st_mtime
        if age < 120:
            return sqlite3.connect(str(CACHE_DB))

    if not vss_copy(profile):
        if CACHE_DB.exists():
            print("Using stale cache...", file=sys.stderr)
            return sqlite3.connect(str(CACHE_DB))
        print("ERROR: Cannot access Edge history. Is Edge running?", file=sys.stderr)
        sys.exit(1)

    return sqlite3.connect(str(CACHE_DB))


def cmd_recent(args):
    """Show most recently visited URLs."""
    conn = get_db(args.profile)
    cur = conn.execute(
        "SELECT url, title, last_visit_time, visit_count "
        "FROM urls ORDER BY last_visit_time DESC LIMIT ?",
        (args.limit,),
    )
    rows = cur.fetchall()
    conn.close()

    if args.json:
        _print_json(rows)
        return

    for row in rows:
        dt = chromium_to_datetime(row[2])
        title = (row[1] or "")[:55]
        print(f"  {format_time(dt):<18s}  {row[3]:>4d}x  {title:<55s}  {row[0][:90]}")


def cmd_search(args):
    """Search history by keyword in URL or title."""
    conn = get_db(args.profile)
    query = f"%{args.query}%"
    cur = conn.execute(
        "SELECT url, title, last_visit_time, visit_count "
        "FROM urls WHERE url LIKE ? OR title LIKE ? "
        "ORDER BY last_visit_time DESC LIMIT ?",
        (query, query, args.limit),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        print(f"No results for: {args.query}")
        return

    if args.json:
        _print_json(rows)
        return

    print(f"Found {len(rows)} results for '{args.query}':\n")
    for row in rows:
        dt = chromium_to_datetime(row[2])
        title = (row[1] or "")[:55]
        print(f"  {format_time(dt):<18s}  {row[3]:>4d}x  {title:<55s}  {row[0][:90]}")


def cmd_date(args):
    """Show history for a specific date or today."""
    if args.date == "today":
        target = datetime.now().date()
    else:
        target = datetime.strptime(args.date, "%Y-%m-%d").date()

    start = datetime_to_chromium(datetime.combine(target, datetime.min.time()))
    end = datetime_to_chromium(datetime.combine(target, datetime.max.time()))

    conn = get_db(args.profile)
    cur = conn.execute(
        "SELECT url, title, last_visit_time, visit_count "
        "FROM urls WHERE last_visit_time BETWEEN ? AND ? "
        "ORDER BY last_visit_time DESC",
        (start, end),
    )
    rows = cur.fetchall()
    conn.close()

    if args.json:
        _print_json(rows)
        return

    print(f"History for {target} ({len(rows)} URLs):\n")
    for row in rows:
        dt = chromium_to_datetime(row[2])
        title = (row[1] or "")[:55]
        print(f"  {dt.strftime('%H:%M:%S') if dt else '-':<10s}  {row[3]:>4d}x  {title:<55s}  {row[0][:90]}")


def cmd_range(args):
    """Show history for a date range."""
    start_date = datetime.strptime(args.start, "%Y-%m-%d")
    end_date = datetime.strptime(args.end, "%Y-%m-%d")
    start = datetime_to_chromium(start_date)
    end = datetime_to_chromium(end_date.replace(hour=23, minute=59, second=59))

    conn = get_db(args.profile)
    cur = conn.execute(
        "SELECT url, title, last_visit_time, visit_count "
        "FROM urls WHERE last_visit_time BETWEEN ? AND ? "
        "ORDER BY last_visit_time DESC",
        (start, end),
    )
    rows = cur.fetchall()
    conn.close()

    if args.json:
        _print_json(rows)
        return

    print(f"History {args.start} to {args.end} ({len(rows)} URLs):\n")
    for row in rows:
        dt = chromium_to_datetime(row[2])
        title = (row[1] or "")[:55]
        print(f"  {format_time(dt):<18s}  {row[3]:>4d}x  {title:<55s}  {row[0][:90]}")


def cmd_top(args):
    """Show most visited URLs."""
    conn = get_db(args.profile)
    cur = conn.execute(
        "SELECT url, title, visit_count, last_visit_time "
        "FROM urls ORDER BY visit_count DESC LIMIT ?",
        (args.limit,),
    )
    rows = cur.fetchall()
    conn.close()

    if args.json:
        _print_json([(r[0], r[1], r[3], r[2]) for r in rows])
        return

    print(f"Top {len(rows)} most visited:\n")
    for i, row in enumerate(rows, 1):
        dt = chromium_to_datetime(row[3])
        title = (row[1] or "")[:50]
        print(f"  {i:>3d}. {row[2]:>5d}x  {title:<50s}  {row[0][:80]}")


def cmd_domains(args):
    """Show top domains by total visit count."""
    conn = get_db(args.profile)
    cur = conn.execute("SELECT url, visit_count FROM urls")
    rows = cur.fetchall()
    conn.close()

    domain_counts = {}
    for url, count in rows:
        domain = extract_domain(url)
        domain_counts[domain] = domain_counts.get(domain, 0) + count

    sorted_domains = sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)

    if args.json:
        print(json.dumps([{"domain": d, "visits": v} for d, v in sorted_domains[:args.limit]], indent=2))
        return

    print(f"Top {min(args.limit, len(sorted_domains))} domains:\n")
    for i, (domain, count) in enumerate(sorted_domains[:args.limit], 1):
        print(f"  {i:>3d}. {count:>6d}x  {domain}")


def cmd_stats(args):
    """Show history statistics."""
    conn = get_db(args.profile)

    total_urls = conn.execute("SELECT COUNT(*) FROM urls").fetchone()[0]
    total_visits = conn.execute("SELECT SUM(visit_count) FROM urls").fetchone()[0] or 0
    oldest = conn.execute("SELECT MIN(last_visit_time) FROM urls WHERE last_visit_time > 0").fetchone()[0]
    newest = conn.execute("SELECT MAX(last_visit_time) FROM urls").fetchone()[0]

    # Today's count
    today_start = datetime_to_chromium(datetime.combine(datetime.now().date(), datetime.min.time()))
    today_count = conn.execute(
        "SELECT COUNT(*) FROM urls WHERE last_visit_time >= ?", (today_start,)
    ).fetchone()[0]

    # Top 5 domains
    cur = conn.execute("SELECT url, visit_count FROM urls")
    domain_counts = {}
    for url, count in cur:
        domain = extract_domain(url)
        domain_counts[domain] = domain_counts.get(domain, 0) + count
    top5 = sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    conn.close()

    oldest_dt = chromium_to_datetime(oldest)
    newest_dt = chromium_to_datetime(newest)

    if args.json:
        print(json.dumps({
            "total_urls": total_urls, "total_visits": total_visits,
            "today_urls": today_count,
            "oldest": oldest_dt.isoformat() if oldest_dt else None,
            "newest": newest_dt.isoformat() if newest_dt else None,
            "top_domains": [{"domain": d, "visits": v} for d, v in top5],
        }, indent=2))
        return

    print(f"  Total URLs:     {total_urls:,}")
    print(f"  Total visits:   {total_visits:,}")
    print(f"  Today's URLs:   {today_count}")
    print(f"  History range:  {oldest_dt.strftime('%Y-%m-%d') if oldest_dt else '?'} → {newest_dt.strftime('%Y-%m-%d') if newest_dt else '?'}")
    print(f"\n  Top domains:")
    for d, v in top5:
        print(f"    {v:>6d}x  {d}")


def cmd_dump(args):
    """Dump all URLs."""
    conn = get_db(args.profile)
    cur = conn.execute(
        "SELECT url, title, last_visit_time, visit_count "
        "FROM urls ORDER BY last_visit_time DESC"
    )
    rows = cur.fetchall()
    conn.close()

    if args.json:
        _print_json(rows)
    else:
        for row in rows:
            dt = chromium_to_datetime(row[2])
            ts = dt.isoformat() if dt else ""
            print(f"{ts}\t{row[3]}\t{row[1] or ''}\t{row[0]}")


def _print_json(rows):
    """Print rows as JSON array."""
    items = []
    for row in rows:
        dt = chromium_to_datetime(row[2])
        items.append({
            "url": row[0],
            "title": row[1] or "",
            "time": dt.isoformat() if dt else None,
            "visits": row[3] if len(row) > 3 else None,
        })
    print(json.dumps(items, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="Read Microsoft Edge browser history")
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help="Edge profile name")
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("recent", help="Recent history")
    p.add_argument("limit", nargs="?", type=int, default=30)

    p = sub.add_parser("search", help="Search history")
    p.add_argument("query", help="Search term")
    p.add_argument("limit", nargs="?", type=int, default=50)

    p = sub.add_parser("today", help="Today's history")

    p = sub.add_parser("date", help="History for a date")
    p.add_argument("date", help="YYYY-MM-DD")

    p = sub.add_parser("range", help="History for date range")
    p.add_argument("start", help="Start date YYYY-MM-DD")
    p.add_argument("end", help="End date YYYY-MM-DD")

    p = sub.add_parser("top", help="Most visited URLs")
    p.add_argument("limit", nargs="?", type=int, default=20)

    p = sub.add_parser("domains", help="Top domains")
    p.add_argument("limit", nargs="?", type=int, default=20)

    p = sub.add_parser("stats", help="History statistics")

    p = sub.add_parser("dump", help="Dump all history")

    args = parser.parse_args()

    if not args.command:
        args.command = "recent"
        args.limit = 30

    commands = {
        "recent": cmd_recent,
        "search": cmd_search,
        "today": lambda a: cmd_date(type(a)(**{**vars(a), "date": "today"})),
        "date": cmd_date,
        "range": cmd_range,
        "top": cmd_top,
        "domains": cmd_domains,
        "stats": cmd_stats,
        "dump": cmd_dump,
    }

    func = commands.get(args.command)
    if func:
        func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
