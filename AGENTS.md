# AGENTS.md

Machine-readable guide for AI coding agents working with this repository.

## What this is

`edge-history-wsl` reads the **Microsoft Edge browsing history** from inside
**WSL2 (Linux)**, even while Edge is running. It uses a Windows Volume Shadow
Copy (VSS) to bypass Edge's exclusive lock on the `History` SQLite DB, then
queries a local copy. Output is human-readable tables or `--json`.

Use this when an agent needs to know what the user has been browsing — for
activity context, time tracking, research recall, or "what was that site I
visited" lookups.

## Entry point

```
edge_history.py        # CLI (Python standard library only)
```

Hard dependency: the **win-admin helper** `win_admin.py` from
https://github.com/marcusice/wsl-win-admin-bridge — VSS creation needs admin
rights, obtained through it. Auto-located next to this script / as a sibling
checkout / under $HOME, or set via the `WIN_ADMIN` env var.

## How to invoke (CLI)

Global flags `--profile` and `--json` go **before** the subcommand.

```bash
python3 edge_history.py recent [N]              # last N URLs (default 30)
python3 edge_history.py search "<query>" [N]    # match URL/title (limit default 50)
python3 edge_history.py today                   # today's history
python3 edge_history.py date <YYYY-MM-DD>       # one day
python3 edge_history.py range <YYYY-MM-DD> <YYYY-MM-DD>   # inclusive range
python3 edge_history.py top [N]                 # most-visited URLs (default 20)
python3 edge_history.py domains [N]             # top domains by visits (default 20)
python3 edge_history.py stats                   # totals + range + top 5 domains
python3 edge_history.py dump [--json]           # all URLs (TSV by default)

python3 edge_history.py --profile "Profile 2" --json recent 100
```

Exact subcommand set: `recent, search, today, date, range, top, domains, stats,
dump`. With no subcommand it defaults to `recent 30`.

JSON shape (query commands): `[{"url","title","time"(ISO|null),"visits"}]`.
`stats --json` and `domains --json` have their own shapes (see the source).

## Preconditions

- Running inside WSL2 with `/mnt/c` accessible; Edge installed on Windows.
- The win-admin helper must be installed AND its scheduled task registered (a
  one-time admin step on Windows — see that project's README). If `win_admin.py`
  is missing, `vss_copy()` prints an install hint and returns False; surface it
  to the user rather than trying to self-elevate.
- Windows username auto-detected (`cmd.exe echo %USERNAME%` / single profile dir
  under /mnt/c/Users); override with `WIN_USER`.

## Configuration (environment variables)

| Variable           | Default                           | Purpose                       |
|--------------------|-----------------------------------|-------------------------------|
| `WIN_ADMIN`        | auto-located `win_admin.py`       | Path to the win-admin helper  |
| `WIN_USER`         | auto-detected                     | Windows username (profile)    |
| `EDGE_HISTORY_LOG` | `edge_history.log` next to script | Log file path                 |

## Safety notes for agents

- Output is the user's **complete** browsing history — highly sensitive. Don't
  exfiltrate it, log it externally, or include it in shared transcripts without
  the user's explicit consent. Prefer narrow queries (`search`, `date`) over
  `dump`.
- VSS creation runs an elevated PowerShell command via the win-admin helper.
- The extracted DB lands in the system temp dir (`$TMPDIR/edge_history_vss.db`).

## Verifying a change

```bash
python3 -m py_compile edge_history.py   # syntax check (no Windows needed)
python3 edge_history.py stats           # round-trip (needs Windows + win-admin + Edge)
```
