from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .dates import (
    Cadence,
    add_cadence,
    annualized_amount,
    decimal_money,
    due_dates_between,
    format_money,
    monthly_amount,
    next_due_date,
    parse_date_input,
)
from .models import Item, User
from .storage import SQLiteStore


DEFAULT_OFFSETS = [7, 3, 1, 0]


@dataclass(frozen=True)
class UpcomingEntry:
    due_date: date
    item: Item
    kind: str


@dataclass(frozen=True)
class ReminderWork:
    user: User
    item: Item
    due_date: date
    offset_days: int
    kind: str


@dataclass(frozen=True)
class ReminderPreviewEntry:
    reminder_date: date
    due_date: date
    item: Item
    offset_days: int
    kind: str


@dataclass(frozen=True)
class ReceiptEntry:
    payment_date: date
    item: Item
    amount: Decimal
    currency: str
    label: str


def normalize_currency(value: str) -> str:
    currency = value.strip().upper()
    if not currency or len(currency) > 8:
        raise ValueError("Use a short currency code such as SGD or USD")
    return currency


def normalize_amount(value: str) -> str:
    amount = Decimal(value.strip())
    if amount < 0:
        raise ValueError("Amount must be zero or greater")
    return format(amount.normalize(), "f")


def parse_timezone(value: str) -> str:
    tz = value.strip()
    try:
        ZoneInfo(tz)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Unknown timezone") from exc
    return tz


def parse_reminder_time(value: str) -> str:
    parsed = time.fromisoformat(value.strip())
    return parsed.strftime("%H:%M")


def parse_window_days(value: str | None, default: int = 30) -> int:
    if not value:
        return default
    days = int(value)
    if days < 1 or days > 365:
        raise ValueError("Window must be between 1 and 365 days")
    return days


