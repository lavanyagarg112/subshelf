from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .dates import Cadence


@dataclass(frozen=True)
class User:
    id: int
    user_key_hash: str
    telegram_chat_id: str
    default_currency: str
    timezone: str
    default_reminder_time: str
    default_reminder_offsets: list[int]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Item:
    id: int
    user_id: int
    type: str
    status: str
    name: str
    amount: str | None
    currency: str | None
    start_date: str | None
    next_due_date: str | None
    cadence_unit: str | None
    cadence_count: int | None
    trial_end_date: str | None
    reminder_offsets: list[int]
    notes: str | None
    created_at: str
    updated_at: str

    @property
    def cadence(self) -> Cadence | None:
        if not self.cadence_unit or not self.cadence_count:
            return None
        return Cadence(self.cadence_unit, self.cadence_count)

    @property
    def decimal_amount(self) -> Decimal | None:
        if self.amount is None:
            return None
        return Decimal(self.amount)
