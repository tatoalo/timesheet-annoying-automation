#!/usr/bin/env python3
"""
Deel timesheet automation for Irish EOR compliance.

Fills monthly timesheets on Deel using Playwright browser automation.
Each working day is logged as a configurable shift (default: 09:00-18:00, 1h break).
The bot fills all shifts and then stops for manual review and submission.

Usage:
    python timesheet.py --dry-run --month 2026-04         # preview working days
    python timesheet.py --month 2026-04                    # fill shifts for April 2026
    python timesheet.py                                    # fill shifts for previous month
    python timesheet.py --explore                          # open Playwright Inspector
    python timesheet.py --start-time 08:00 --end-time 17:00 --break-hours 0.5
"""

from __future__ import annotations

import argparse
import calendar
import datetime
import functools
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Page, sync_playwright, TimeoutError as PlaywrightTimeoutError


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = Path(__file__).parent
LOG_FILE = LOG_DIR / "timesheet-audit.log"

logger = logging.getLogger("timesheet")
logger.setLevel(logging.DEBUG)

# Console handler: INFO level, concise format
_console = logging.StreamHandler(sys.stderr)
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_console)

# File handler: DEBUG level, JSON-lines for audit trail
_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_file_handler)


def _log_json(event: str, **data: object) -> None:
    """Write a structured JSON log entry to the audit file."""
    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "event": event,
        **data,
    }
    logger.debug(json.dumps(entry))


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------
def retry(max_attempts: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """Retry decorator with exponential backoff for Playwright interactions."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            current_delay = delay
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except (PlaywrightTimeoutError, Exception) as e:
                    last_exception = e
                    if attempt < max_attempts:
                        _log_json(
                            "retry",
                            function=func.__name__,
                            attempt=attempt,
                            max_attempts=max_attempts,
                            error=str(e),
                            delay=current_delay,
                        )
                        logger.info(
                            f"  Retry {attempt}/{max_attempts} for {func.__name__}: {e}"
                        )
                        time.sleep(current_delay)
                        current_delay *= backoff
            raise last_exception
        return wrapper
    return decorator


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
    # Shift form fields
    # TODO: Discover actual selectors via --explore mode. These are best-effort placeholders.
    "date_picker": '[data-testid="date-picker"], input[name*="date"], input[placeholder*="date" i]',
    "start_time": '[data-testid="start-time"], input[name*="start"], input[placeholder*="start" i]',
    "end_time": '[data-testid="end-time"], input[name*="end"], input[placeholder*="end" i]',
    "break_duration": '[data-testid="break-duration"], input[name*="break"], input[placeholder*="break" i]',
    "add_shift_btn": 'text="Add Shift", text="Add shift", button:has-text("Add")',
    # Actions
    "submit_btn": 'text="Submit"',
    "confirm_btn": 'text="Confirm"',
}

DEEL_URL = "https://app.deel.com"

# Shift defaults (overridable via CLI)
DEFAULT_SHIFT_START = "09:00"
DEFAULT_SHIFT_END = "18:00"
DEFAULT_BREAK_HOURS = 1.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _calculate_net_hours(start: str, end: str, break_hours: float) -> float:
    """Calculate net working hours from shift start/end times and break."""
    start_h, start_m = map(int, start.split(":"))
    end_h, end_m = map(int, end.split(":"))
    total_minutes = (end_h * 60 + end_m) - (start_h * 60 + start_m)
    return (total_minutes / 60) - break_hours


def _validate_time(value: str) -> str:
    """Validate HH:MM time format for argparse."""
    try:
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        return f"{h:02d}:{m:02d}"
    except (ValueError, IndexError):
        raise argparse.ArgumentTypeError(f"Invalid time format: {value!r}. Use HH:MM (e.g., 09:00).")


@retry(max_attempts=3, delay=0.5)
def _click_element(page: Page, selector_key: str, timeout: int = 10000) -> None:
    """Click an element using SELECTORS[selector_key], trying each comma-separated selector."""
    selectors = [s.strip() for s in SELECTORS[selector_key].split(",")]
    last_error = None
    for selector in selectors:
        try:
            page.click(selector, timeout=timeout // len(selectors))
            _log_json("click", selector_key=selector_key, selector=selector, status="ok")
            return
        except PlaywrightTimeoutError as e:
            last_error = e
            continue
    _log_json("click_failed", selector_key=selector_key, selectors=selectors)
    raise last_error


@retry(max_attempts=3, delay=0.5)
def _fill_element(page: Page, selector_key: str, value: str, timeout: int = 10000) -> None:
    """Fill an element using SELECTORS[selector_key], trying each comma-separated selector."""
    selectors = [s.strip() for s in SELECTORS[selector_key].split(",")]
    last_error = None
    for selector in selectors:
        try:
            page.fill(selector, value, timeout=timeout // len(selectors))
            _log_json("fill", selector_key=selector_key, selector=selector, value=value, status="ok")
            return
        except PlaywrightTimeoutError as e:
            last_error = e
            continue
    _log_json("fill_failed", selector_key=selector_key, selectors=selectors, value=value)
    raise last_error


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
    shift_start: str = DEFAULT_SHIFT_START
    shift_end: str = DEFAULT_SHIFT_END
    break_hours: float = DEFAULT_BREAK_HOURS


def run_automation(config: AutomationConfig) -> None:
    """Run the Deel timesheet filling automation."""
    _log_json("browser_launch", slow_mo=config.slow_mo, explore=config.explore)

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
            _log_json("error_timeout", error=str(e))
            logger.error(f"Timed out waiting for element: {e}")
            logger.error("Use --explore mode to verify selectors.")
            sys.exit(1)
        except Exception as e:
            _screenshot_error(page, config.month_label)
            _log_json("error_unexpected", error=str(e), type=type(e).__name__)
            logger.error(f"Unexpected error: {e}")
            sys.exit(1)
        finally:
            _log_json("browser_close")
            browser.close()


def _explore_mode(page: Page) -> None:
    """Open Deel with Playwright Inspector for selector discovery."""
    logger.info("Opening Deel in explore mode...")
    logger.info("Use the Playwright Inspector to find selectors.")
    logger.info("Update the SELECTORS dict in timesheet.py with what you find.\n")
    page.goto(DEEL_URL)
    page.pause()


def _submit_timesheets(page: Page, config: AutomationConfig) -> None:
    """Navigate Deel and fill timesheets for all working days.

    After filling, the browser stays open for the user to review and
    submit manually. The bot does NOT click submit.
    """
    _log_json(
        "automation_start",
        month=config.month_label,
        working_days=len(config.working_days),
        shift=f"{config.shift_start}-{config.shift_end}",
        break_hours=config.break_hours,
    )

    # Step 1: Open Deel and wait for manual login
    logger.info(f"Opening Deel at {DEEL_URL}")
    page.goto(DEEL_URL)
    input("\n>>> Log in to Deel in the browser, then press Enter here to continue...")
    _log_json("login_complete")

    # Step 2: Navigate to Time Tracking
    logger.info("Navigating to Time Tracking...")
    _click_element(page, "time_tracking_tab")
    page.wait_for_load_state("networkidle")
    _log_json("navigated", target="time_tracking")

    # Step 3: Click Add Multiple Shifts
    logger.info("Opening Add Multiple Shifts form...")
    _click_element(page, "add_multiple_shifts_btn")
    page.wait_for_load_state("networkidle")
    _log_json("navigated", target="add_multiple_shifts")

    # Step 4: Fill in shifts for each working day
    logger.info(f"Filling in {len(config.working_days)} working days...")
    filled = 0
    errors = []
    for i, day in enumerate(config.working_days, 1):
        logger.info(f"  [{i}/{len(config.working_days)}] {day.strftime('%a %d %b %Y')}")
        try:
            _fill_shift(page, day, config)
            filled += 1
        except Exception as e:
            error_msg = f"Failed to fill shift for {day}: {e}"
            logger.warning(error_msg)
            errors.append(error_msg)
            _log_json("fill_shift_error", date=day.isoformat(), error=str(e))

    # Step 5: Screenshot the filled form
    screenshot_path = f"timesheet-{config.month_label}-filled.png"
    page.screenshot(path=screenshot_path)
    logger.info(f"Screenshot saved: {screenshot_path}")
    _log_json("screenshot", path=screenshot_path)

    # Step 6: Summary and handoff
    net_hours = _calculate_net_hours(config.shift_start, config.shift_end, config.break_hours)

    if errors:
        logger.warning(f"{len(errors)} shift(s) failed to fill:")
        for err in errors:
            logger.warning(f"  - {err}")

    _log_json(
        "automation_fill_complete",
        filled=filled,
        total=len(config.working_days),
        errors=len(errors),
    )

    # Step 7: Hand off to user — DO NOT submit programmatically
    print("\n" + "=" * 60)
    print("  ALL SHIFTS FILLED -- REVIEW AND SUBMIT MANUALLY")
    print("=" * 60)
    print(f"  Month:       {config.month_label}")
    print(f"  Shifts:      {filled}/{len(config.working_days)}")
    print(f"  Hours/day:   {net_hours:.1f}h ({config.shift_start}-{config.shift_end}, {config.break_hours}h break)")
    print(f"  Total hours: {filled * net_hours:.1f}h")
    print(f"  Screenshot:  {screenshot_path}")
    if errors:
        print(f"  WARNINGS:    {len(errors)} shift(s) failed (see above)")
    print()
    print("  Please review the filled form in the browser and submit manually.")
    print("  Press Enter here when you are done to close the browser.")
    print("=" * 60)
    input("\n>>> Press Enter to close the browser...")
    _log_json("user_dismissed_browser")


@retry(max_attempts=2, delay=1.0)
def _fill_shift(page: Page, day: datetime.date, config: AutomationConfig) -> None:
    """Fill in a single shift entry for the given day.

    NOTE: The selectors used here are best-effort placeholders.
    Use --explore mode to discover the actual form structure and update
    the SELECTORS dict accordingly.

    TODO: After running --explore on the live Deel UI, update:
      - SELECTORS["date_picker"] with the actual date input selector
      - SELECTORS["start_time"] with the actual start time input selector
      - SELECTORS["end_time"] with the actual end time input selector
      - SELECTORS["break_duration"] with the actual break input selector
      - SELECTORS["add_shift_btn"] with the actual "add/confirm row" button selector
    """
    _log_json("fill_shift_start", date=day.isoformat())

    # Step 1: Fill the date
    # TODO: Deel's date picker may require clicking a calendar widget rather than
    # typing directly. Adjust after --explore verification.
    date_str = day.strftime("%Y-%m-%d")  # TODO: Verify Deel's expected date format
    _fill_element(page, "date_picker", date_str)

    # Step 2: Fill start time
    _fill_element(page, "start_time", config.shift_start)

    # Step 3: Fill end time
    _fill_element(page, "end_time", config.shift_end)

    # Step 4: Fill break duration
    # TODO: Deel may expect break as "1:00", "60", or "1" — verify via --explore
    break_str = str(config.break_hours)
    _fill_element(page, "break_duration", break_str)

    # Step 5: Confirm/add the shift entry
    # TODO: There may or may not be an explicit "Add" button per row.
    # Some UIs auto-add rows. Verify via --explore.
    try:
        _click_element(page, "add_shift_btn", timeout=3000)
    except (PlaywrightTimeoutError, Exception):
        logger.debug("No explicit add-shift button found; row may auto-save.")

    _log_json("fill_shift_done", date=day.isoformat())


def _screenshot_error(page: Page, month_label: str) -> None:
    """Take a screenshot on error for debugging."""
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = f"timesheet-error-{month_label}-{timestamp}.png"
    try:
        page.screenshot(path=path)
        logger.error(f"Error screenshot saved: {path}")
        _log_json("error_screenshot", path=path)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_month(value: str) -> tuple[int, int]:
    """Parse a YYYY-MM string into (year, month) with validation."""
    try:
        parts = value.split("-")
        if len(parts) != 2:
            raise ValueError
        year, month = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        raise argparse.ArgumentTypeError(f"Invalid month format: {value!r}. Use YYYY-MM.")

    if month < 1 or month > 12:
        raise argparse.ArgumentTypeError(f"Invalid month: {month}. Must be 1-12.")
    if year < 2020 or year > 2100:
        raise argparse.ArgumentTypeError(f"Invalid year: {year}. Must be 2020-2100.")

    return year, month


def previous_month() -> tuple[int, int]:
    """Return (year, month) for the previous month."""
    today = datetime.date.today()
    if today.month == 1:
        return today.year - 1, 12
    return today.year, today.month - 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fill Irish EOR timesheets on Deel (manual submission).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
        "  python timesheet.py --dry-run --month 2026-04\n"
        "  python timesheet.py --month 2026-03\n"
        "  python timesheet.py --explore\n"
        "  python timesheet.py --start-time 08:00 --end-time 16:30 --break-hours 0.5\n",
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
    parser.add_argument(
        "--start-time",
        type=_validate_time,
        default=DEFAULT_SHIFT_START,
        help=f"Shift start time in HH:MM format (default: {DEFAULT_SHIFT_START}).",
    )
    parser.add_argument(
        "--end-time",
        type=_validate_time,
        default=DEFAULT_SHIFT_END,
        help=f"Shift end time in HH:MM format (default: {DEFAULT_SHIFT_END}).",
    )
    parser.add_argument(
        "--break-hours",
        type=float,
        default=DEFAULT_BREAK_HOURS,
        help=f"Break duration in hours (default: {DEFAULT_BREAK_HOURS}).",
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

    # Validate shift times produce positive net hours
    net_hours = _calculate_net_hours(args.start_time, args.end_time, args.break_hours)
    if net_hours <= 0:
        parser.error(
            f"Shift produces {net_hours:.1f} net hours. "
            f"End time ({args.end_time}) must be after start time ({args.start_time}) "
            f"with room for {args.break_hours}h break."
        )

    year, month = args.month if args.month else previous_month()
    month_label = f"{year}-{month:02d}"

    # Reject future or incomplete months
    today = datetime.date.today()
    target_last_day = datetime.date(year, month, calendar.monthrange(year, month)[1])
    if target_last_day > today:
        print(
            f"Error: Cannot submit timesheets for a future or incomplete month ({month_label}).",
            file=sys.stderr,
        )
        print(f"The month must have ended before you can submit. Today is {today}.", file=sys.stderr)
        sys.exit(1)

    working_days = get_working_days(year, month)
    total_hours = len(working_days) * net_hours

    _log_json(
        "run_start",
        month=month_label,
        working_days=len(working_days),
        total_hours=total_hours,
        dry_run=args.dry_run,
        shift_start=args.start_time,
        shift_end=args.end_time,
        break_hours=args.break_hours,
    )

    print(f"Month: {month_label}")
    print(f"Working days: {len(working_days)}")
    print(f"Total hours: {total_hours:.1f}h ({args.start_time}-{args.end_time}, {args.break_hours}h break)\n")

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
        shift_start=args.start_time,
        shift_end=args.end_time,
        break_hours=args.break_hours,
    )
    run_automation(config)


if __name__ == "__main__":
    main()
