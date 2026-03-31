# timesheet-annoying-automation

Automates the monthly timesheet filling on [Deel](https://app.deel.com) for Irish EOR compliance.

From April 2026, all EORs in Ireland must submit timesheets monthly.
This tool uses **Playwright browser automation** to fill out the "Add Multiple Shifts" form on Deel,
so you can see exactly what's happening in the browser at every step.

**The bot fills all shifts but does NOT submit** — you review and submit manually.

## What it does

1. Calculates all working days (Mon-Fri) for a given month, excluding Irish public holidays
2. Opens Deel in a real browser window — you log in manually (SSO/MFA)
3. Navigates to Time Tracking -> Add Multiple Shifts
4. Fills in each working day with a configurable shift (default: 09:00-18:00, 1h break)
5. Takes a confirmation screenshot and prints a summary
6. **Stops and waits** — you review the filled form and submit manually in the browser

## Setup

```bash
pip install .
playwright install chromium
```

## Usage

```bash
# Preview working days for a month (no browser)
python timesheet.py --dry-run --month 2026-03

# Fill timesheets for the previous month
python timesheet.py

# Fill timesheets for a specific month
python timesheet.py --month 2026-03

# Custom shift hours
python timesheet.py --start-time 08:00 --end-time 16:30 --break-hours 0.5

# Open Deel with Playwright Inspector to discover/update selectors
python timesheet.py --explore
```

## CLI options

| Flag | Default | Description |
|------|---------|-------------|
| `--month YYYY-MM` | previous month | Target month (must be in the past) |
| `--dry-run` | off | Preview working days without launching browser |
| `--start-time HH:MM` | `09:00` | Shift start time |
| `--end-time HH:MM` | `18:00` | Shift end time |
| `--break-hours N` | `1.0` | Break duration in hours |
| `--slow-mo MS` | `500` | Slow down Playwright actions (ms) |
| `--explore` | off | Open Playwright Inspector for selector discovery |

## How it works

The script runs Playwright in **headed mode** (visible browser window) so you can watch
and verify every action. It will:

1. Launch Chromium and open Deel
2. Wait for you to log in and press Enter in the terminal
3. Navigate to the timesheet submission page
4. Fill in shifts for all working days in the target month
5. Save a screenshot for your records
6. **Stop and keep the browser open** for you to review and submit manually

No credentials are stored or handled by the script — you authenticate directly in the browser.

## Audit log

Each run appends structured JSON entries to `timesheet-audit.log` (git-ignored). This provides
a machine-readable record of what the bot did: navigation events, shifts filled, errors, and timestamps.

## Irish public holidays

The script automatically excludes all 10 Irish bank holidays:

- New Year's Day (Jan 1)
- St. Brigid's Day (1st Monday in February)
- St. Patrick's Day (Mar 17)
- Easter Monday
- May Bank Holiday (1st Monday in May)
- June Bank Holiday (1st Monday in June)
- August Bank Holiday (1st Monday in August)
- October Bank Holiday (last Monday in October)
- Christmas Day (Dec 25)
- St. Stephen's Day (Dec 26)

When a fixed-date holiday falls on a weekend, the next Monday is treated as the observed day off.

## First-time selector setup

Deel's UI may change over time. Use `--explore` mode to open Playwright Inspector
and discover the correct CSS selectors, then update the `SELECTORS` dict in `timesheet.py`.
