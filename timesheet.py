#!/usr/bin/env python3
"""
Deel timesheet automation for Irish EOR compliance.
 
Submits monthly timesheets on Deel using Playwright browser automation.
Each working day is logged as a 9-hour shift (09:00-18:00) with a 1-hour lunch break.
 
Usage:
    python timesheet.py --dry-run --month 2026-04   # preview working days
    python timesheet.py --month 2026-04              # submit for April 2026
    python timesheet.py                              # submit for previous month
    python timesheet.py --explore                    # open Playwright Inspector
"""
 
from __future__ import annotations
 
import argparse
import calendar
import datetime
import sys
from dataclasses import dataclass
 
from playwright.sync_api import Page, sync_playwright, TimeoutError as PlaywrightTimeoutError
 
# ---------------------------------------------------------------------------
# Selectors — update these if Deel changes their UI.
# Use --explore mode to discover selectors with Playwright Inspector.
# ---------------------------------------------------------------------------
SELECTORS = {
    # Navigation
    "workspace_icon": 'a[href*="workspace"], [data-testid="workspace"]',
    "time_tracking_tab": 'text="Time Tracking"',
    # Add multiple shifts
    "add_multiple_shifts_btn": 'text="Add Multiple Shifts", text="Add multiple shifts"',
    # Shift form fields — these are placeholders, discover via --explore
    "date_picker": '[data-testid="date-picker"]',
    "start_time": '[data-testid="start-time"]',
    "end_time": '[data-testid="end-time"]',
    "break_duration": '[data-testid="break-duration"]',
    # Actions
    "submit_btn": 'text="Submit"',
    "confirm_btn": 'text="Confirm"',
}
 
