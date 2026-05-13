from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .dates import Cadence, parse_date_input
from .services import normalize_amount, normalize_currency


ACTIVE_TEMPLATE = "/add Spotify 10.98 SGD monthly from 2026-01-10"
TRIAL_TEMPLATE = "/trial Canva ends in 7 days then 19.99 SGD monthly"
INTERESTED_TEMPLATE = "/interested Cursor 20 USD monthly"


@dataclass(frozen=True)
class QuickActive:
    name: str
    amount: str
    currency: str
    cadence: Cadence
    start_date: date


@dataclass(frozen=True)
class QuickTrial:
    name: str
    amount: str
    currency: str
    cadence: Cadence
    trial_end_date: date


@dataclass(frozen=True)
class QuickInterested:
    name: str
    amount: str
    currency: str
    cadence: Cadence


class QuickAddError(ValueError):
    pass


def quickadd_help_text() -> str:
    return "\n".join(
        [
            "Quick add templates",
            "",
            "Active subscription",
            ACTIVE_TEMPLATE,
            "Required word: from before the start date.",
            "",
            "Trial",
            TRIAL_TEMPLATE,
            "Required words: ends before the trial end date, then before the paid plan.",
            "",
            "Watchlist",
            INTERESTED_TEMPLATE,
            "No required connector words.",
            "",
            "Billing schedule means how often it renews.",
            "Use monthly, yearly, every 6 months, or every 14 days.",
            "Dates can be today, tomorrow, in 7 days, or YYYY-MM-DD.",
            "The older | separator format still works if you prefer it.",
        ]
    )


def parse_active(text: str, today: date) -> QuickActive:
    if "|" in text:
        return _parse_active_parts(text, today)

    match = re.fullmatch(
        rf"(?P<name>.+?)\s+{_MONEY_RE}\s+(?P<cadence>{_CADENCE_RE})\s+from\s+(?P<date>.+)",
        text.strip(),
        re.IGNORECASE,
    )
    if not match:
        raise QuickAddError(_active_error(text))
    amount, currency = _money_from_match(match)
    return QuickActive(
        name=_name(match.group("name")),
        amount=amount,
        currency=currency,
        cadence=parse_cadence(match.group("cadence")),
        start_date=parse_date_input(match.group("date"), today),
    )


def parse_trial(text: str, today: date) -> QuickTrial:
    if "|" in text:
        return _parse_trial_parts(text, today)

    match = re.fullmatch(
        rf"(?P<name>.+?)\s+ends\s+(?P<date>.+?)\s+then\s+{_MONEY_RE}\s+(?P<cadence>{_CADENCE_RE})",
        text.strip(),
        re.IGNORECASE,
    )
    if not match:
        raise QuickAddError(_trial_error(text))
    amount, currency = _money_from_match(match)
    return QuickTrial(
        name=_name(match.group("name")),
        amount=amount,
        currency=currency,
        cadence=parse_cadence(match.group("cadence")),
        trial_end_date=parse_date_input(match.group("date"), today),
    )


def parse_interested(text: str, today: date) -> QuickInterested:
    del today
    if "|" in text:
        return _parse_interested_parts(text)

    match = re.fullmatch(
        rf"(?P<name>.+?)\s+{_MONEY_RE}\s+(?P<cadence>{_CADENCE_RE})",
        text.strip(),
        re.IGNORECASE,
    )
    if not match:
        raise QuickAddError(f"Use this template: {INTERESTED_TEMPLATE}")
    amount, currency = _money_from_match(match)
    return QuickInterested(
        name=_name(match.group("name")),
        amount=amount,
        currency=currency,
        cadence=parse_cadence(match.group("cadence")),
    )


def parse_cadence(text: str) -> Cadence:
    value = text.strip().lower()
    if value == "monthly":
        return Cadence("months", 1)
    if value == "yearly":
        return Cadence("years", 1)

    match = re.fullmatch(r"(?:every\s+)?(\d+)\s+(days?|months?|years?)", value)
    if not match:
        raise QuickAddError("Billing schedule must be monthly, yearly, every X months, or every X days.")

    count = int(match.group(1))
    unit = match.group(2)
    if unit.startswith("day"):
        return Cadence("days", count)
    if unit.startswith("month"):
        return Cadence("months", count)
    return Cadence("years", count)


def _parts(text: str, expected: int) -> list[str]:
    parts = [part.strip() for part in text.split("|")]
    if len(parts) != expected or any(not part for part in parts):
        raise QuickAddError("Use the exact template with | separators.")
    return parts


