# Claude Code Usage Monitor Widget

A compact, always-on-top Windows desktop widget that tracks your Claude Code
token consumption in real-time, logs historical burn rates, and recommends
the best times to work based on past usage patterns.

```
┌─────────────────────────────┐
│ ☁ Claude Usage          ⚙ ✕│
├─────────────────────────────┤
│ 5hr:  [████████░░]  78%    │
│ Week: [██████░░░░]  52%    │
│ ─────────────────────────── │
│ Burn: 2,340 tok/min         │
│ ETA:  Session ~47m          │
│ Updated 14:23:01 | 41k/wk  │
│ [📊 Show Optimisation]      │
└─────────────────────────────┘
```

---

## Requirements

- Windows 10 / 11 (also runs on macOS / Linux with minor tray limitations)
- Python 3.10+
- Claude Code installed and used at least once

---

## Installation

```bash
git clone <repo-url>
cd claude-usage-widget
pip install -r requirements.txt
python main.py
```

---

## Finding your Claude Code data path

The widget searches these locations automatically (in order):

| Priority | Path |
|----------|------|
| 1 | `%USERPROFILE%\.claude\projects` |
| 2 | `%USERPROFILE%\.config\claude\projects` |
| 3 | `%APPDATA%\Claude\projects` |
| 4 | `%APPDATA%\Claude\Code\User\globalStorage` |
| 5 | `%LOCALAPPDATA%\Claude\projects` |

Each location is searched recursively for `*.jsonl` files.

If the widget shows **"Claude Code data not found"**:

1. Open the **⚙ Settings** dialog.
2. Paste your actual data path into **Custom data path**.
3. Click **Save & Close**.

To find the path manually, open a terminal and run:

```powershell
Get-ChildItem -Path $HOME -Recurse -Filter "*.jsonl" -ErrorAction SilentlyContinue |
    Select-Object -First 5 DirectoryName
```

---

## Configuration

All settings are stored in `config.json` (same folder as `main.py`) and are
also editable via the **⚙** button on the widget.

| Key | Default | Description |
|-----|---------|-------------|
| `plan` | `"pro"` | `pro` (19k), `max5` (88k), `max20` (220k), `custom` |
| `custom_session_limit` | `null` | Override session token limit |
| `custom_weekly_limit` | `null` | Override weekly token limit (auto = session × 33.6) |
| `refresh_interval_seconds` | `10` | How often to re-read data (5–60) |
| `opacity` | `0.92` | Window transparency (0.1–1.0) |
| `always_on_top` | `true` | Keep widget above other windows |
| `alert_thresholds` | `[75,90,95]` | % levels that trigger toast notifications |
| `theme` | `"dark"` | `"dark"` or `"light"` |
| `custom_data_path` | `null` | Absolute path to your `.jsonl` folder |
| `data_retention_days` | `90` | Days of history kept in the local DB |

---

## Token limits by plan

| Plan | Session limit (5 hr) | Weekly limit (auto) |
|------|---------------------|--------------------|
| pro | 19,000 | ~639,000 |
| max5 | 88,000 | ~2,960,000 |
| max20 | 220,000 | ~7,400,000 |
| custom | configurable | configurable |

---

## Auto-start with Windows

```bash
# Enable
python main.py --autostart

# Disable
python main.py --no-autostart
```

This writes/removes an entry in
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.

---

## Compile to .exe (optional)

```bash
pip install pyinstaller
pyinstaller build.spec
# Output: dist/claude-usage-monitor.exe
```

---

## Optimisation panel

Click **📊 Show Optimisation** to expand the historical analysis panel.
Rankings appear after the widget has collected data across multiple sessions
(each slot needs ≥ 2 snapshots before it is ranked).

Data is grouped into 168 one-hour slots (7 days × 24 hours).
The widget ranks them from lowest to highest average burn rate so you know
exactly when Claude Code is under the least load.

---

## Troubleshooting

**Widget shows 0% / no data**
- Make sure Claude Code has been used and has created `.jsonl` files.
- Check the widget log at `%USERPROFILE%\.claude-usage-widget\widget.log`.
- Set `custom_data_path` in Settings to point directly at your `.jsonl` folder.

**Notifications not appearing**
- Install `plyer`: `pip install plyer`
- Windows Focus Assist / Do Not Disturb may suppress toasts.

**System tray icon missing**
- Install both `pystray` and `Pillow`: `pip install pystray Pillow`

**High CPU / memory**
- Increase `refresh_interval_seconds` (e.g. 30) in Settings.
- Use `data_retention_days: 30` to keep the SQLite DB small.

**Widget disappears off-screen after monitor change**
- Delete `config.json` and restart; the window will reappear at (100, 100).

---

## File structure

```
claude-usage-widget/
├── main.py             # GUI, tray, alert logic
├── usage_monitor.py    # JSONL reader & metric calculator
├── database.py         # SQLite store for snapshots & sessions
├── optimization.py     # Best/worst time ranker
├── config.json         # User settings
├── build.spec          # PyInstaller config
├── requirements.txt    # Python dependencies
└── usage_history.db    # Created at runtime
```

---

## Data sources

Usage data is read from Claude Code's local JSONL files.  Each line is a JSON
object with fields including `timestamp`, `input_tokens`, `output_tokens`,
`cache_creation_input_tokens`, `cache_read_input_tokens`, `cost_usd`, and
`model`.  No data is sent anywhere – everything stays on your machine.

Reference: [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor)
