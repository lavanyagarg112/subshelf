from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from decimal import Decimal

from subshelf.crypto import FieldCrypto, generate_key
from subshelf.dates import Cadence, add_cadence, due_dates_between, next_due_date
from subshelf.quickadd import QuickAddError, parse_active, parse_interested, parse_trial, quickadd_help_text
from subshelf.services import SubShelfService
from subshelf.storage import SQLiteStore


class DateLogicTests(unittest.TestCase):
    def test_month_end_recurrence_preserves_calendar_day_where_possible(self) -> None:
        cadence = Cadence("months", 1)
        start = date(2026, 1, 31)

        self.assertEqual(add_cadence(start, cadence, 1), date(2026, 2, 28))
        self.assertEqual(add_cadence(start, cadence, 2), date(2026, 3, 31))
        self.assertEqual(next_due_date(start, cadence, date(2026, 2, 28)), date(2026, 2, 28))
        self.assertEqual(
            next_due_date(start, cadence, date(2026, 2, 28), include_today=False),
            date(2026, 3, 31),
        )

    def test_due_dates_count_payments_through_today(self) -> None:
        dates = due_dates_between(
            date(2026, 1, 10),
            Cadence("months", 1),
            date(2026, 1, 1),
            date(2026, 5, 12),
        )
        self.assertEqual(
            dates,
            [
                date(2026, 1, 10),
                date(2026, 2, 10),
                date(2026, 3, 10),
                date(2026, 4, 10),
                date(2026, 5, 10),
            ],
        )


class StoreServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "subshelf.sqlite3")
        crypto = FieldCrypto.from_key(generate_key())
        self.store = SQLiteStore(self.db_path, crypto)
        self.store.init_db()
        self.service = SubShelfService(self.store)
        self.user = self.store.create_or_update_user(
            telegram_user_id=1001,
            telegram_chat_id=555001,
            default_currency="SGD",
            timezone_name="Asia/Singapore",
            reminder_time="09:00",
            reminder_offsets=[7, 3, 1, 0],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_spending_groups_by_currency(self) -> None:
        self.service.add_active(
            user=self.user,
            name="Spotify",
            amount="10.98",
            currency="SGD",
            start_date=date(2026, 1, 10),
            cadence=Cadence("months", 1),
            today=date(2026, 5, 12),
        )
        self.service.add_active(
            user=self.user,
            name="iCloud",
            amount="2.99",
            currency="USD",
            start_date=date(2026, 1, 10),
            cadence=Cadence("months", 1),
            today=date(2026, 5, 12),
        )

        spending = self.service.spending(self.user, today=date(2026, 5, 12))

        self.assertEqual(spending["Since start dates"]["SGD"], Decimal("54.90"))
        self.assertEqual(spending["Since start dates"]["USD"], Decimal("14.95"))

    def test_user_data_is_scoped(self) -> None:
        other = self.store.create_or_update_user(
            telegram_user_id=2002,
            telegram_chat_id=555002,
            default_currency="USD",
            timezone_name="UTC",
            reminder_time="12:00",
            reminder_offsets=[1, 0],
        )
        self.service.add_active(
            user=self.user,
            name="Spotify",
            amount="10.98",
            currency="SGD",
            start_date=date(2026, 5, 1),
            cadence=Cadence("months", 1),
            today=date(2026, 5, 12),
        )
        self.service.add_active(
            user=other,
            name="Domain",
            amount="18",
            currency="USD",
            start_date=date(2026, 5, 1),
            cadence=Cadence("years", 1),
            today=date(2026, 5, 12),
        )

        self.assertEqual([item.name for item in self.store.list_items(self.user.id)], ["Spotify"])
        self.assertEqual([item.name for item in self.store.list_items(other.id)], ["Domain"])
        self.assertIsNone(self.store.get_item(self.user.id, self.store.list_items(other.id)[0].id))

    def test_sensitive_fields_are_not_plaintext_in_sqlite(self) -> None:
        self.service.add_active(
            user=self.user,
            name="Spotify",
            amount="10.98",
            currency="SGD",
            start_date=date(2026, 5, 1),
            cadence=Cadence("months", 1),
            today=date(2026, 5, 12),
        )

        with open(self.db_path, "rb") as handle:
            raw = handle.read()

        for plaintext in [b"Spotify", b"10.98", b"SGD", b"555001"]:
            self.assertNotIn(plaintext, raw)

    def test_trial_and_interested_behavior(self) -> None:
        trial = self.service.add_trial(
            user=self.user,
            name="Canva",
            trial_end_date=date(2026, 5, 15),
            paid_amount="19.99",
            currency="SGD",
            paid_cadence=Cadence("months", 1),
        )
        watch = self.service.add_interested(
            user=self.user,
            name="Cursor",
            expected_amount="20",
            currency="USD",
            cadence=Cadence("months", 1),
        )

        upcoming, totals = self.service.upcoming(self.user, today=date(2026, 5, 12), days=7)
        spending = self.service.spending(self.user, today=date(2026, 5, 12))

        self.assertEqual([entry.item.name for entry in upcoming], ["Canva"])
        self.assertEqual(totals["SGD"], Decimal("19.99"))
        self.assertEqual(dict(spending["Since start dates"]), {})
        self.assertEqual(trial.type, "trial")
        self.assertEqual(watch.type, "interested")

    def test_search_matches_user_items_including_cancelled(self) -> None:
        item = self.service.add_active(
            user=self.user,
            name="Spotify Premium",
            amount="10.98",
            currency="SGD",
            start_date=date(2026, 5, 1),
            cadence=Cadence("months", 1),
            today=date(2026, 5, 12),
        )
        self.service.cancel_item(self.user, item.id)

        matches = self.service.search_items(self.user, "spot")

        self.assertEqual([match.name for match in matches], ["Spotify Premium"])

    def test_cancelled_subscription_actions_are_not_cancel_again(self) -> None:
        import sys
        import types

        from subshelf.bot import item_details, single_item_markup

        telegram_stub = None
        if "telegram" not in sys.modules:
            telegram_stub = types.ModuleType("telegram")

            class InlineKeyboardButton:
                def __init__(self, text: str, callback_data: str):
                    self.text = text
                    self.callback_data = callback_data

            class InlineKeyboardMarkup:
                def __init__(self, inline_keyboard):
                    self.inline_keyboard = inline_keyboard

            telegram_stub.InlineKeyboardButton = InlineKeyboardButton
            telegram_stub.InlineKeyboardMarkup = InlineKeyboardMarkup
            sys.modules["telegram"] = telegram_stub

        item = self.service.add_active(
            user=self.user,
            name="Spotify",
            amount="10.98",
            currency="SGD",
            start_date=date(2026, 1, 10),
            cadence=Cadence("months", 1),
            today=date(2026, 5, 13),
        )
        cancelled, _ = self.service.cancel_item(self.user, item.id)

        try:
            details = item_details(self.service, self.user, cancelled)
            markup = single_item_markup(cancelled)
            labels = [button.text for row in markup.inline_keyboard for button in row]
        finally:
            if telegram_stub:
                sys.modules.pop("telegram", None)

        self.assertIn("Last known amount: SGD 10.98", details)
        self.assertNotIn("Next due:", details)
        self.assertNotIn("Reminders:", details)
        self.assertIn("Restore subscription", labels)
        self.assertIn("Delete", labels)
        self.assertNotIn("Cancel subscription", labels)

    def test_restore_cancelled_subscription_recalculates_next_due(self) -> None:
        item = self.service.add_active(
            user=self.user,
            name="Spotify",
            amount="10.98",
            currency="SGD",
            start_date=date(2026, 1, 10),
            cadence=Cadence("months", 1),
            today=date(2026, 5, 13),
        )
        self.service.cancel_item(self.user, item.id)

        restored = self.service.restore_item(self.user, item.id, today=date(2026, 5, 13))

        self.assertEqual(restored.status, "active")
        self.assertEqual(restored.next_due_date, "2026-06-10")

    def test_amount_update_effective_date_changes_future_spending_only(self) -> None:
        item = self.service.add_active(
            user=self.user,
            name="Spotify",
            amount="10",
            currency="SGD",
            start_date=date(2026, 1, 10),
            cadence=Cadence("months", 1),
            today=date(2026, 5, 12),
        )

        self.service.update_amount(self.user, item, "20", date(2026, 4, 10))
        spending = self.service.spending(self.user, today=date(2026, 5, 12))

        self.assertEqual(spending["Since start dates"]["SGD"], Decimal("70"))

    def test_list_line_distinguishes_current_and_next_cycle_amounts(self) -> None:
        item = self.service.add_active(
            user=self.user,
            name="Spotify",
            amount="10.98",
            currency="SGD",
            start_date=date(2026, 1, 10),
            cadence=Cadence("months", 1),
            today=date(2026, 5, 13),
        )

        self.service.update_amount(self.user, item, "13", date(2026, 6, 10))
        updated = self.store.get_item(self.user.id, item.id)
        line = self.service.format_active_line(updated, date(2026, 5, 13))
        spending = self.service.spending(self.user, today=date(2026, 5, 13))

        self.assertIn("renews 2026-06-10 at SGD 13.00", line)
        self.assertIn("current cycle SGD 10.98", line)
        self.assertEqual(spending["Since start dates"]["SGD"], Decimal("54.90"))
        self.assertEqual(spending["Projected future renewals (next 12 months)"]["SGD"], Decimal("156"))

    def test_receipt_uses_effective_amounts(self) -> None:
        item = self.service.add_active(
            user=self.user,
            name="Spotify",
            amount="10.98",
            currency="SGD",
            start_date=date(2026, 1, 10),
            cadence=Cadence("months", 1),
            today=date(2026, 5, 13),
        )

        self.service.update_amount(self.user, item, "13", date(2026, 6, 10))
        receipt = self.service.receipt_text(self.user, today=date(2026, 5, 13))
        future_receipt = self.service.receipt_text(
            self.user,
            today=date(2026, 5, 13),
            mode="upcoming",
            days=30,
        )

        self.assertIn("SUBSHELF RECEIPT", receipt)
        self.assertIn("Spotify", receipt)
        self.assertIn("SGD 10.98", receipt)
        self.assertNotIn("SGD 13.00", receipt)
        self.assertIn("Spotify renews", future_receipt)
        self.assertIn("SGD 13.00", future_receipt)

    def test_reminder_preview_shows_offsets(self) -> None:
        self.service.add_active(
            user=self.user,
            name="Spotify",
            amount="10.98",
            currency="SGD",
            start_date=date(2026, 5, 20),
            cadence=Cadence("months", 1),
            reminder_offsets=[7, 1, 0],
            today=date(2026, 5, 12),
        )

        preview = self.service.reminder_preview(self.user, today=date(2026, 5, 12), days=8)

        self.assertEqual(
            [(entry.reminder_date, entry.due_date, entry.offset_days) for entry in preview],
            [
                (date(2026, 5, 13), date(2026, 5, 20), 7),
                (date(2026, 5, 19), date(2026, 5, 20), 1),
                (date(2026, 5, 20), date(2026, 5, 20), 0),
            ],
        )


class QuickAddTests(unittest.TestCase):
    def test_parse_active_template(self) -> None:
        parsed = parse_active("Spotify 10.98 SGD monthly from 2026-01-10", date(2026, 5, 13))

        self.assertEqual(parsed.name, "Spotify")
        self.assertEqual(parsed.amount, "10.98")
        self.assertEqual(parsed.currency, "SGD")
        self.assertEqual(parsed.cadence, Cadence("months", 1))
        self.assertEqual(parsed.start_date, date(2026, 1, 10))

    def test_parse_trial_template(self) -> None:
        parsed = parse_trial("Canva Pro ends in 7 days then USD 19.99 every 6 months", date(2026, 5, 13))

        self.assertEqual(parsed.name, "Canva Pro")
        self.assertEqual(parsed.amount, "19.99")
        self.assertEqual(parsed.currency, "USD")
        self.assertEqual(parsed.cadence, Cadence("months", 6))
        self.assertEqual(parsed.trial_end_date, date(2026, 5, 20))

    def test_parse_interested_template(self) -> None:
        parsed = parse_interested("Cursor USD 20 yearly", date(2026, 5, 13))

        self.assertEqual(parsed.name, "Cursor")
        self.assertEqual(parsed.amount, "20")
        self.assertEqual(parsed.currency, "USD")
        self.assertEqual(parsed.cadence, Cadence("years", 1))

    def test_pipe_quick_add_still_works(self) -> None:
        parsed = parse_active("Spotify | 10.98 SGD | monthly | from 2026-01-10", date(2026, 5, 13))

        self.assertEqual(parsed.name, "Spotify")
        self.assertEqual(parsed.currency, "SGD")

    def test_quick_add_help_names_required_words(self) -> None:
        help_text = quickadd_help_text()

        self.assertIn("Required word: from", help_text)
        self.assertIn("Required words: ends", help_text)
        self.assertIn("then", help_text)
        self.assertIn("Billing schedule means how often it renews", help_text)
        self.assertNotIn("Cadence can", help_text)

    def test_missing_required_quick_add_word_gets_specific_error(self) -> None:
        with self.assertRaisesRegex(QuickAddError, "Missing required word: from"):
            parse_active("Spotify 10.98 SGD monthly 2026-01-10", date(2026, 5, 13))

        with self.assertRaisesRegex(QuickAddError, "Missing required word: then"):
            parse_trial("Canva ends in 7 days 19.99 SGD monthly", date(2026, 5, 13))


if __name__ == "__main__":
    unittest.main()
