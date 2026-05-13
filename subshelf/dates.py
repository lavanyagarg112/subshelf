from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP


VALID_CADENCE_UNITS = {"days", "months", "years"}


@dataclass(frozen=True)
class Cadence:
    unit: str
    count: int = 1

    def __post_init__(self) -> None:
        if self.unit not in VALID_CADENCE_UNITS:
            raise ValueError(f"Unsupported cadence unit: {self.unit}")
        if self.count < 1:
            raise ValueError("Cadence count must be positive")

    def display(self) -> str:
        if self.unit == "months" and self.count == 1:
            return "monthly"
        if self.unit == "years" and self.count == 1:
            return "yearly"
        return f"every {self.count} {self.unit[:-1] if self.count == 1 else self.unit}"


def parse_iso_date(value: str) -> date:
    return date.fromisoformat(value.strip())


def parse_date_input(value: str, today: date) -> date:
    text = value.strip().lower()
    if text == "today":
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)

    match = re.fullmatch(r"in\s+(\d+)\s+days?", text)
    if match:
        return today + timedelta(days=int(match.group(1)))

    return parse_iso_date(text)


def add_cadence(start: date, cadence: Cadence, periods: int = 1) -> date:
    if periods < 0:
        raise ValueError("periods must be non-negative")
    if periods == 0:
        return start
    if cadence.unit == "days":
        return start + timedelta(days=cadence.count * periods)

    if cadence.unit == "months":
        months_to_add = cadence.count * periods
    else:
        months_to_add = cadence.count * periods * 12

    month_index = start.month - 1 + months_to_add
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _period_guess(start: date, cadence: Cadence, target: date) -> int:
    if target <= start:
        return 0
    if cadence.unit == "days":
        return max(0, (target - start).days // cadence.count)

    months_between = (target.year - start.year) * 12 + target.month - start.month
    if cadence.unit == "months":
        return max(0, months_between // cadence.count)
    return max(0, months_between // (cadence.count * 12))


def next_due_date(start: date, cadence: Cadence, today: date, *, include_today: bool = True) -> date:
    """Return the first payment date on or after `today` by default.

    The spec says next renewal is strictly after today, but also says a payment
    on today's date counts as due today. The user-facing MVP uses due-today
    behavior so `/upcoming` and reminders do not skip same-day renewals.
    """

    period = _period_guess(start, cadence, today)
    candidate = add_cadence(start, cadence, period)
    while candidate < today or (not include_today and candidate <= today):
        period += 1
        candidate = add_cadence(start, cadence, period)
    return candidate


def due_dates_between(
    start: date,
    cadence: Cadence,
    window_start: date,
    window_end: date,
) -> list[date]:
    if window_end < window_start:
        return []

    first = next_due_date(start, cadence, max(start, window_start), include_today=True)
    dates: list[date] = []
    period = _period_guess(start, cadence, first)
    candidate = add_cadence(start, cadence, period)
    while candidate < first:
        period += 1
        candidate = add_cadence(start, cadence, period)

    while candidate <= window_end:
        dates.append(candidate)
        period += 1
        candidate = add_cadence(start, cadence, period)
    return dates


def decimal_money(value: str | Decimal) -> Decimal:
    return Decimal(str(value))


def format_money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def annualized_amount(amount: Decimal, cadence: Cadence) -> Decimal:
    if cadence.unit == "years":
        multiplier = Decimal(1) / Decimal(cadence.count)
    elif cadence.unit == "months":
        multiplier = Decimal(12) / Decimal(cadence.count)
    else:
        multiplier = Decimal(365) / Decimal(cadence.count)
    return amount * multiplier


def monthly_amount(amount: Decimal, cadence: Cadence) -> Decimal:
    if cadence.unit == "years":
        divisor = Decimal(cadence.count * 12)
        return amount / divisor
    if cadence.unit == "months":
        return amount / Decimal(cadence.count)
    return amount * Decimal(30) / Decimal(cadence.count)