class SubShelfService:
    def __init__(self, store: SQLiteStore):
        self.store = store

    def add_active(
        self,
        *,
        user: User,
        name: str,
        amount: str,
        currency: str,
        start_date: date,
        cadence: Cadence,
        reminder_offsets: list[int] | None = None,
        today: date | None = None,
    ) -> Item:
        today = today or local_today(user)
        due = next_due_date(start_date, cadence, today, include_today=True)
        item = self.store.create_item(
            user_id=user.id,
            type_="active",
            status="active",
            name=name.strip(),
            amount=normalize_amount(amount),
            currency=normalize_currency(currency),
            start_date=start_date.isoformat(),
            next_due_date=due.isoformat(),
            cadence_unit=cadence.unit,
            cadence_count=cadence.count,
            trial_end_date=None,
            reminder_offsets=reminder_offsets if reminder_offsets is not None else user.default_reminder_offsets,
        )
        self.store.create_price_change(item.id, item.amount or "0", start_date.isoformat())
        return item

    def add_trial(
        self,
        *,
        user: User,
        name: str,
        trial_end_date: date,
        paid_amount: str,
        currency: str,
        paid_cadence: Cadence,
        reminder_offsets: list[int] | None = None,
    ) -> Item:
        item = self.store.create_item(
            user_id=user.id,
            type_="trial",
            status="active",
            name=name.strip(),
            amount=normalize_amount(paid_amount),
            currency=normalize_currency(currency),
            start_date=None,
            next_due_date=trial_end_date.isoformat(),
            cadence_unit=paid_cadence.unit,
            cadence_count=paid_cadence.count,
            trial_end_date=trial_end_date.isoformat(),
            reminder_offsets=reminder_offsets if reminder_offsets is not None else user.default_reminder_offsets,
        )
        self.store.create_price_change(item.id, item.amount or "0", trial_end_date.isoformat())
        return item

    def add_interested(
        self,
        *,
        user: User,
        name: str,
        expected_amount: str,
        currency: str,
        cadence: Cadence,
    ) -> Item:
        item = self.store.create_item(
            user_id=user.id,
            type_="interested",
            status="active",
            name=name.strip(),
            amount=normalize_amount(expected_amount),
            currency=normalize_currency(currency),
            start_date=None,
            next_due_date=None,
            cadence_unit=cadence.unit,
            cadence_count=cadence.count,
            trial_end_date=None,
            reminder_offsets=[],
        )
        self.store.create_price_change(item.id, item.amount or "0", local_today(user).isoformat())
        return item

    def list_text(self, user: User) -> str:
        items = self.store.list_items(user.id)
        active = [item for item in items if item.type == "active" and item.status == "active"]
        trials = [item for item in items if item.type == "trial" and item.status == "active"]
        needs_confirmation = [
            item for item in items if item.type == "trial" and item.status == "needs_confirmation"
        ]
        interested = [item for item in items if item.type == "interested" and item.status == "active"]

        lines = ["Your SubShelf"]
        counter = 1
        today = local_today(user)
        counter = _append_section(lines, "Active", active, counter, lambda item: self.format_active_line(item, today))
        counter = _append_section(lines, "Trials", trials, counter, lambda item: self.format_trial_line(item, today))
        counter = _append_section(
            lines,
            "Trials needing confirmation",
            needs_confirmation,
            counter,
            lambda item: self.format_trial_line(item, today),
        )
        _append_section(lines, "Watchlist", interested, counter, lambda item: self.format_interested_line(item, today))
        if len(lines) == 1:
            lines.append("")
            lines.append("Nothing tracked yet. Use /add, /trial, or /interested.")
        return "\n".join(lines)

    def format_active_line(self, item: Item, today: date) -> str:
        cadence = item.cadence.display() if item.cadence else "recurring"
        due = self.next_due_for_item(item, today)
        due_text = due.isoformat() if due else item.next_due_date
        next_amount = self.amount_for_item_on_date(item, due) if due else item.decimal_amount
        current_amount = self.amount_for_item_on_date(item, today) or next_amount
        if next_amount is None:
            return f"{item.name} - {item.currency or ''} {cadence} - renews {due_text}".strip()
        next_text = f"{item.currency} {format_money(next_amount)}"
        if current_amount is not None and current_amount != next_amount:
            current_text = f"{item.currency} {format_money(current_amount)}"
            return f"{item.name} - renews {due_text} at {next_text} {cadence} (current cycle {current_text})"
        return f"{item.name} - {next_text} {cadence} - renews {due_text}"

    def format_trial_line(self, item: Item, today: date) -> str:
        cadence = item.cadence.display() if item.cadence else "recurring"
        status = "needs confirmation, " if item.status == "needs_confirmation" else ""
        trial_end = date.fromisoformat(item.trial_end_date) if item.trial_end_date else today
        amount = self.amount_for_item_on_date(item, trial_end) or item.decimal_amount or Decimal()
        return f"{item.name} - {status}trial ends {item.trial_end_date}, then {item.currency} {format_money(amount)} {cadence}"

    def format_interested_line(self, item: Item, today: date) -> str:
        cadence = item.cadence.display() if item.cadence else "recurring"
        amount = self.amount_for_item_on_date(item, today) or item.decimal_amount or Decimal()
        return f"{item.name} - {item.currency} {format_money(amount)} {cadence}"

    def upcoming(self, user: User, *, today: date, days: int = 30) -> tuple[list[UpcomingEntry], dict[str, Decimal]]:
        window_end = today + timedelta(days=days)
        entries: list[UpcomingEntry] = []
        totals: dict[str, Decimal] = defaultdict(Decimal)
        items = self.store.list_items(user.id)

        for item in items:
            cadence = item.cadence
            if item.status != "active" or not cadence:
                continue
            currency = item.currency
            if not currency:
                continue

            if item.type == "active" and item.start_date:
                start = date.fromisoformat(item.start_date)
                for due in due_dates_between(start, cadence, today, window_end):
                    amount = self.amount_for_item_on_date(item, due)
                    if amount is None:
                        continue
                    entries.append(UpcomingEntry(due, item, "renewal"))
                    totals[currency] += amount
            elif item.type == "trial" and item.trial_end_date:
                trial_end = date.fromisoformat(item.trial_end_date)
                if today <= trial_end <= window_end:
                    amount = self.amount_for_item_on_date(item, trial_end)
                    if amount is None:
                        continue
                    entries.append(UpcomingEntry(trial_end, item, "trial"))
                    totals[currency] += amount

        entries.sort(key=lambda entry: (entry.due_date, entry.item.name.lower()))
        return entries, totals

    def upcoming_text(self, user: User, *, today: date, days: int = 30) -> str:
        entries, totals = self.upcoming(user, today=today, days=days)
        lines = [f"Upcoming in the next {days} days"]
        if not entries:
            lines.append("")
            lines.append("No renewals or trial endings in this window.")
            return "\n".join(lines)

        current_date: date | None = None
        for entry in entries:
            if entry.due_date != current_date:
                current_date = entry.due_date
                lines.append("")
                lines.append(current_date.isoformat())
            item = entry.item
            if entry.kind == "trial":
                amount = self.amount_for_item_on_date(item, entry.due_date) or Decimal()
                lines.append(
                    f"- {item.name} trial ends, then {item.currency} {format_money(amount)}/{item.cadence.display() if item.cadence else 'cycle'}"
                )
            else:
                amount = self.amount_for_item_on_date(item, entry.due_date) or Decimal()
                lines.append(
                    f"- {item.name} renews, {item.currency} {format_money(amount)}"
                )

        lines.append("")
        lines.append("Total if all renew:")
        lines.extend(_format_totals(totals))
        return "\n".join(lines)

    def spending(self, user: User, *, today: date) -> dict[str, dict[str, Decimal]]:
        first_of_month = today.replace(day=1)
        first_of_year = today.replace(month=1, day=1)
        projected_end = add_cadence(today, Cadence("years", 1), 1)
        report: dict[str, dict[str, Decimal]] = {
            "This month": defaultdict(Decimal),
            "This year": defaultdict(Decimal),
            "Since start dates": defaultdict(Decimal),
            "Projected future renewals (next 12 months)": defaultdict(Decimal),
        }

        for item in self.store.list_items(user.id):
            if item.type != "active" or item.status != "active" or not item.start_date:
                continue
            cadence = item.cadence
            currency = item.currency
            if not cadence or not currency:
                continue
            start = date.fromisoformat(item.start_date)

            for due in due_dates_between(start, cadence, first_of_month, today):
                if due <= today:
                    amount = self.amount_for_item_on_date(item, due)
                    if amount is None:
                        continue
                    report["This month"][currency] += amount
            for due in due_dates_between(start, cadence, first_of_year, today):
                if due <= today:
                    amount = self.amount_for_item_on_date(item, due)
                    if amount is None:
                        continue
                    report["This year"][currency] += amount
            for due in due_dates_between(start, cadence, start, today):
                amount = self.amount_for_item_on_date(item, due)
                if amount is None:
                    continue
                report["Since start dates"][currency] += amount
            for due in due_dates_between(start, cadence, today + timedelta(days=1), projected_end):
                amount = self.amount_for_item_on_date(item, due)
                if amount is None:
                    continue
                report["Projected future renewals (next 12 months)"][currency] += amount

        return report

    def spending_text(self, user: User, *, today: date) -> str:
        report = self.spending(user, today=today)
        lines = ["Spending"]
        for section, totals in report.items():
            lines.append("")
            lines.append(section)
            lines.extend(_format_totals(totals))

        watchlist = self.watchlist_impact(user)
        if watchlist:
            lines.append("")
            lines.append("Watchlist potential")
            for currency, values in sorted(watchlist.items()):
                lines.append(
                    f"{currency} {format_money(values['monthly'])}/month, {format_money(values['yearly'])}/year"
                )
        return "\n".join(lines)

    def receipt_text(self, user: User, *, today: date, mode: str = "month", days: int = 30) -> str:
        mode = mode.lower()
        title, start, end, entries = self.receipt_entries(user, today=today, mode=mode, days=days)
        receipt_no = f"{today.strftime('%Y%m%d')}-{mode.upper()}"
        lines = [
            "SUBSHELF RECEIPT",
            f"No. {receipt_no}",
            title,
            f"Period: {start.isoformat()} to {end.isoformat()}",
            "-" * 32,
        ]
        if not entries:
            lines.append("No subscription charges here.")
        else:
            for entry in entries:
                amount = f"{entry.currency} {format_money(entry.amount)}"
                lines.append(f"{entry.payment_date.isoformat()}  {entry.label}")
                lines.append(f"            {amount}")
        lines.append("-" * 32)
        lines.append("TOTAL")
        lines.extend(_format_totals(_receipt_totals(entries)))
        lines.append("-" * 32)
        lines.append("Filed by SubShelf.")
        return "\n".join(lines)

    def receipt_entries(
        self,
        user: User,
        *,
        today: date,
        mode: str,
        days: int = 30,
    ) -> tuple[str, date, date, list[ReceiptEntry]]:
        if mode == "upcoming":
            start = today
            end = today + timedelta(days=days)
            entries = self._receipt_upcoming_entries(user, start, end)
            return f"Future renewals, next {days} days", start, end, entries

        if mode == "year":
            start = today.replace(month=1, day=1)
            title = "Paid renewals, this year"
        elif mode == "all":
            start = self._earliest_active_start(user, today)
            title = "Paid renewals, all tracked time"
        else:
            start = today.replace(day=1)
            title = "Paid renewals, this month"
        entries = self._receipt_paid_entries(user, start, today)
        return title, start, today, entries

    def _receipt_paid_entries(self, user: User, start: date, end: date) -> list[ReceiptEntry]:
        entries: list[ReceiptEntry] = []
        for item in self.store.list_items(user.id):
            if item.type != "active" or item.status != "active" or not item.start_date or not item.cadence or not item.currency:
                continue
            item_start = date.fromisoformat(item.start_date)
            for payment_date in due_dates_between(item_start, item.cadence, start, end):
                amount = self.amount_for_item_on_date(item, payment_date)
                if amount is None:
                    continue
                entries.append(ReceiptEntry(payment_date, item, amount, item.currency, item.name))
        return sorted(entries, key=lambda entry: (entry.payment_date, entry.label.lower()))

    def _receipt_upcoming_entries(self, user: User, start: date, end: date) -> list[ReceiptEntry]:
        entries: list[ReceiptEntry] = []
        for item in self.store.list_items(user.id):
            if item.status != "active" or not item.cadence or not item.currency:
                continue
            if item.type == "active" and item.start_date:
                item_start = date.fromisoformat(item.start_date)
                for payment_date in due_dates_between(item_start, item.cadence, start, end):
                    amount = self.amount_for_item_on_date(item, payment_date)
                    if amount is None:
                        continue
                    entries.append(ReceiptEntry(payment_date, item, amount, item.currency, f"{item.name} renews"))
            elif item.type == "trial" and item.trial_end_date:
                trial_end = date.fromisoformat(item.trial_end_date)
                if start <= trial_end <= end:
                    amount = self.amount_for_item_on_date(item, trial_end)
                    if amount is None:
                        continue
                    entries.append(ReceiptEntry(trial_end, item, amount, item.currency, f"{item.name} trial ends"))
        return sorted(entries, key=lambda entry: (entry.payment_date, entry.label.lower()))

    def _earliest_active_start(self, user: User, today: date) -> date:
        starts = [
            date.fromisoformat(item.start_date)
            for item in self.store.list_items(user.id)
            if item.type == "active" and item.status == "active" and item.start_date
        ]
        return min(starts) if starts else today

    def reminder_preview(self, user: User, *, today: date, days: int = 30) -> list[ReminderPreviewEntry]:
        window_end = today + timedelta(days=days)
        entries: list[ReminderPreviewEntry] = []
        items = self.store.list_items(user.id)

        for item in items:
            if item.status != "active":
                continue
            offsets = item.reminder_offsets or user.default_reminder_offsets
            max_offset = max(offsets) if offsets else 0

            if item.type == "active" and item.start_date and item.cadence:
                start = date.fromisoformat(item.start_date)
                due_dates = due_dates_between(
                    start,
                    item.cadence,
                    today,
                    window_end + timedelta(days=max_offset),
                )
                for due in due_dates:
                    for offset in offsets:
                        reminder_date = due - timedelta(days=offset)
                        if today <= reminder_date <= window_end:
                            entries.append(
                                ReminderPreviewEntry(reminder_date, due, item, offset, "renewal")
                            )
            elif item.type == "trial" and item.trial_end_date:
                due = date.fromisoformat(item.trial_end_date)
                for offset in offsets:
                    reminder_date = due - timedelta(days=offset)
                    if today <= reminder_date <= window_end:
                        entries.append(ReminderPreviewEntry(reminder_date, due, item, offset, "trial"))

        entries.sort(
            key=lambda entry: (
                entry.reminder_date,
                entry.due_date,
                entry.item.name.lower(),
                entry.offset_days,
            )
        )
        return entries

    def reminders_text(self, user: User, *, today: date, days: int = 30) -> str:
        entries = self.reminder_preview(user, today=today, days=days)
        lines = [f"Reminder preview for the next {days} days"]
        lines.append(f"Local send time: {user.default_reminder_time} {user.timezone}")
        if not entries:
            lines.append("")
            lines.append("No reminders scheduled in this window.")
            return "\n".join(lines)

        current_date: date | None = None
        for entry in entries:
            if entry.reminder_date != current_date:
                current_date = entry.reminder_date
                lines.append("")
                lines.append(current_date.isoformat())
            if entry.kind == "trial":
                lines.append(
                    f"- {entry.item.name}: trial ends {entry.due_date.isoformat()} ({_offset_text(entry.offset_days)})"
                )
            else:
                amount = format_money(self.amount_for_item_on_date(entry.item, entry.due_date) or Decimal())
                lines.append(
                    f"- {entry.item.name}: renews {entry.due_date.isoformat()}, {entry.item.currency} {amount} ({_offset_text(entry.offset_days)})"
                )
        return "\n".join(lines)

    def watchlist_impact(self, user: User) -> dict[str, dict[str, Decimal]]:
        impact: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        for item in self.store.list_items(user.id):
            if item.type != "interested" or item.status != "active":
                continue
            amount = item.decimal_amount
            cadence = item.cadence
            currency = item.currency
            if amount is None or cadence is None or not currency:
                continue
            impact[currency]["monthly"] += monthly_amount(amount, cadence)
            impact[currency]["yearly"] += annualized_amount(amount, cadence)
        return impact

    def convert_interested_to_active(self, user: User, item_id: int, *, today: date) -> Item | None:
        item = self.store.get_item(user.id, item_id)
        if item is None or item.type != "interested" or item.status != "active" or item.cadence is None:
            return None
        due = next_due_date(today, item.cadence, today, include_today=True)
        updated = self.store.update_item(
            user.id,
            item.id,
            type="active",
            status="active",
            start_date=today.isoformat(),
            next_due_date=due.isoformat(),
            reminder_offsets=user.default_reminder_offsets,
        )
        if updated and updated.amount:
            self._ensure_initial_price(updated)
            self.store.create_price_change(updated.id, updated.amount, today.isoformat())
        return updated

    def continue_trial(self, user: User, item_id: int, *, today: date) -> Item | None:
        item = self.store.get_item(user.id, item_id)
        if item is None or item.type != "trial" or item.cadence is None or not item.trial_end_date:
            return None
        start = date.fromisoformat(item.trial_end_date)
        due = next_due_date(start, item.cadence, today, include_today=True)
        updated = self.store.update_item(
            user.id,
            item.id,
            type="active",
            status="active",
            start_date=start.isoformat(),
            next_due_date=due.isoformat(),
            trial_end_date=None,
        )
        if updated and updated.amount:
            self._ensure_initial_price(updated)
            self.store.create_price_change(updated.id, updated.amount, start.isoformat())
        return updated

    def cancel_item(self, user: User, item_id: int) -> tuple[Item | None, Decimal | None]:
        item = self.store.get_item(user.id, item_id)
        if item is None:
            return None, None
        savings = None
        amount = self.amount_for_item_on_date(item, local_today(user))
        if amount is not None and item.cadence is not None:
            savings = annualized_amount(amount, item.cadence)
        updated = self.store.update_item(user.id, item.id, status="cancelled")
        return updated, savings

    def restore_item(self, user: User, item_id: int, *, today: date) -> Item | None:
        item = self.store.get_item(user.id, item_id)
        if item is None or item.status != "cancelled":
            return None

        fields: dict[str, object] = {"status": "active"}
        if item.type == "active" and item.start_date and item.cadence:
            due = next_due_date(date.fromisoformat(item.start_date), item.cadence, today, include_today=True)
            fields["next_due_date"] = due.isoformat()
        elif item.type == "trial" and item.trial_end_date:
            trial_end = date.fromisoformat(item.trial_end_date)
            fields["status"] = "active" if trial_end >= today else "needs_confirmation"
            fields["next_due_date"] = item.trial_end_date

        return self.store.update_item(user.id, item.id, **fields)

    def search_items(self, user: User, query: str) -> list[Item]:
        term = query.strip().lower()
        if not term:
            return []
        return [
            item
            for item in self.store.list_items(user.id, include_cancelled=True)
            if term in item.name.lower() and item.status != "deleted"
        ]

    def amount_for_item_on_date(self, item: Item, target_date: date) -> Decimal | None:
        amount = self.store.amount_for_item_on_date(item.id, target_date.isoformat(), item.amount)
        return decimal_money(amount) if amount is not None else None

    def price_history_lines(self, item: Item) -> list[str]:
        changes = self.store.list_price_changes(item.id)
        return [f"{effective_date}: {item.currency} {format_money(decimal_money(amount))}" for effective_date, amount in changes]

    def next_due_for_item(self, item: Item, today: date) -> date | None:
        if item.type == "active" and item.start_date and item.cadence:
            return next_due_date(date.fromisoformat(item.start_date), item.cadence, today, include_today=True)
        if item.type == "trial" and item.trial_end_date:
            return date.fromisoformat(item.trial_end_date)
        return None

    def update_amount(self, user: User, item: Item, new_amount: str, effective_date: date) -> Item | None:
        normalized = normalize_amount(new_amount)
        self._ensure_initial_price(item)
        self.store.create_price_change(item.id, normalized, effective_date.isoformat())
        return self.store.update_item(user.id, item.id, amount=normalized)

    def amount_effective_date(self, item: Item, choice: str, today: date) -> date:
        if choice == "since_start":
            return self._price_start_date(item, today)
        if choice == "this_cycle":
            return self._current_cycle_start(item, today)
        if choice == "next_cycle":
            if item.type == "active" and item.start_date and item.cadence:
                return next_due_date(date.fromisoformat(item.start_date), item.cadence, today, include_today=False)
            return self._price_start_date(item, today)
        raise ValueError(f"Unsupported amount effective choice: {choice}")

    def _ensure_initial_price(self, item: Item) -> None:
        if item.amount is None:
            return
        if self.store.list_price_changes(item.id):
            return
        self.store.create_price_change(item.id, item.amount, self._price_start_date(item, date.today()).isoformat())

    def _price_start_date(self, item: Item, today: date) -> date:
        if item.type == "active" and item.start_date:
            return date.fromisoformat(item.start_date)
        if item.type == "trial" and item.trial_end_date:
            return date.fromisoformat(item.trial_end_date)
        return today

    def _current_cycle_start(self, item: Item, today: date) -> date:
        if item.type != "active" or not item.start_date or not item.cadence:
            return self._price_start_date(item, today)
        start = date.fromisoformat(item.start_date)
        due_dates = due_dates_between(start, item.cadence, start, today)
        return due_dates[-1] if due_dates else start

    def reminder_work_due(self, now_utc: datetime) -> list[ReminderWork]:
        work: list[ReminderWork] = []
        for user in self.store.list_users():
            local_now = now_utc.astimezone(ZoneInfo(user.timezone))
            if local_now.strftime("%H:%M") != user.default_reminder_time:
                self._mark_overdue_trials(user, local_now.date())
                continue

            items = self.store.list_items(user.id)
            for item in items:
                if item.status != "active":
                    continue
                offsets = item.reminder_offsets or user.default_reminder_offsets
                if item.type == "active" and item.start_date and item.cadence:
                    due = next_due_date(
                        date.fromisoformat(item.start_date),
                        item.cadence,
                        local_now.date(),
                        include_today=True,
                    )
                    for offset in offsets:
                        if due - timedelta(days=offset) == local_now.date():
                            work.append(ReminderWork(user, item, due, offset, "renewal"))
                elif item.type == "trial" and item.trial_end_date:
                    trial_end = date.fromisoformat(item.trial_end_date)
                    for offset in offsets:
                        if trial_end - timedelta(days=offset) == local_now.date():
                            work.append(ReminderWork(user, item, trial_end, offset, "trial"))
            self._mark_overdue_trials(user, local_now.date())
        return work

    def _mark_overdue_trials(self, user: User, today: date) -> None:
        for item in self.store.list_items(user.id):
            if item.type == "trial" and item.status == "active" and item.trial_end_date:
                if date.fromisoformat(item.trial_end_date) < today:
                    self.store.update_item(user.id, item.id, status="needs_confirmation")