DEEL_URL = "https://app.deel.com"
SHIFT_START = "09:00"
SHIFT_END = "18:00"
BREAK_HOURS = 1
NET_HOURS_PER_DAY = 8
 
 
# ---------------------------------------------------------------------------
# Easter calculation (Anonymous Gregorian algorithm)
# ---------------------------------------------------------------------------
def easter_date(year: int) -> datetime.date:
    """Calculate Easter Sunday for a given year using the Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return datetime.date(year, month, day + 1)
 
 
# ---------------------------------------------------------------------------
# Irish public holidays
# ---------------------------------------------------------------------------
def _first_monday(year: int, month: int) -> datetime.date:
    """Return the first Monday of a given month."""
    cal = calendar.Calendar(firstweekday=calendar.MONDAY)
    for day in cal.itermonthdates(year, month):
        if day.month == month and day.weekday() == calendar.MONDAY:
            return day
    raise ValueError(f"No Monday found in {year}-{month}")
 
 
def _last_monday(year: int, month: int) -> datetime.date:
    """Return the last Monday of a given month."""
    cal = calendar.Calendar(firstweekday=calendar.MONDAY)
    last = None
    for day in cal.itermonthdates(year, month):
        if day.month == month and day.weekday() == calendar.MONDAY:
            last = day
    if last is None:
        raise ValueError(f"No Monday found in {year}-{month}")
    return last
 
 
def _observe(date: datetime.date) -> datetime.date:
    """If a fixed-date holiday falls on a weekend, return the next Monday."""
    if date.weekday() == 5:  # Saturday
        return date + datetime.timedelta(days=2)
    if date.weekday() == 6:  # Sunday
        return date + datetime.timedelta(days=1)
    return date
 
 
def get_irish_public_holidays(year: int) -> set[datetime.date]:
    """Return all Irish bank holidays for a given year."""
    easter_sunday = easter_date(year)
    easter_monday = easter_sunday + datetime.timedelta(days=1)
 
    holidays = {
        _observe(datetime.date(year, 1, 1)),       # New Year's Day
        _first_monday(year, 2),                     # St. Brigid's Day
        _observe(datetime.date(year, 3, 17)),       # St. Patrick's Day
        easter_monday,                              # Easter Monday
        _first_monday(year, 5),                     # May Bank Holiday
        _first_monday(year, 6),                     # June Bank Holiday
        _first_monday(year, 8),                     # August Bank Holiday
        _last_monday(year, 10),                     # October Bank Holiday
        _observe(datetime.date(year, 12, 25)),      # Christmas Day
        _observe(datetime.date(year, 12, 26)),      # St. Stephen's Day
    }
 
    return holidays
 
 
# ---------------------------------------------------------------------------
# Working days calculator
# ---------------------------------------------------------------------------
def get_working_days(year: int, month: int) -> list[datetime.date]:
    """Return all working days (Mon-Fri, excluding Irish holidays) for a month."""
    holidays = get_irish_public_holidays(year)
    days_in_month = calendar.monthrange(year, month)[1]
 
    working_days = []
    for day in range(1, days_in_month + 1):
        date = datetime.date(year, month, day)
        if date.weekday() < 5 and date not in holidays:  # Mon-Fri, not a holiday
            working_days.append(date)
 
    return working_days
 
 
# ---------------------------------------------------------------------------
# Playwright automation
# ---------------------------------------------------------------------------
@dataclass
class AutomationConfig:
    working_days: list[datetime.date]
    month_label: str
    slow_mo: int
    explore: bool
 
 
def run_automation(config: AutomationConfig) -> None:
    """Run the Deel timesheet submission automation."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=config.slow_mo)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
 
        try:
            if config.explore:
                _explore_mode(page)
            else:
                _submit_timesheets(page, config)
        except PlaywrightTimeoutError as e:
            _screenshot_error(page, config.month_label)
            print(f"\nError: Timed out waiting for element: {e}", file=sys.stderr)
            print("Use --explore mode to verify selectors.", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            _screenshot_error(page, config.month_label)
            print(f"\nUnexpected error: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            browser.close()
 
 
def _explore_mode(page: Page) -> None:
    """Open Deel with Playwright Inspector for selector discovery."""
    print("Opening Deel in explore mode...")
    print("Use the Playwright Inspector to find selectors.")
    print("Update the SELECTORS dict in timesheet.py with what you find.\n")
    page.goto(DEEL_URL)
    page.pause()
 
 
def _submit_timesheets(page: Page, config: AutomationConfig) -> None:
    """Navigate Deel and submit timesheets for all working days."""
    # Step 1: Open Deel and wait for manual login
    print(f"Opening Deel at {DEEL_URL}")
    page.goto(DEEL_URL)
    input("\n>>> Log in to Deel in the browser, then press Enter here to continue...")
 
    # Step 2: Navigate to Time Tracking
    print("Navigating to Time Tracking...")
    page.click(SELECTORS["time_tracking_tab"], timeout=10000)
    page.wait_for_load_state("networkidle")
 
    # Step 3: Click Add Multiple Shifts
    print("Opening Add Multiple Shifts form...")
    page.click(SELECTORS["add_multiple_shifts_btn"], timeout=10000)
    page.wait_for_load_state("networkidle")
 
    # Step 4: Fill in shifts for each working day
    print(f"Filling in {len(config.working_days)} working days...")
    for i, day in enumerate(config.working_days, 1):
        print(f"  [{i}/{len(config.working_days)}] {day.strftime('%a %d %b %Y')}")
        _fill_shift(page, day)
 
    # Step 5: Screenshot before submit
    screenshot_path = f"timesheet-{config.month_label}-confirmation.png"
    page.screenshot(path=screenshot_path)
    print(f"\nScreenshot saved: {screenshot_path}")
 
    # Step 6: Confirm submission
    confirm = input(">>> Review the browser. Press Enter to submit, or type 'abort' to cancel: ")
    if confirm.strip().lower() == "abort":
        print("Aborted. No timesheets submitted.")
        return
 
    page.click(SELECTORS["submit_btn"], timeout=10000)
    print(f"Timesheets submitted for {config.month_label}!")
 
 
def _fill_shift(page: Page, day: datetime.date) -> None:
    """Fill in a single shift entry.
 
    NOTE: This is a skeleton — the exact form interaction depends on Deel's UI.
    Use --explore mode to discover the actual form structure and update this function.
    """
    # TODO: Implement based on actual Deel UI discovered via --explore mode.
    #
    # Expected flow:
    #   1. Select/click the date for this day
    #   2. Set start time to 09:00
    #   3. Set end time to 18:00
    #   4. Set break duration to 1 hour
    #   5. Confirm/add the entry
    #
    # Example (placeholder):
    #   page.fill(SELECTORS["date_picker"], day.strftime("%Y-%m-%d"))
    #   page.fill(SELECTORS["start_time"], SHIFT_START)
    #   page.fill(SELECTORS["end_time"], SHIFT_END)
    #   page.fill(SELECTORS["break_duration"], str(BREAK_HOURS))
    pass
 
 
def _screenshot_error(page: Page, month_label: str) -> None:
    """Take a screenshot on error for debugging."""
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = f"timesheet-error-{month_label}-{timestamp}.png"
    try:
        page.screenshot(path=path)
        print(f"Error screenshot saved: {path}", file=sys.stderr)
    except Exception:
        pass
 
 
# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_month(value: str) -> tuple[int, int]:
    """Parse a YYYY-MM string into (year, month)."""
    try:
        parts = value.split("-")
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        raise argparse.ArgumentTypeError(f"Invalid month format: {value!r}. Use YYYY-MM.")
 
 
def previous_month() -> tuple[int, int]:
    """Return (year, month) for the previous month."""
    today = datetime.date.today()
    if today.month == 1:
        return today.year - 1, 12
    return today.year, today.month - 1
 
 
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Submit Irish EOR timesheets on Deel.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
        "  python timesheet.py --dry-run --month 2026-04\n"
        "  python timesheet.py --month 2026-04\n"
        "  python timesheet.py --explore\n",
    )
    parser.add_argument(
        "--month",
        type=parse_month,
        default=None,
        help="Month to submit timesheets for (YYYY-MM). Defaults to previous month.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print working days and total hours without launching a browser.",
    )
    parser.add_argument(
        "--slow-mo",
        type=int,
        default=500,
        help="Milliseconds to slow down Playwright actions (default: 500).",
    )
    parser.add_argument(
        "--explore",
        action="store_true",
        help="Open Deel with Playwright Inspector for selector discovery.",
    )
 
    args = parser.parse_args()
 
    if args.explore:
        config = AutomationConfig(
            working_days=[],
            month_label="explore",
            slow_mo=args.slow_mo,
            explore=True,
        )
        run_automation(config)
        return
 
    year, month = args.month if args.month else previous_month()
    month_label = f"{year}-{month:02d}"
    working_days = get_working_days(year, month)
    total_hours = len(working_days) * NET_HOURS_PER_DAY
 
    print(f"Month: {month_label}")
    print(f"Working days: {len(working_days)}")
    print(f"Total hours: {total_hours}h ({SHIFT_START}-{SHIFT_END}, {BREAK_HOURS}h break)\n")
 
    holidays = get_irish_public_holidays(year)
    month_holidays = sorted(h for h in holidays if h.month == month)
    if month_holidays:
        print("Public holidays this month:")
        for h in month_holidays:
            print(f"  {h.strftime('%a %d %b %Y')}")
        print()
 
    for day in working_days:
        print(f"  {day.strftime('%a %d %b %Y')}")
 
    if args.dry_run:
        return
 
    print()
    proceed = input(">>> Press Enter to start browser automation, or type 'abort' to cancel: ")
    if proceed.strip().lower() == "abort":
        print("Aborted.")
        return
 
    config = AutomationConfig(
        working_days=working_days,
        month_label=month_label,
        slow_mo=args.slow_mo,
        explore=False,
    )
    run_automation(config)
 
 
if __name__ == "__main__":
    main()