_AMOUNT_RE = r"\d+(?:\.\d+)?"
_CURRENCY_RE = r"[A-Za-z]{3,8}"
_CADENCE_RE = r"(?:monthly|yearly|(?:every\s+)?\d+\s+(?:days?|months?|years?))"
_MONEY_RE = (
    rf"(?:(?P<amount>{_AMOUNT_RE})\s+(?P<currency>{_CURRENCY_RE})|"
    rf"(?P<currency_first>{_CURRENCY_RE})\s+(?P<amount_second>{_AMOUNT_RE}))"
)


def _parse_active_parts(text: str, today: date) -> QuickActive:
    parts = _parts(text, 4)
    start_text = _strip_prefix(parts[3], "from")
    amount, currency = _parse_money(parts[1])
    return QuickActive(
        name=_name(parts[0]),
        amount=amount,
        currency=currency,
        cadence=parse_cadence(parts[2]),
        start_date=parse_date_input(start_text, today),
    )


def _parse_trial_parts(text: str, today: date) -> QuickTrial:
    parts = _parts(text, 3)
    end_text = _strip_prefix(parts[1], "ends")
    amount, currency, cadence = _parse_money_and_cadence(_strip_prefix(parts[2], "then"))
    return QuickTrial(
        name=_name(parts[0]),
        amount=amount,
        currency=currency,
        cadence=cadence,
        trial_end_date=parse_date_input(end_text, today),
    )


def _parse_interested_parts(text: str) -> QuickInterested:
    parts = _parts(text, 3)
    amount, currency = _parse_money(parts[1])
    return QuickInterested(
        name=_name(parts[0]),
        amount=amount,
        currency=currency,
        cadence=parse_cadence(parts[2]),
    )


def _money_from_match(match: re.Match[str]) -> tuple[str, str]:
    amount = match.group("amount") or match.group("amount_second")
    currency = match.group("currency") or match.group("currency_first")
    return normalize_amount(amount), normalize_currency(currency)


def _active_error(text: str) -> str:
    lowered = text.lower()
    if " from " not in f" {lowered} ":
        return f"Missing required word: from. Use: {ACTIVE_TEMPLATE}"
    return f"Use this template: {ACTIVE_TEMPLATE}"


def _trial_error(text: str) -> str:
    lowered = text.lower()
    padded = f" {lowered} "
    if " ends " not in padded:
        return f"Missing required word: ends. Use: {TRIAL_TEMPLATE}"
    if " then " not in padded:
        return f"Missing required word: then. Use: {TRIAL_TEMPLATE}"
    return f"Use this template: {TRIAL_TEMPLATE}"


def _name(text: str) -> str:
    name = text.strip()
    if not name:
        raise QuickAddError("Name is required.")
    return name


def _strip_prefix(text: str, prefix: str) -> str:
    value = text.strip()
    lowered = value.lower()
    prefix = prefix.lower()
    if lowered == prefix:
        raise QuickAddError(f"Add a value after {prefix}.")
    if lowered.startswith(prefix + " "):
        return value[len(prefix) + 1 :].strip()
    return value


def _parse_money(text: str) -> tuple[str, str]:
    value = text.strip()
    patterns = [
        r"^(?P<amount>\d+(?:\.\d+)?)\s+(?P<currency>[A-Za-z]{3,8})$",
        r"^(?P<currency>[A-Za-z]{3,8})\s+(?P<amount>\d+(?:\.\d+)?)$",
    ]
    for pattern in patterns:
        match = re.fullmatch(pattern, value)
        if match:
            return normalize_amount(match.group("amount")), normalize_currency(match.group("currency"))
    raise QuickAddError("Amount and currency must look like 10.98 SGD or SGD 10.98.")


def _parse_money_and_cadence(text: str) -> tuple[str, str, Cadence]:
    value = text.strip()
    patterns = [
        r"^(?P<amount>\d+(?:\.\d+)?)\s+(?P<currency>[A-Za-z]{3,8})\s+(?P<cadence>.+)$",
        r"^(?P<currency>[A-Za-z]{3,8})\s+(?P<amount>\d+(?:\.\d+)?)\s+(?P<cadence>.+)$",
    ]
    for pattern in patterns:
        match = re.fullmatch(pattern, value)
        if match:
            return (
                normalize_amount(match.group("amount")),
                normalize_currency(match.group("currency")),
                parse_cadence(match.group("cadence")),
            )
    raise QuickAddError("Paid plan must look like then 19.99 SGD monthly.")