def local_today(user: User) -> date:
    return datetime.now(ZoneInfo(user.timezone)).date()


def local_snooze_until_utc(user: User) -> str:
    local_now = datetime.now(ZoneInfo(user.timezone))
    hour, minute = map(int, user.default_reminder_time.split(":"))
    local_target = datetime.combine(
        local_now.date() + timedelta(days=1),
        time(hour=hour, minute=minute),
        tzinfo=ZoneInfo(user.timezone),
    )
    return local_target.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _append_section(
    lines: list[str],
    title: str,
    items: list[Item],
    counter: int,
    formatter,
) -> int:
    if not items:
        return counter
    lines.append("")
    lines.append(title)
    for item in items:
        lines.append(f"{counter}. {formatter(item)}")
        counter += 1
    return counter


def _format_totals(totals: dict[str, Decimal]) -> list[str]:
    if not totals:
        return ["None"]
    return [f"{currency} {format_money(amount)}" for currency, amount in sorted(totals.items())]


def _receipt_totals(entries: list[ReceiptEntry]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for entry in entries:
        totals[entry.currency] += entry.amount
    return totals


def _offset_text(offset_days: int) -> str:
    if offset_days == 0:
        return "on the day"
    if offset_days == 1:
        return "1 day before"
    return f"{offset_days} days before"


def date_from_user_text(text: str, user: User) -> date:
    return parse_date_input(text, local_today(user))
