from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .crypto import FieldCrypto
from .models import Item, User


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class SQLiteStore:
    def __init__(self, db_path: str, crypto: FieldCrypto):
        self.db_path = db_path
        self.crypto = crypto

    @contextmanager
    def connect(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id integer primary key,
                    user_key_hash text unique not null,
                    telegram_chat_id_enc text not null,
                    default_currency_enc text not null,
                    timezone text not null,
                    default_reminder_time text not null,
                    default_reminder_offsets_json text not null,
                    created_at text not null,
                    updated_at text not null
                );

                CREATE TABLE IF NOT EXISTS items (
                    id integer primary key,
                    user_id integer not null references users(id) on delete cascade,
                    type text not null check (type in ('active', 'trial', 'interested')),
                    status text not null check (status in ('active', 'cancelled', 'deleted', 'needs_confirmation')),
                    name_enc text not null,
                    amount_enc text,
                    currency_enc text,
                    start_date text,
                    next_due_date text,
                    cadence_unit text check (cadence_unit in ('days', 'months', 'years') or cadence_unit is null),
                    cadence_count integer,
                    trial_end_date text,
                    reminder_offsets_json text not null,
                    notes_enc text,
                    created_at text not null,
                    updated_at text not null
                );

                CREATE TABLE IF NOT EXISTS reminder_events (
                    id integer primary key,
                    item_id integer not null references items(id) on delete cascade,
                    due_date text not null,
                    offset_days integer not null,
                    sent_at text not null,
                    unique (item_id, due_date, offset_days)
                );

                CREATE TABLE IF NOT EXISTS snoozes (
                    id integer primary key,
                    item_id integer not null references items(id) on delete cascade,
                    snooze_until text not null,
                    created_at text not null
                );

                CREATE TABLE IF NOT EXISTS item_prices (
                    id integer primary key,
                    item_id integer not null references items(id) on delete cascade,
                    amount_enc text not null,
                    effective_date text not null,
                    created_at text not null,
                    unique (item_id, effective_date)
                );

                CREATE INDEX IF NOT EXISTS idx_items_user_status ON items(user_id, status);
                CREATE INDEX IF NOT EXISTS idx_items_due ON items(next_due_date, trial_end_date);
                CREATE INDEX IF NOT EXISTS idx_snoozes_until ON snoozes(snooze_until);
                CREATE INDEX IF NOT EXISTS idx_item_prices_lookup ON item_prices(item_id, effective_date);
                """
            )

    def _user_from_row(self, row: sqlite3.Row) -> User:
        return User(
            id=row["id"],
            user_key_hash=row["user_key_hash"],
            telegram_chat_id=self.crypto.decrypt(row["telegram_chat_id_enc"]) or "",
            default_currency=self.crypto.decrypt(row["default_currency_enc"]) or "",
            timezone=row["timezone"],
            default_reminder_time=row["default_reminder_time"],
            default_reminder_offsets=json.loads(row["default_reminder_offsets_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _item_from_row(self, row: sqlite3.Row) -> Item:
        return Item(
            id=row["id"],
            user_id=row["user_id"],
            type=row["type"],
            status=row["status"],
            name=self.crypto.decrypt(row["name_enc"]) or "",
            amount=self.crypto.decrypt(row["amount_enc"]) if row["amount_enc"] else None,
            currency=self.crypto.decrypt(row["currency_enc"]) if row["currency_enc"] else None,
            start_date=row["start_date"],
            next_due_date=row["next_due_date"],
            cadence_unit=row["cadence_unit"],
            cadence_count=row["cadence_count"],
            trial_end_date=row["trial_end_date"],
            reminder_offsets=json.loads(row["reminder_offsets_json"]),
            notes=self.crypto.decrypt(row["notes_enc"]) if row["notes_enc"] else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create_or_update_user(
        self,
        *,
        telegram_user_id: int,
        telegram_chat_id: int | str,
        default_currency: str,
        timezone_name: str,
        reminder_time: str,
        reminder_offsets: list[int],
    ) -> User:
        now = utc_now_iso()
        user_key_hash = self.crypto.user_key_hash(telegram_user_id)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO users (
                    user_key_hash, telegram_chat_id_enc, default_currency_enc,
                    timezone, default_reminder_time, default_reminder_offsets_json,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_key_hash) DO UPDATE SET
                    telegram_chat_id_enc=excluded.telegram_chat_id_enc,
                    default_currency_enc=excluded.default_currency_enc,
                    timezone=excluded.timezone,
                    default_reminder_time=excluded.default_reminder_time,
                    default_reminder_offsets_json=excluded.default_reminder_offsets_json,
                    updated_at=excluded.updated_at
                """,
                (
                    user_key_hash,
                    self.crypto.encrypt(telegram_chat_id),
                    self.crypto.encrypt(default_currency.upper()),
                    timezone_name,
                    reminder_time,
                    json.dumps(sorted(set(reminder_offsets), reverse=True)),
                    now,
                    now,
                ),
            )
        user = self.get_user_by_telegram_id(telegram_user_id)
        if user is None:
            raise RuntimeError("User upsert failed")
        return user

    def get_user_by_telegram_id(self, telegram_user_id: int) -> User | None:
        user_key_hash = self.crypto.user_key_hash(telegram_user_id)
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_key_hash = ?", (user_key_hash,)).fetchone()
        return self._user_from_row(row) if row else None

    def get_user_by_id(self, user_id: int) -> User | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._user_from_row(row) if row else None

    def list_users(self) -> list[User]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [self._user_from_row(row) for row in rows]

    def update_user_settings(
        self,
        user_id: int,
        *,
        default_currency: str | None = None,
        timezone_name: str | None = None,
        reminder_time: str | None = None,
        reminder_offsets: list[int] | None = None,
    ) -> None:
        fields: dict[str, Any] = {"updated_at": utc_now_iso()}
        if default_currency is not None:
            fields["default_currency_enc"] = self.crypto.encrypt(default_currency.upper())
        if timezone_name is not None:
            fields["timezone"] = timezone_name
        if reminder_time is not None:
            fields["default_reminder_time"] = reminder_time
        if reminder_offsets is not None:
            fields["default_reminder_offsets_json"] = json.dumps(sorted(set(reminder_offsets), reverse=True))
        self._update_user_fields(user_id, fields)

    def _update_user_fields(self, user_id: int, fields: dict[str, Any]) -> None:
        assignments = ", ".join(f"{field} = ?" for field in fields)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE users SET {assignments} WHERE id = ?",
                (*fields.values(), user_id),
            )

    def create_item(
        self,
        *,
        user_id: int,
        type_: str,
        status: str,
        name: str,
        amount: str | None,
        currency: str | None,
        start_date: str | None,
        next_due_date: str | None,
        cadence_unit: str | None,
        cadence_count: int | None,
        trial_end_date: str | None,
        reminder_offsets: list[int],
        notes: str | None = None,
    ) -> Item:
        now = utc_now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO items (
                    user_id, type, status, name_enc, amount_enc, currency_enc,
                    start_date, next_due_date, cadence_unit, cadence_count,
                    trial_end_date, reminder_offsets_json, notes_enc, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    type_,
                    status,
                    self.crypto.encrypt(name),
                    self.crypto.encrypt(amount) if amount is not None else None,
                    self.crypto.encrypt(currency.upper()) if currency is not None else None,
                    start_date,
                    next_due_date,
                    cadence_unit,
                    cadence_count,
                    trial_end_date,
                    json.dumps(sorted(set(reminder_offsets), reverse=True)),
                    self.crypto.encrypt(notes) if notes else None,
                    now,
                    now,
                ),
            )
            item_id = cursor.lastrowid
        item = self.get_item(user_id, item_id)
        if item is None:
            raise RuntimeError("Item insert failed")
        return item

    def get_item(self, user_id: int, item_id: int) -> Item | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM items WHERE id = ? AND user_id = ?",
                (item_id, user_id),
            ).fetchone()
        return self._item_from_row(row) if row else None

    def list_items(
        self,
        user_id: int,
        *,
        include_cancelled: bool = False,
        include_deleted: bool = False,
    ) -> list[Item]:
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]
        if not include_cancelled:
            clauses.append("status != 'cancelled'")
        if not include_deleted:
            clauses.append("status != 'deleted'")
        where = " AND ".join(clauses)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM items WHERE {where} ORDER BY created_at, id",
                params,
            ).fetchall()
        return [self._item_from_row(row) for row in rows]

    def update_item(self, user_id: int, item_id: int, **fields: Any) -> Item | None:
        encrypted_fields = {
            "name": "name_enc",
            "amount": "amount_enc",
            "currency": "currency_enc",
            "notes": "notes_enc",
        }
        plain_fields = {
            "type": "type",
            "status": "status",
            "start_date": "start_date",
            "next_due_date": "next_due_date",
            "cadence_unit": "cadence_unit",
            "cadence_count": "cadence_count",
            "trial_end_date": "trial_end_date",
        }
        updates: dict[str, Any] = {}
        for field, value in fields.items():
            if field in encrypted_fields:
                column = encrypted_fields[field]
                if field == "currency" and value is not None:
                    value = str(value).upper()
                updates[column] = self.crypto.encrypt(value) if value is not None else None
            elif field == "reminder_offsets":
                updates["reminder_offsets_json"] = json.dumps(sorted(set(value), reverse=True))
            elif field in plain_fields:
                updates[plain_fields[field]] = value
            else:
                raise ValueError(f"Unsupported item field: {field}")

        if not updates:
            return self.get_item(user_id, item_id)
        updates["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{field} = ?" for field in updates)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE items SET {assignments} WHERE id = ? AND user_id = ?",
                (*updates.values(), item_id, user_id),
            )
        return self.get_item(user_id, item_id)

    def create_price_change(self, item_id: int, amount: str, effective_date: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO item_prices (item_id, amount_enc, effective_date, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(item_id, effective_date) DO UPDATE SET
                    amount_enc=excluded.amount_enc,
                    created_at=excluded.created_at
                """,
                (item_id, self.crypto.encrypt(amount), effective_date, utc_now_iso()),
            )

    def list_price_changes(self, item_id: int) -> list[tuple[str, str]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT effective_date, amount_enc
                FROM item_prices
                WHERE item_id = ?
                ORDER BY effective_date, id
                """,
                (item_id,),
            ).fetchall()
        return [(row["effective_date"], self.crypto.decrypt(row["amount_enc"]) or "0") for row in rows]

    def amount_for_item_on_date(self, item_id: int, effective_date: str, fallback_amount: str | None) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT amount_enc
                FROM item_prices
                WHERE item_id = ? AND effective_date <= ?
                ORDER BY effective_date DESC, id DESC
                LIMIT 1
                """,
                (item_id, effective_date),
            ).fetchone()
        if row:
            return self.crypto.decrypt(row["amount_enc"])
        return fallback_amount

    def record_reminder_event(self, item_id: int, due_date: str, offset_days: int) -> bool:
        try:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO reminder_events (item_id, due_date, offset_days, sent_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (item_id, due_date, offset_days, utc_now_iso()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def create_snooze(self, item_id: int, snooze_until: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO snoozes (item_id, snooze_until, created_at) VALUES (?, ?, ?)",
                (item_id, snooze_until, utc_now_iso()),
            )

    def list_due_snoozes(self, now_iso: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT snoozes.id as snooze_id, snoozes.snooze_until, items.*, users.telegram_chat_id_enc
                FROM snoozes
                JOIN items ON items.id = snoozes.item_id
                JOIN users ON users.id = items.user_id
                WHERE snoozes.snooze_until <= ?
                  AND items.status IN ('active', 'needs_confirmation')
                ORDER BY snoozes.snooze_until
                """,
                (now_iso,),
            ).fetchall()
        return rows

    def delete_snooze(self, snooze_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM snoozes WHERE id = ?", (snooze_id,))

    def item_from_snooze_row(self, row: sqlite3.Row) -> Item:
        return self._item_from_row(row)

    def chat_id_from_snooze_row(self, row: sqlite3.Row) -> str:
        return self.crypto.decrypt(row["telegram_chat_id_enc"]) or ""
