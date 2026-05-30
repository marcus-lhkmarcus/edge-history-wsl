# edge-history-wsl

> **TL;DR** — read your **Microsoft Edge browsing history** from inside WSL2
> (Linux), even while Edge is running. Uses a Volume Shadow Copy to get past
> Edge's exclusive lock on the history database, then queries it with SQL.

> 🤖 **AI agents:** see [AGENTS.md](AGENTS.md) for a machine-readable invocation
> guide and [llms.txt](llms.txt) for a quick index.

Edge (like all Chromium browsers) keeps an exclusive lock on its `History`
SQLite database while running, so you can't just copy it. This tool snapshots the
C: drive with **VSS** (Volume Shadow Copy), pulls the locked DB out of the
snapshot, and queries a local copy — search, filter by date, top sites, top
domains, stats, or a full dump.

```bash
python3 edge_history.py recent 50
python3 edge_history.py search "github"
python3 edge_history.py stats
```

## How it works

```
WSL2 (Linux)                              Windows (admin)
────────────                              ───────────────
edge_history.py
  │ 1. writes a VSS PowerShell script
  │ 2. runs it via win_admin.py ────────► elevated PowerShell:
  │                                          Win32_ShadowCopy.Create("C:\")
  │                                          copy History out of the snapshot
  │                                          delete the snapshot
  │ 3. copies the DB to WSL temp ◄──────── %LOCALAPPDATA%\...\Temp\...
  │ 4. queries it with sqlite3 (cached 2 min)
  ▼
prints results (table or --json)
```

VSS is needed because the live `History` DB is locked; snapshotting sidesteps the
lock without closing Edge. The shadow copy is created and deleted per refresh
(~3s), and the extracted DB is cached for 2 minutes to avoid repeated VSS calls.

## Requirements

- Windows 10/11 with WSL2, Microsoft Edge installed
- Python 3.7+ inside WSL (standard library only — no `pip install`)
- **[wsl-win-admin-bridge](https://github.com/marcus-lhkmarcus/wsl-win-admin-bridge)** —
  VSS creation needs admin rights, which this tool obtains through that helper's
  `win_admin.py`. Install it (one-time scheduled-task setup), then either:
  - place/symlink its `win_admin.py` next to `edge_history.py`, or
  - point the `WIN_ADMIN` env var at it:
    `export WIN_ADMIN=/path/to/wsl-win-admin-bridge/win_admin.py`

The Windows username (for the Edge profile path) is auto-detected; override with
`WIN_USER` if needed.

## Usage

```bash
HIST="python3 edge_history.py"

# Recent history (default 30)
$HIST recent
$HIST recent 50

# Search by keyword (URL or title); optional result limit (default 50)
$HIST search "github"
$HIST search "stackoverflow" 20

# Today's history
$HIST today

# Specific date / date range (inclusive)
$HIST date 2026-03-09
$HIST range 2026-03-01 2026-03-10

# Most visited URLs (default 20)
$HIST top
$HIST top 50

# Top domains by total visit count (default 20)
$HIST domains
$HIST domains 30

# Summary statistics
$HIST stats

# Full dump (TSV, or --json) for piping/processing
$HIST dump
$HIST dump --json
```

Global options (place **before** the subcommand):

- `--profile <name>` — Edge profile: `Default`, `Profile 1`, `Profile 2`, …
- `--json` — JSON output (supported by all query commands)

```bash
python3 edge_history.py --profile "Profile 2" --json recent 100
```

## Configuration

| Variable            | Default                          | Purpose                          |
|---------------------|----------------------------------|----------------------------------|
| `WIN_ADMIN`         | auto-located `win_admin.py`      | Path to the win-admin helper     |
| `WIN_USER`          | auto-detected                    | Windows username (Edge profile)  |
| `EDGE_HISTORY_LOG`  | `edge_history.log` next to script| Log file path                    |

## Notes

- Edge timestamps are microseconds since `1601-01-01` (the Chromium epoch);
  conversion is handled for you.
- The `History` database can be large (it holds every URL ever visited).
- A 2-minute cache of the extracted DB means rapid successive queries don't each
  trigger a fresh VSS snapshot.

## Security & privacy notes

- This reads your **complete** browsing history — treat its output as sensitive.
- VSS snapshot creation runs an **elevated** PowerShell command (via the
  win-admin helper). Only use this on a machine you control.
- The extracted DB is written to the system temp dir; delete it
  (`$TMPDIR/edge_history_vss.db`) if you don't want it lingering.

## License

MIT — see [LICENSE](LICENSE).
