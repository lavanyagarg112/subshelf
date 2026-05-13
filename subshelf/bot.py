from __future__ import annotations

import asyncio
import logging
import warnings
from datetime import date, datetime, timezone
from decimal import InvalidOperation

from .config import AppConfig
from .crypto import FieldCrypto
from .dates import Cadence, format_money
from .quickadd import (
    ACTIVE_TEMPLATE,
    INTERESTED_TEMPLATE,
    TRIAL_TEMPLATE,
    QuickAddError,
    parse_active as parse_quick_active,
    parse_interested as parse_quick_interested,
    parse_trial as parse_quick_trial,
    quickadd_help_text,
)
from .services import (
    DEFAULT_OFFSETS,
    SubShelfService,
    date_from_user_text,
    local_snooze_until_utc,
    local_today,
    normalize_amount,
    normalize_currency,
    parse_reminder_time,
    parse_timezone,
    parse_window_days,
)
from .storage import SQLiteStore, utc_now_iso


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

try:
    from telegram.warnings import PTBUserWarning

    warnings.filterwarnings(
        "ignore",
        message=r"If 'per_message=False', 'CallbackQueryHandler' will not be tracked.*",
        category=PTBUserWarning,
    )
except ModuleNotFoundError:
    pass

(
    OB_CURRENCY,
    OB_TIMEZONE,
    OB_TIMEZONE_OTHER,
    OB_TIME,
    OB_OFFSETS,
    ADD_NAME,
    ADD_AMOUNT,
    ADD_CURRENCY,
    ADD_START,
    ADD_CADENCE,
    ADD_CADENCE_COUNT,
    ADD_OFFSETS,
    TRIAL_NAME,
    TRIAL_END,
    TRIAL_AMOUNT,
    TRIAL_CURRENCY,
    TRIAL_CADENCE,
    TRIAL_CADENCE_COUNT,
    TRIAL_OFFSETS,
    WATCH_NAME,
    WATCH_AMOUNT,
    WATCH_CURRENCY,
    WATCH_CADENCE,
    WATCH_CADENCE_COUNT,
    EDIT_FIELD,
    EDIT_VALUE,
    EDIT_CADENCE,
    EDIT_CADENCE_COUNT,
    EDIT_OFFSETS,
    EDIT_AMOUNT_EFFECTIVE,
    EDIT_AMOUNT_CUSTOM_DATE,
    SETTINGS_FIELD,
    SETTINGS_VALUE,
    SETTINGS_OFFSETS,
) = range(34)


COMMON_CURRENCIES = ["SGD", "USD", "EUR", "GBP", "INR"]
COMMON_TIMEZONES = ["Asia/Singapore", "UTC", "America/New_York", "Europe/London", "Asia/Kolkata"]
COMMON_TIMES = ["09:00", "12:00", "18:00", "21:00"]
OFFSET_LABELS = {
    7: "7 days before",
    3: "3 days before",
    1: "1 day before",
    0: "on the day",
}


def main() -> None:
    config = AppConfig.from_env()
    crypto = FieldCrypto.from_key(config.encryption_key)
    store = SQLiteStore(config.db_path, crypto)
    store.init_db()
    service = SubShelfService(store)

    try:
        from telegram.ext import Application
    except ModuleNotFoundError as exc:
        raise RuntimeError("python-telegram-bot is required. Install dependencies with pip install -r requirements.txt") from exc

    application = (
        Application.builder()
        .token(config.telegram_bot_token)
        .post_init(lambda app: _post_init(app, service))
        .build()
    )
    register_handlers(application, service)
    application.run_polling(allowed_updates=None)


async def _post_init(application, service: SubShelfService) -> None:
    application.create_task(_reminder_loop(application, service))


def register_handlers(application, service: SubShelfService) -> None:
    from telegram.ext import (
        CallbackQueryHandler,
        CommandHandler,
        ConversationHandler,
        MessageHandler,
        filters,
    )

    application.bot_data["service"] = service

    application.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("start", lambda update, context: start(update, context, service))],
            states={
                OB_CURRENCY: [
                    CallbackQueryHandler(lambda u, c: onboarding_currency(u, c, service), pattern=r"^ob_currency:"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: onboarding_currency_text(u, c, service)),
                ],
                OB_TIMEZONE: [
                    CallbackQueryHandler(lambda u, c: onboarding_timezone(u, c, service), pattern=r"^ob_tz:"),
                ],
                OB_TIMEZONE_OTHER: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: onboarding_timezone_text(u, c, service)),
                ],
                OB_TIME: [
                    CallbackQueryHandler(lambda u, c: onboarding_time(u, c, service), pattern=r"^ob_time:"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: onboarding_time_text(u, c, service)),
                ],
                OB_OFFSETS: [
                    CallbackQueryHandler(lambda u, c: onboarding_offsets(u, c, service), pattern=r"^ob_offset:"),
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel_flow)],
        )
    )

    application.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler("add", lambda u, c: add_start(u, c, service)),
                MessageHandler(filters.Regex("^Add subscription$"), lambda u, c: add_start(u, c, service)),
            ],
            states={
                ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
                ADD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_amount)],
                ADD_CURRENCY: [
                    CallbackQueryHandler(add_currency, pattern=r"^currency:"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_currency_text),
                ],
                ADD_START: [
                    CallbackQueryHandler(add_start_date_button, pattern=r"^date_today$"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_start_date_text),
                ],
                ADD_CADENCE: [CallbackQueryHandler(add_cadence, pattern=r"^cadence:")],
                ADD_CADENCE_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_cadence_count)],
                ADD_OFFSETS: [CallbackQueryHandler(lambda u, c: add_offsets(u, c, service), pattern=r"^add_offset:")],
            },
            fallbacks=[CommandHandler("cancel", cancel_flow)],
        )
    )

    application.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler("trial", lambda u, c: trial_start(u, c, service)),
                MessageHandler(filters.Regex("^Add trial$"), lambda u, c: trial_start(u, c, service)),
            ],
            states={
                TRIAL_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, trial_name)],
                TRIAL_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, trial_end)],
                TRIAL_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, trial_amount)],
                TRIAL_CURRENCY: [
                    CallbackQueryHandler(trial_currency, pattern=r"^currency:"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, trial_currency_text),
                ],
                TRIAL_CADENCE: [CallbackQueryHandler(trial_cadence, pattern=r"^cadence:")],
                TRIAL_CADENCE_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, trial_cadence_count)],
                TRIAL_OFFSETS: [CallbackQueryHandler(lambda u, c: trial_offsets(u, c, service), pattern=r"^trial_offset:")],
            },
            fallbacks=[CommandHandler("cancel", cancel_flow)],
        )
    )

    application.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler("interested", lambda u, c: watch_start(u, c, service)),
                MessageHandler(filters.Regex("^Add interested$"), lambda u, c: watch_start(u, c, service)),
            ],
            states={
                WATCH_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, watch_name)],
                WATCH_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, watch_amount)],
                WATCH_CURRENCY: [
                    CallbackQueryHandler(watch_currency, pattern=r"^currency:"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, watch_currency_text),
                ],
                WATCH_CADENCE: [CallbackQueryHandler(watch_cadence, pattern=r"^cadence:")],
                WATCH_CADENCE_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, watch_cadence_count)],
            },
            fallbacks=[CommandHandler("cancel", cancel_flow)],
        )
    )

    application.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(lambda u, c: edit_start(u, c, service), pattern=r"^edit:\d+$")],
            states={
                EDIT_FIELD: [CallbackQueryHandler(lambda u, c: edit_field(u, c, service), pattern=r"^editfield:")],
                EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: edit_value(u, c, service))],
                EDIT_CADENCE: [CallbackQueryHandler(lambda u, c: edit_cadence(u, c, service), pattern=r"^cadence:")],
                EDIT_CADENCE_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: edit_cadence_count(u, c, service))],
                EDIT_OFFSETS: [CallbackQueryHandler(lambda u, c: edit_offsets(u, c, service), pattern=r"^edit_offset:")],
                EDIT_AMOUNT_EFFECTIVE: [CallbackQueryHandler(lambda u, c: edit_amount_effective(u, c, service), pattern=r"^amounteff:")],
                EDIT_AMOUNT_CUSTOM_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: edit_amount_custom_date(u, c, service))],
            },
            fallbacks=[CommandHandler("cancel", cancel_flow)],
        )
    )

    application.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler("settings", lambda u, c: settings_start(u, c, service)),
                MessageHandler(filters.Regex("^Settings$"), lambda u, c: settings_start(u, c, service)),
            ],
            states={
                SETTINGS_FIELD: [
                    CallbackQueryHandler(lambda u, c: settings_field(u, c, service), pattern=r"^settings:")
                ],
                SETTINGS_VALUE: [
                    CallbackQueryHandler(
                        lambda u, c: settings_value(u, c, service),
                        pattern=r"^(settings_currency|settings_tz|settings_time):",
                    ),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: settings_value(u, c, service)),
                ],
                SETTINGS_OFFSETS: [
                    CallbackQueryHandler(lambda u, c: settings_offsets(u, c, service), pattern=r"^settings_offset:")
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel_flow)],
        )
    )

    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("quickadd", quickadd_help_command))
    application.add_handler(MessageHandler(filters.Regex("^Quick add help$"), quickadd_help_command))
    application.add_handler(CommandHandler("list", lambda u, c: list_command(u, c, service)))
    application.add_handler(MessageHandler(filters.Regex("^View all$"), lambda u, c: list_command(u, c, service)))
    application.add_handler(CommandHandler("search", lambda u, c: search_command(u, c, service)))
    application.add_handler(CommandHandler("upcoming", lambda u, c: upcoming_command(u, c, service)))
    application.add_handler(MessageHandler(filters.Regex("^Upcoming$"), lambda u, c: upcoming_command(u, c, service)))
    application.add_handler(CommandHandler("reminders", lambda u, c: reminders_command(u, c, service)))
    application.add_handler(CommandHandler("test_reminder", lambda u, c: test_reminder_command(u, c, service)))
    application.add_handler(CommandHandler("spending", lambda u, c: spending_command(u, c, service)))
    application.add_handler(MessageHandler(filters.Regex("^Spending$"), lambda u, c: spending_command(u, c, service)))
    application.add_handler(CommandHandler("receipt", lambda u, c: receipt_command(u, c, service)))
    application.add_handler(CallbackQueryHandler(lambda u, c: list_manage_action(u, c, service), pattern=r"^(listcat|manageitem):"))
    application.add_handler(CallbackQueryHandler(lambda u, c: item_action(u, c, service), pattern=r"^(details|cancelitem|restoreitem|deleteitem|convert|trialcontinue|trialcancel|snooze):"))


async def start(update, context, service: SubShelfService):
    from telegram.ext import ConversationHandler

    user = service.store.get_user_by_telegram_id(update.effective_user.id)
    if user:
        await update.message.reply_text(
            "SubShelf is ready. Use the menu, guided commands, or /quickadd for one-line templates.",
            reply_markup=main_menu_markup(),
        )
        return ConversationHandler.END

    context.user_data.clear()
    await update.message.reply_text("Choose your default currency.", reply_markup=currency_markup("ob_currency"))
    return OB_CURRENCY


async def onboarding_currency(update, context, service: SubShelfService):
    query = update.callback_query
    await query.answer()
    value = query.data.split(":", 1)[1]
    if value == "other":
        await query.edit_message_text("Type your currency code, such as SGD or USD.")
        return OB_CURRENCY
    context.user_data["default_currency"] = value
    await query.edit_message_text("Choose your timezone.", reply_markup=timezone_markup())
    return OB_TIMEZONE


async def onboarding_currency_text(update, context, service: SubShelfService):
    try:
        context.user_data["default_currency"] = normalize_currency(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return OB_CURRENCY
    await update.message.reply_text("Choose your timezone.", reply_markup=timezone_markup())
    return OB_TIMEZONE


async def onboarding_timezone(update, context, service: SubShelfService):
    query = update.callback_query
    await query.answer()
    value = query.data.split(":", 1)[1]
    if value == "other":
        await query.edit_message_text("Type an IANA timezone, such as Asia/Singapore.")
        return OB_TIMEZONE_OTHER
    context.user_data["timezone"] = value
    await query.edit_message_text("Choose your default reminder time.", reply_markup=time_markup("ob_time"))
    return OB_TIME


async def onboarding_timezone_text(update, context, service: SubShelfService):
    try:
        context.user_data["timezone"] = parse_timezone(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return OB_TIMEZONE_OTHER
    await update.message.reply_text("Choose your default reminder time.", reply_markup=time_markup("ob_time"))
    return OB_TIME


async def onboarding_time(update, context, service: SubShelfService):
    query = update.callback_query
    await query.answer()
    context.user_data["reminder_time"] = query.data.split(":", 1)[1]
    context.user_data["offsets"] = set(DEFAULT_OFFSETS)
    await query.edit_message_text("Choose reminder offsets.", reply_markup=offset_markup("ob_offset", context.user_data["offsets"]))
    return OB_OFFSETS


async def onboarding_time_text(update, context, service: SubShelfService):
    try:
        context.user_data["reminder_time"] = parse_reminder_time(update.message.text)
    except ValueError:
        await update.message.reply_text("Use HH:MM, such as 09:00.")
        return OB_TIME
    context.user_data["offsets"] = set(DEFAULT_OFFSETS)
    await update.message.reply_text("Choose reminder offsets.", reply_markup=offset_markup("ob_offset", context.user_data["offsets"]))
    return OB_OFFSETS


async def onboarding_offsets(update, context, service: SubShelfService):
    from telegram.ext import ConversationHandler

    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    offsets = context.user_data.setdefault("offsets", set(DEFAULT_OFFSETS))
    if action == "done":
        if not offsets:
            offsets.add(0)
        user = service.store.create_or_update_user(
            telegram_user_id=update.effective_user.id,
            telegram_chat_id=update.effective_chat.id,
            default_currency=context.user_data["default_currency"],
            timezone_name=context.user_data["timezone"],
            reminder_time=context.user_data["reminder_time"],
            reminder_offsets=sorted(offsets, reverse=True),
        )
        await query.edit_message_text(
            f"Onboarding complete. Default currency: {user.default_currency}.",
        )
        await query.message.reply_text("Use the menu to start tracking.", reply_markup=main_menu_markup())
        return ConversationHandler.END

    offsets.symmetric_difference_update({int(action)})
    await query.edit_message_reply_markup(reply_markup=offset_markup("ob_offset", offsets))
    return OB_OFFSETS


async def add_start(update, context, service: SubShelfService):
    from telegram.ext import ConversationHandler

    user = await require_user(update, service)
    if not user:
        return ConversationHandler.END
    if context.args:
        try:
            parsed = parse_quick_active(_command_args(update), local_today(user))
        except (QuickAddError, ValueError, InvalidOperation) as exc:
            await update.message.reply_text(f"{exc}\n\n{quickadd_help_text()}")
            return ConversationHandler.END
        item = service.add_active(
            user=user,
            name=parsed.name,
            amount=parsed.amount,
            currency=parsed.currency,
            start_date=parsed.start_date,
            cadence=parsed.cadence,
        )
        await update.message.reply_text(
            f"Added {item.name}. Next renewal: {item.next_due_date}.",
            reply_markup=main_menu_markup(),
        )
        return ConversationHandler.END
    context.user_data["flow"] = {"telegram_user_id": update.effective_user.id}
    await update.message.reply_text("Subscription name?")
    return ADD_NAME


async def add_name(update, context):
    context.user_data["flow"]["name"] = update.message.text.strip()
    await update.message.reply_text("Amount?")
    return ADD_AMOUNT


async def add_amount(update, context):
    try:
        context.user_data["flow"]["amount"] = normalize_amount(update.message.text)
    except (InvalidOperation, ValueError):
        await update.message.reply_text("Use a valid amount, such as 10.98.")
        return ADD_AMOUNT
    await update.message.reply_text("Currency?", reply_markup=currency_markup("currency"))
    return ADD_CURRENCY


async def add_currency(update, context):
    query = update.callback_query
    await query.answer()
    value = query.data.split(":", 1)[1]
    if value == "other":
        await query.edit_message_text("Type the currency code.")
        return ADD_CURRENCY
    context.user_data["flow"]["currency"] = value
    await query.edit_message_text("Start date? Use YYYY-MM-DD, today, tomorrow, or in 7 days.", reply_markup=today_markup())
    return ADD_START


async def add_currency_text(update, context):
    try:
        context.user_data["flow"]["currency"] = normalize_currency(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return ADD_CURRENCY
    await update.message.reply_text("Start date? Use YYYY-MM-DD, today, tomorrow, or in 7 days.", reply_markup=today_markup())
    return ADD_START


async def add_start_date_button(update, context):
    query = update.callback_query
    await query.answer()
    service: SubShelfService = context.application.bot_data["service"]
    user = service.store.get_user_by_telegram_id(update.effective_user.id)
    context.user_data["flow"]["start_date"] = local_today(user)
    await query.edit_message_text("How often does it renew?", reply_markup=cadence_markup())
    return ADD_CADENCE


async def add_start_date_text(update, context):
    user = context.application.bot_data["service"].store.get_user_by_telegram_id(update.effective_user.id)
    try:
        context.user_data["flow"]["start_date"] = date_from_user_text(update.message.text, user)
    except ValueError:
        await update.message.reply_text("Use YYYY-MM-DD, today, tomorrow, or in 7 days.")
        return ADD_START
    await update.message.reply_text("How often does it renew?", reply_markup=cadence_markup())
    return ADD_CADENCE


async def add_cadence(update, context):
    return await cadence_choice(update, context, ADD_CADENCE_COUNT, ADD_OFFSETS, "add_offset")


async def add_cadence_count(update, context):
    return await cadence_count(update, context, ADD_OFFSETS, "add_offset")


async def add_offsets(update, context, service: SubShelfService):
    from telegram.ext import ConversationHandler

    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    offsets = context.user_data.setdefault("offsets", set(DEFAULT_OFFSETS))
    if action != "done":
        offsets.symmetric_difference_update({int(action)})
        await query.edit_message_reply_markup(reply_markup=offset_markup("add_offset", offsets))
        return ADD_OFFSETS

    user = service.store.get_user_by_telegram_id(update.effective_user.id)
    flow = context.user_data["flow"]
    item = service.add_active(
        user=user,
        name=flow["name"],
        amount=flow["amount"],
        currency=flow["currency"],
        start_date=flow["start_date"],
        cadence=flow["cadence"],
        reminder_offsets=sorted(offsets, reverse=True),
    )
    await query.edit_message_text(f"Added {item.name}. Next renewal: {item.next_due_date}.")
    await query.message.reply_text("Done.", reply_markup=main_menu_markup())
    return ConversationHandler.END


async def trial_start(update, context, service: SubShelfService):
    from telegram.ext import ConversationHandler

    user = await require_user(update, service)
    if not user:
        return ConversationHandler.END
    if context.args:
        try:
            parsed = parse_quick_trial(_command_args(update), local_today(user))
        except (QuickAddError, ValueError, InvalidOperation) as exc:
            await update.message.reply_text(f"{exc}\n\n{quickadd_help_text()}")
            return ConversationHandler.END
        item = service.add_trial(
            user=user,
            name=parsed.name,
            trial_end_date=parsed.trial_end_date,
            paid_amount=parsed.amount,
            currency=parsed.currency,
            paid_cadence=parsed.cadence,
        )
        await update.message.reply_text(
            f"Added trial {item.name}. Trial ends: {item.trial_end_date}.",
            reply_markup=main_menu_markup(),
        )
        return ConversationHandler.END
    context.user_data["flow"] = {"telegram_user_id": update.effective_user.id}
    await update.message.reply_text("Trial name?")
    return TRIAL_NAME


async def trial_name(update, context):
    context.user_data["flow"]["name"] = update.message.text.strip()
    await update.message.reply_text("Trial end date? Use YYYY-MM-DD, tomorrow, or in 7 days.")
    return TRIAL_END


async def trial_end(update, context):
    user = context.application.bot_data["service"].store.get_user_by_telegram_id(update.effective_user.id)
    try:
        context.user_data["flow"]["trial_end_date"] = date_from_user_text(update.message.text, user)
    except ValueError:
        await update.message.reply_text("Use YYYY-MM-DD, tomorrow, or in 7 days.")
        return TRIAL_END
    await update.message.reply_text("Paid amount after trial?")
    return TRIAL_AMOUNT


async def trial_amount(update, context):
    try:
        context.user_data["flow"]["amount"] = normalize_amount(update.message.text)
    except (InvalidOperation, ValueError):
        await update.message.reply_text("Use a valid amount, such as 19.99.")
        return TRIAL_AMOUNT
    await update.message.reply_text("Currency?", reply_markup=currency_markup("currency"))
    return TRIAL_CURRENCY


async def trial_currency(update, context):
    query = update.callback_query
    await query.answer()
    value = query.data.split(":", 1)[1]
    if value == "other":
        await query.edit_message_text("Type the currency code.")
        return TRIAL_CURRENCY
    context.user_data["flow"]["currency"] = value
    await query.edit_message_text("If it continues, how often will it renew?", reply_markup=cadence_markup())
    return TRIAL_CADENCE


async def trial_currency_text(update, context):
    try:
        context.user_data["flow"]["currency"] = normalize_currency(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return TRIAL_CURRENCY
    await update.message.reply_text("If it continues, how often will it renew?", reply_markup=cadence_markup())
    return TRIAL_CADENCE


async def trial_cadence(update, context):
    return await cadence_choice(update, context, TRIAL_CADENCE_COUNT, TRIAL_OFFSETS, "trial_offset")


async def trial_cadence_count(update, context):
    return await cadence_count(update, context, TRIAL_OFFSETS, "trial_offset")


async def trial_offsets(update, context, service: SubShelfService):
    from telegram.ext import ConversationHandler

    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    offsets = context.user_data.setdefault("offsets", set(DEFAULT_OFFSETS))
    if action != "done":
        offsets.symmetric_difference_update({int(action)})
        await query.edit_message_reply_markup(reply_markup=offset_markup("trial_offset", offsets))
        return TRIAL_OFFSETS

    user = service.store.get_user_by_telegram_id(update.effective_user.id)
    flow = context.user_data["flow"]
    item = service.add_trial(
        user=user,
        name=flow["name"],
        trial_end_date=flow["trial_end_date"],
        paid_amount=flow["amount"],
        currency=flow["currency"],
        paid_cadence=flow["cadence"],
        reminder_offsets=sorted(offsets, reverse=True),
    )
    await query.edit_message_text(f"Added trial {item.name}. Trial ends: {item.trial_end_date}.")
    await query.message.reply_text("Done.", reply_markup=main_menu_markup())
    return ConversationHandler.END


async def watch_start(update, context, service: SubShelfService):
    from telegram.ext import ConversationHandler

    user = await require_user(update, service)
    if not user:
        return ConversationHandler.END
    if context.args:
        try:
            parsed = parse_quick_interested(_command_args(update), local_today(user))
        except (QuickAddError, ValueError, InvalidOperation) as exc:
            await update.message.reply_text(f"{exc}\n\n{quickadd_help_text()}")
            return ConversationHandler.END
        item = service.add_interested(
            user=user,
            name=parsed.name,
            expected_amount=parsed.amount,
            currency=parsed.currency,
            cadence=parsed.cadence,
        )
        await update.message.reply_text(
            f"Added {item.name} to your watchlist.",
            reply_markup=main_menu_markup(),
        )
        return ConversationHandler.END
    context.user_data["flow"] = {"telegram_user_id": update.effective_user.id}
    await update.message.reply_text("Interested subscription name?")
    return WATCH_NAME


async def watch_name(update, context):
    context.user_data["flow"]["name"] = update.message.text.strip()
    await update.message.reply_text("Expected amount?")
    return WATCH_AMOUNT


async def watch_amount(update, context):
    try:
        context.user_data["flow"]["amount"] = normalize_amount(update.message.text)
    except (InvalidOperation, ValueError):
        await update.message.reply_text("Use a valid amount, such as 20.")
        return WATCH_AMOUNT
    await update.message.reply_text("Currency?", reply_markup=currency_markup("currency"))
    return WATCH_CURRENCY


async def watch_currency(update, context):
    query = update.callback_query
    await query.answer()
    value = query.data.split(":", 1)[1]
    if value == "other":
        await query.edit_message_text("Type the currency code.")
        return WATCH_CURRENCY
    context.user_data["flow"]["currency"] = value
    await query.edit_message_text("How often would it renew?", reply_markup=cadence_markup())
    return WATCH_CADENCE


async def watch_currency_text(update, context):
    try:
        context.user_data["flow"]["currency"] = normalize_currency(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return WATCH_CURRENCY
    await update.message.reply_text("How often would it renew?", reply_markup=cadence_markup())
    return WATCH_CADENCE


async def watch_cadence(update, context):
    query = update.callback_query
    await query.answer()
    value = query.data.split(":", 1)[1]
    if value == "monthly":
        context.user_data["flow"]["cadence"] = Cadence("months", 1)
    elif value == "yearly":
        context.user_data["flow"]["cadence"] = Cadence("years", 1)
    else:
        context.user_data["pending_cadence_unit"] = "months" if value == "x_months" else "days"
        await query.edit_message_text(f"How many {context.user_data['pending_cadence_unit']}?")
        return WATCH_CADENCE_COUNT
    return await save_watch(query, context)


async def watch_cadence_count(update, context):
    try:
        count = int(update.message.text)
        context.user_data["flow"]["cadence"] = Cadence(context.user_data["pending_cadence_unit"], count)
    except ValueError:
        await update.message.reply_text("Use a positive whole number.")
        return WATCH_CADENCE_COUNT
    return await save_watch(update.message, context)


async def save_watch(message_or_query, context):
    from telegram.ext import ConversationHandler

    service: SubShelfService = context.application.bot_data["service"]
    flow = context.user_data["flow"]
    user = service.store.get_user_by_telegram_id(flow["telegram_user_id"])
    item = service.add_interested(
        user=user,
        name=flow["name"],
        expected_amount=flow["amount"],
        currency=flow["currency"],
        cadence=flow["cadence"],
    )
    text = f"Added {item.name} to your watchlist."
    if hasattr(message_or_query, "edit_message_text"):
        await message_or_query.edit_message_text(text)
        await message_or_query.message.reply_text("Done.", reply_markup=main_menu_markup())
    else:
        await message_or_query.reply_text(text, reply_markup=main_menu_markup())
    return ConversationHandler.END


async def list_command(update, context, service: SubShelfService):
    user = await require_user(update, service)
    if not user:
        return
    if context.args:
        category = normalize_list_category(context.args[0])
        if category is None:
            await update.message.reply_text("Use /list active, /list trials, /list watchlist, or /list cancelled.")
            return
        await update.message.reply_text(
            manage_category_text(service, user, category),
            reply_markup=category_items_markup(service, user, category),
        )
        return
    await update.message.reply_text(service.list_text(user), reply_markup=list_overview_markup(service, user))


async def search_command(update, context, service: SubShelfService):
    user = await require_user(update, service)
    if not user:
        return
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Use /search spotify or /search netflix.")
        return
    matches = service.search_items(user, query)
    if not matches:
        await update.message.reply_text(f"No matches for {query}.")
        return
    await update.message.reply_text(search_results_text(matches, query), reply_markup=search_results_markup(matches))


async def upcoming_command(update, context, service: SubShelfService):
    user = await require_user(update, service)
    if not user:
        return
    try:
        days = parse_window_days(context.args[0] if context.args else None)
    except (ValueError, IndexError):
        await update.message.reply_text("Use /upcoming, /upcoming 7, /upcoming 14, or /upcoming 30.")
        return
    await update.message.reply_text(service.upcoming_text(user, today=local_today(user), days=days))


async def spending_command(update, context, service: SubShelfService):
    user = await require_user(update, service)
    if not user:
        return
    await update.message.reply_text(service.spending_text(user, today=local_today(user)))


async def receipt_command(update, context, service: SubShelfService):
    user = await require_user(update, service)
    if not user:
        return
    mode = context.args[0].lower() if context.args else "month"
    days = 30
    if mode not in {"month", "year", "all", "upcoming"}:
        await update.message.reply_text("Use /receipt, /receipt year, /receipt all, or /receipt upcoming 30.")
        return
    if mode == "upcoming" and len(context.args) > 1:
        try:
            days = parse_window_days(context.args[1])
        except ValueError:
            await update.message.reply_text("Use /receipt upcoming 7, /receipt upcoming 30, or /receipt upcoming 90.")
            return
    await update.message.reply_text(service.receipt_text(user, today=local_today(user), mode=mode, days=days))


async def reminders_command(update, context, service: SubShelfService):
    user = await require_user(update, service)
    if not user:
        return
    try:
        days = parse_window_days(context.args[0] if context.args else None)
    except (ValueError, IndexError):
        await update.message.reply_text("Use /reminders, /reminders 7, /reminders 14, or /reminders 30.")
        return
    await update.message.reply_text(service.reminders_text(user, today=local_today(user), days=days))


async def test_reminder_command(update, context, service: SubShelfService):
    user = await require_user(update, service)
    if not user:
        return
    days = 365
    entries = service.reminder_preview(user, today=local_today(user), days=days)
    if context.args:
        try:
            item_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Use /test_reminder or /test_reminder ITEM_ID.")
            return
        entries = [entry for entry in entries if entry.item.id == item_id]
    if not entries:
        await update.message.reply_text("No upcoming reminder to preview.")
        return
    await update.message.reply_text(_test_reminder_text(entries[0]))


async def settings_start(update, context, service: SubShelfService):
    user = await require_user(update, service)
    if not user:
        return -1
    context.user_data["settings_user_id"] = update.effective_user.id
    await update.message.reply_text(settings_text(user), reply_markup=settings_markup())
    return SETTINGS_FIELD


async def settings_field(update, context, service: SubShelfService):
    from telegram.ext import ConversationHandler

    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    if action == "done":
        await query.edit_message_text("Settings closed.")
        return ConversationHandler.END

    context.user_data["settings_field"] = action
    if action == "currency":
        await query.edit_message_text("Choose your default currency, or type a currency code.", reply_markup=currency_markup("settings_currency"))
        return SETTINGS_VALUE
    if action == "timezone":
        await query.edit_message_text("Choose your timezone, or type an IANA timezone.", reply_markup=timezone_markup("settings_tz"))
        return SETTINGS_VALUE
    if action == "time":
        await query.edit_message_text("Choose your default reminder time, or type HH:MM.", reply_markup=time_markup("settings_time"))
        return SETTINGS_VALUE
    if action == "offsets":
        user = service.store.get_user_by_telegram_id(update.effective_user.id)
        context.user_data["offsets"] = set(user.default_reminder_offsets)
        await query.edit_message_text("Choose your default reminder offsets.", reply_markup=offset_markup("settings_offset", context.user_data["offsets"]))
        return SETTINGS_OFFSETS

    await query.edit_message_text("Unknown settings action.")
    return ConversationHandler.END


async def settings_value(update, context, service: SubShelfService):
    user = service.store.get_user_by_telegram_id(context.user_data["settings_user_id"])
    field = context.user_data["settings_field"]
    try:
        if update.callback_query:
            query = update.callback_query
            await query.answer()
            value = query.data.split(":", 1)[1]
            if value == "other":
                await query.edit_message_text(_settings_other_prompt(field))
                return SETTINGS_VALUE
        else:
            query = None
            value = update.message.text

        updates = _settings_update_for_value(field, value)
    except (ValueError, InvalidOperation) as exc:
        target = update.callback_query.message if update.callback_query else update.message
        await target.reply_text(str(exc) if str(exc) else "That value does not look valid. Try again.")
        return SETTINGS_VALUE

    service.store.update_user_settings(user.id, **updates)
    fresh_user = service.store.get_user_by_telegram_id(context.user_data["settings_user_id"])
    if update.callback_query:
        await update.callback_query.edit_message_text(settings_text(fresh_user), reply_markup=settings_markup())
    else:
        await update.message.reply_text(settings_text(fresh_user), reply_markup=settings_markup())
    return SETTINGS_FIELD


async def settings_offsets(update, context, service: SubShelfService):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    offsets = context.user_data.setdefault("offsets", set(DEFAULT_OFFSETS))
    if action != "done":
        offsets.symmetric_difference_update({int(action)})
        await query.edit_message_reply_markup(reply_markup=offset_markup("settings_offset", offsets))
        return SETTINGS_OFFSETS

    if not offsets:
        offsets.add(0)
    user = service.store.get_user_by_telegram_id(context.user_data["settings_user_id"])
    service.store.update_user_settings(user.id, reminder_offsets=sorted(offsets, reverse=True))
    fresh_user = service.store.get_user_by_telegram_id(context.user_data["settings_user_id"])
    await query.edit_message_text(settings_text(fresh_user), reply_markup=settings_markup())
    return SETTINGS_FIELD


def settings_text(user) -> str:
    offsets = ", ".join(OFFSET_LABELS.get(offset, f"{offset} days before") for offset in user.default_reminder_offsets)
    return "\n".join(
        [
            "Settings",
            f"Default currency: {user.default_currency}",
            f"Timezone: {user.timezone}",
            f"Reminder time: {user.default_reminder_time}",
            f"Reminder offsets: {offsets}",
        ]
    )


async def help_command(update, context):
    await update.message.reply_text(
        "\n".join(
            [
                "SubShelf commands",
                "/add - add an active subscription",
                "/trial - add a trial",
                "/interested - add something to the watchlist",
                "/quickadd - show one-line templates",
                "/list - view everything",
                "/list active - only active subscriptions",
                "/list trials - only trials",
                "/list watchlist - only watchlist",
                "/list cancelled - cancelled items",
                "/search spotify - find matching items",
                "/upcoming 7 - upcoming renewals",
                "/reminders 30 - preview scheduled reminders",
                "/test_reminder - preview the next reminder message",
                "/spending - spending totals",
                "/receipt - receipt-style summary for this month",
                "/receipt upcoming 30 - receipt-style future renewals",
                "/settings - current settings",
                "/cancel - cancel the current guided flow",
                "",
                "Quick add example",
                ACTIVE_TEMPLATE,
            ]
        ),
        reply_markup=main_menu_markup(),
    )


async def quickadd_help_command(update, context):
    await update.message.reply_text(quickadd_help_text(), reply_markup=main_menu_markup())


async def list_manage_action(update, context, service: SubShelfService):
    query = update.callback_query
    await query.answer()
    user = service.store.get_user_by_telegram_id(update.effective_user.id)
    if not user:
        await query.edit_message_text("Use /start first.")
        return

    action, payload = query.data.split(":", 1)
    if action == "listcat" and payload == "summary":
        await query.edit_message_text(service.list_text(user), reply_markup=list_overview_markup(service, user))
        return
    if action == "listcat":
        await query.edit_message_text(
            manage_category_text(service, user, payload),
            reply_markup=category_items_markup(service, user, payload),
        )
        return

    item_id_text, _, category = payload.partition(":")
    item = service.store.get_item(user.id, int(item_id_text))
    if not item:
        await query.edit_message_text("Item not found.")
        return
    await query.edit_message_text(item_details(service, user, item), reply_markup=single_item_markup(item, back_category=category or None))


async def item_action(update, context, service: SubShelfService):
    query = update.callback_query
    await query.answer()
    user = service.store.get_user_by_telegram_id(update.effective_user.id)
    if not user:
        await query.edit_message_text("Use /start first.")
        return
    action, raw_item_id = query.data.split(":", 1)
    item_id = int(raw_item_id)
    item = service.store.get_item(user.id, item_id)
    if not item:
        await query.edit_message_text("Item not found.")
        return

    if action == "details":
        await query.edit_message_text(item_details(service, user, item), reply_markup=single_item_markup(item))
    elif action == "cancelitem":
        if item.status == "cancelled":
            await query.edit_message_text("This item is already cancelled.")
            return
        updated, savings = service.cancel_item(user, item_id)
        if not updated:
            await query.edit_message_text("Item not found.")
        elif item.type == "trial":
            await query.edit_message_text(f"Cancelled trial: {updated.name}.")
        elif item.type == "interested":
            await query.edit_message_text(f"Stopped watching: {updated.name}.")
        elif savings is None or not updated.currency:
            await query.edit_message_text(f"Cancelled subscription: {updated.name}.")
        else:
            await query.edit_message_text(
                f"Cancelled subscription: {updated.name}. Estimated yearly savings: {updated.currency} {format_money(savings)}."
            )
    elif action == "deleteitem":
        service.store.update_item(user.id, item_id, status="deleted")
        await query.edit_message_text("Deleted.")
    elif action == "restoreitem":
        restored = service.restore_item(user, item_id, today=local_today(user))
        if restored:
            await query.edit_message_text(item_details(service, user, restored), reply_markup=single_item_markup(restored))
        else:
            await query.edit_message_text("Item not found.")
    elif action == "convert":
        converted = service.convert_interested_to_active(user, item_id, today=local_today(user))
        await query.edit_message_text(
            f"Converted {converted.name} to active. Next renewal: {converted.next_due_date}."
            if converted
            else "Could not convert that item."
        )
    elif action == "trialcontinue":
        converted = service.continue_trial(user, item_id, today=local_today(user))
        await query.edit_message_text(
            f"{converted.name} is now active. Next renewal: {converted.next_due_date}."
            if converted
            else "Could not continue that trial."
        )
    elif action == "trialcancel":
        service.store.update_item(user.id, item_id, status="cancelled")
        await query.edit_message_text(f"Cancelled trial: {item.name}.")
    elif action == "snooze":
        service.store.create_snooze(item.id, local_snooze_until_utc(user))
        await query.edit_message_text(f"Will remind you tomorrow about {item.name}.")


async def edit_start(update, context, service: SubShelfService):
    query = update.callback_query
    await query.answer()
    item_id = int(query.data.split(":", 1)[1])
    user = service.store.get_user_by_telegram_id(update.effective_user.id)
    item = service.store.get_item(user.id, item_id) if user else None
    if not item:
        await query.edit_message_text("Item not found.")
        return -1
    context.user_data["edit_item_id"] = item_id
    context.user_data["edit_user_id"] = update.effective_user.id
    await query.edit_message_text(f"Edit {item.name}.", reply_markup=edit_field_markup(item))
    return EDIT_FIELD


async def edit_field(update, context, service: SubShelfService):
    query = update.callback_query
    await query.answer()
    field = query.data.split(":", 1)[1]
    context.user_data["edit_field"] = field
    if field == "cadence":
        await query.edit_message_text("Choose the new billing schedule.", reply_markup=cadence_markup())
        return EDIT_CADENCE
    if field == "reminders":
        user = service.store.get_user_by_telegram_id(update.effective_user.id)
        item = service.store.get_item(user.id, context.user_data["edit_item_id"])
        context.user_data["offsets"] = set(item.reminder_offsets)
        await query.edit_message_text("Choose reminder offsets.", reply_markup=offset_markup("edit_offset", context.user_data["offsets"]))
        return EDIT_OFFSETS
    prompt = {
        "name": "New name?",
        "amount": "New amount?",
        "currency": "New currency?",
        "date": "New date? For active items this is the start date; for trials this is the trial end date.",
    }[field]
    await query.edit_message_text(prompt)
    return EDIT_VALUE


async def edit_value(update, context, service: SubShelfService):
    from telegram.ext import ConversationHandler

    user = service.store.get_user_by_telegram_id(update.effective_user.id)
    item = service.store.get_item(user.id, context.user_data["edit_item_id"])
    field = context.user_data["edit_field"]
    value = update.message.text
    updates = {}
    try:
        if field == "name":
            updates["name"] = value.strip()
        elif field == "amount":
            context.user_data["pending_amount"] = normalize_amount(value)
            context.user_data["pending_amount_item_id"] = item.id
            await update.message.reply_text(
                "When should this new amount apply?",
                reply_markup=amount_effective_markup(item),
            )
            return EDIT_AMOUNT_EFFECTIVE
        elif field == "currency":
            updates["currency"] = normalize_currency(value)
        elif field == "date":
            parsed = date_from_user_text(value, user)
            if item.type == "trial":
                updates["trial_end_date"] = parsed.isoformat()
                updates["next_due_date"] = parsed.isoformat()
            elif item.type == "active" and item.cadence:
                updates["start_date"] = parsed.isoformat()
                updates["next_due_date"] = service_next_due(item.cadence, parsed, local_today(user))
    except (ValueError, InvalidOperation):
        await update.message.reply_text("That value does not look valid. Try again.")
        return EDIT_VALUE
    updated = service.store.update_item(user.id, item.id, **updates)
    await update.message.reply_text(f"Updated {updated.name}.", reply_markup=main_menu_markup())
    return ConversationHandler.END


async def edit_amount_effective(update, context, service: SubShelfService):
    from telegram.ext import ConversationHandler

    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]
    if choice == "custom":
        await query.edit_message_text("Custom effective date? Use YYYY-MM-DD, today, tomorrow, or in 7 days.")
        return EDIT_AMOUNT_CUSTOM_DATE

    user = service.store.get_user_by_telegram_id(update.effective_user.id)
    item = service.store.get_item(user.id, context.user_data["pending_amount_item_id"])
    try:
        effective_date = service.amount_effective_date(item, choice, local_today(user))
    except ValueError:
        await query.edit_message_text("Could not apply that amount change.")
        return ConversationHandler.END
    updated = service.update_amount(user, item, context.user_data["pending_amount"], effective_date)
    await query.edit_message_text(
        f"Updated amount for {updated.name} from {effective_date.isoformat()}."
    )
    await query.message.reply_text("Done.", reply_markup=main_menu_markup())
    return ConversationHandler.END


async def edit_amount_custom_date(update, context, service: SubShelfService):
    from telegram.ext import ConversationHandler

    user = service.store.get_user_by_telegram_id(update.effective_user.id)
    item = service.store.get_item(user.id, context.user_data["pending_amount_item_id"])
    try:
        effective_date = date_from_user_text(update.message.text, user)
    except ValueError:
        await update.message.reply_text("Use YYYY-MM-DD, today, tomorrow, or in 7 days.")
        return EDIT_AMOUNT_CUSTOM_DATE
    updated = service.update_amount(user, item, context.user_data["pending_amount"], effective_date)
    await update.message.reply_text(
        f"Updated amount for {updated.name} from {effective_date.isoformat()}.",
        reply_markup=main_menu_markup(),
    )
    return ConversationHandler.END


async def edit_cadence(update, context, service: SubShelfService):
    query = update.callback_query
    await query.answer()
    value = query.data.split(":", 1)[1]
    if value == "monthly":
        context.user_data["edit_cadence"] = Cadence("months", 1)
    elif value == "yearly":
        context.user_data["edit_cadence"] = Cadence("years", 1)
    else:
        context.user_data["pending_cadence_unit"] = "months" if value == "x_months" else "days"
        await query.edit_message_text(f"How many {context.user_data['pending_cadence_unit']}?")
        return EDIT_CADENCE_COUNT
    return await save_edit_cadence(query, context, service)


async def edit_cadence_count(update, context, service: SubShelfService):
    try:
        context.user_data["edit_cadence"] = Cadence(context.user_data["pending_cadence_unit"], int(update.message.text))
    except ValueError:
        await update.message.reply_text("Use a positive whole number.")
        return EDIT_CADENCE_COUNT
    return await save_edit_cadence(update.message, context, service)


async def save_edit_cadence(message_or_query, context, service: SubShelfService):
    from telegram.ext import ConversationHandler

    user = service.store.get_user_by_telegram_id(context.user_data["edit_user_id"])
    item = service.store.get_item(user.id, context.user_data["edit_item_id"])
    cadence = context.user_data["edit_cadence"]
    updates = {"cadence_unit": cadence.unit, "cadence_count": cadence.count}
    if item.type == "active" and item.start_date:
        updates["next_due_date"] = service_next_due(cadence, date.fromisoformat(item.start_date), local_today(user))
    updated = service.store.update_item(user.id, item.id, **updates)
    text = f"Updated billing schedule for {updated.name}."
    if hasattr(message_or_query, "edit_message_text"):
        await message_or_query.edit_message_text(text)
        await message_or_query.message.reply_text("Done.", reply_markup=main_menu_markup())
    else:
        await message_or_query.reply_text(text, reply_markup=main_menu_markup())
    return ConversationHandler.END


async def edit_offsets(update, context, service: SubShelfService):
    from telegram.ext import ConversationHandler

    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    offsets = context.user_data.setdefault("offsets", set())
    if action != "done":
        offsets.symmetric_difference_update({int(action)})
        await query.edit_message_reply_markup(reply_markup=offset_markup("edit_offset", offsets))
        return EDIT_OFFSETS
    user = service.store.get_user_by_telegram_id(update.effective_user.id)
    item = service.store.update_item(
        user.id,
        context.user_data["edit_item_id"],
        reminder_offsets=sorted(offsets, reverse=True) or [0],
    )
    await query.edit_message_text(f"Updated reminders for {item.name}.")
    await query.message.reply_text("Done.", reply_markup=main_menu_markup())
    return ConversationHandler.END


async def cadence_choice(update, context, custom_state, next_state, offset_prefix):
    query = update.callback_query
    await query.answer()
    value = query.data.split(":", 1)[1]
    if value == "monthly":
        context.user_data["flow"]["cadence"] = Cadence("months", 1)
    elif value == "yearly":
        context.user_data["flow"]["cadence"] = Cadence("years", 1)
    else:
        context.user_data["pending_cadence_unit"] = "months" if value == "x_months" else "days"
        await query.edit_message_text(f"How many {context.user_data['pending_cadence_unit']}?")
        return custom_state
    context.user_data["offsets"] = set(DEFAULT_OFFSETS)
    await query.edit_message_text("Choose reminder offsets.", reply_markup=offset_markup(offset_prefix, context.user_data["offsets"]))
    return next_state


async def cadence_count(update, context, next_state, offset_prefix):
    try:
        count = int(update.message.text)
        context.user_data["flow"]["cadence"] = Cadence(context.user_data["pending_cadence_unit"], count)
    except ValueError:
        await update.message.reply_text("Use a positive whole number.")
        return next_state - 1
    context.user_data["offsets"] = set(DEFAULT_OFFSETS)
    await update.message.reply_text("Choose reminder offsets.", reply_markup=offset_markup(offset_prefix, context.user_data["offsets"]))
    return next_state


async def cancel_flow(update, context):
    from telegram.ext import ConversationHandler

    await update.message.reply_text("Cancelled the current flow.", reply_markup=main_menu_markup())
    return ConversationHandler.END


async def require_user(update, service: SubShelfService):
    user = service.store.get_user_by_telegram_id(update.effective_user.id)
    if not user:
        await update.message.reply_text("Use /start first.")
        return None
    return user


def service_next_due(cadence: Cadence, start: date, today: date) -> str:
    from .dates import next_due_date

    return next_due_date(start, cadence, today, include_today=True).isoformat()


def _command_args(update) -> str:
    return update.message.text.partition(" ")[2].strip()


def _settings_update_for_value(field: str, value: str) -> dict:
    if field == "currency":
        return {"default_currency": normalize_currency(value)}
    if field == "timezone":
        return {"timezone_name": parse_timezone(value)}
    if field == "time":
        return {"reminder_time": parse_reminder_time(value)}
    raise ValueError("Unknown settings field.")


def _settings_other_prompt(field: str) -> str:
    if field == "currency":
        return "Type a currency code, such as SGD or USD."
    if field == "timezone":
        return "Type an IANA timezone, such as Asia/Singapore."
    if field == "time":
        return "Type a local reminder time, such as 09:00."
    return "Type the new value."


def _test_reminder_text(entry) -> str:
    item = entry.item
    header = "Test reminder preview"
    if entry.kind == "trial" and entry.offset_days == 0:
        return "\n".join(
            [
                header,
                "",
                f"Trial ends today: {item.name}",
                f"Paid plan: {item.currency} {item.amount} {item.cadence.display() if item.cadence else ''}",
                "",
                "Buttons would be: I cancelled trial, I continued trial, Remind me tomorrow",
            ]
        )
    if entry.kind == "trial":
        when = "tomorrow" if entry.offset_days == 1 else f"in {entry.offset_days} days"
        return "\n".join(
            [
                header,
                "",
                f"{item.name} trial ends {when}.",
                f"Date: {entry.due_date.isoformat()}",
                f"Then: {item.currency} {item.amount}",
                "",
                "Buttons would be: Cancel trial, Remind tomorrow, Edit",
            ]
        )

    when = "today" if entry.offset_days == 0 else f"in {entry.offset_days} days"
    return "\n".join(
        [
            header,
            "",
            f"{item.name} renews {when}.",
            f"Amount: {item.currency} {item.amount}",
            f"Date: {entry.due_date.isoformat()}",
            "",
            "Buttons would be: Cancel subscription, Remind tomorrow, Edit",
        ]
    )


def normalize_list_category(value: str) -> str | None:
    normalized = value.strip().lower()
    aliases = {
        "active": "active",
        "subscriptions": "active",
        "subs": "active",
        "trials": "trials",
        "trial": "trials",
        "watchlist": "watchlist",
        "interested": "watchlist",
        "cancelled": "cancelled",
        "canceled": "cancelled",
    }
    return aliases.get(normalized)


def search_results_text(items, query: str) -> str:
    lines = [f"Search results for {query}"]
    for item in items:
        lines.append(f"{item.id}. {item.name} - {item.type}, {item.status}")
    return "\n".join(lines)


def search_results_markup(items):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows = [[InlineKeyboardButton(f"Manage {item.id}. {item.name}", callback_data=f"details:{item.id}")] for item in items[:20]]
    return InlineKeyboardMarkup(rows)


def amount_effective_markup(item):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    if item.type == "active":
        rows = [
            [InlineKeyboardButton("Since start date", callback_data="amounteff:since_start")],
            [InlineKeyboardButton("This current cycle", callback_data="amounteff:this_cycle")],
            [InlineKeyboardButton("Next renewal", callback_data="amounteff:next_cycle")],
            [InlineKeyboardButton("Custom date", callback_data="amounteff:custom")],
        ]
    elif item.type == "trial":
        rows = [
            [InlineKeyboardButton("From paid plan start", callback_data="amounteff:since_start")],
            [InlineKeyboardButton("Custom date", callback_data="amounteff:custom")],
        ]
    else:
        rows = [
            [InlineKeyboardButton("Update watchlist amount", callback_data="amounteff:since_start")],
            [InlineKeyboardButton("Custom date", callback_data="amounteff:custom")],
        ]
    return InlineKeyboardMarkup(rows)


def main_menu_markup():
    from telegram import KeyboardButton, ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("Add subscription"), KeyboardButton("Add trial")],
            [KeyboardButton("Add interested"), KeyboardButton("View all")],
            [KeyboardButton("Upcoming"), KeyboardButton("Spending"), KeyboardButton("Settings")],
            [KeyboardButton("Quick add help")],
        ],
        resize_keyboard=True,
    )


def currency_markup(prefix: str):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    buttons = [[InlineKeyboardButton(currency, callback_data=f"{prefix}:{currency}") for currency in COMMON_CURRENCIES[:3]]]
    buttons.append([InlineKeyboardButton(currency, callback_data=f"{prefix}:{currency}") for currency in COMMON_CURRENCIES[3:]])
    buttons.append([InlineKeyboardButton("Other", callback_data=f"{prefix}:other")])
    return InlineKeyboardMarkup(buttons)


def timezone_markup(prefix: str = "ob_tz"):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    buttons = [[InlineKeyboardButton(tz, callback_data=f"{prefix}:{tz}")] for tz in COMMON_TIMEZONES]
    buttons.append([InlineKeyboardButton("Other", callback_data=f"{prefix}:other")])
    return InlineKeyboardMarkup(buttons)


def time_markup(prefix: str):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(value, callback_data=f"{prefix}:{value}") for value in COMMON_TIMES[:2]],
         [InlineKeyboardButton(value, callback_data=f"{prefix}:{value}") for value in COMMON_TIMES[2:]]]
    )


def today_markup():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup([[InlineKeyboardButton("Use today", callback_data="date_today")]])


def cadence_markup():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Monthly", callback_data="cadence:monthly"),
                InlineKeyboardButton("Yearly", callback_data="cadence:yearly"),
            ],
            [
                InlineKeyboardButton("Every X months", callback_data="cadence:x_months"),
                InlineKeyboardButton("Every X days", callback_data="cadence:x_days"),
            ],
        ]
    )


def offset_markup(prefix: str, selected: set[int]):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows = []
    for offset in DEFAULT_OFFSETS:
        marker = "[x]" if offset in selected else "[ ]"
        rows.append([InlineKeyboardButton(f"{marker} {OFFSET_LABELS[offset]}", callback_data=f"{prefix}:{offset}")])
    rows.append([InlineKeyboardButton("Done", callback_data=f"{prefix}:done")])
    return InlineKeyboardMarkup(rows)


def settings_markup():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Currency", callback_data="settings:currency"),
                InlineKeyboardButton("Timezone", callback_data="settings:timezone"),
            ],
            [
                InlineKeyboardButton("Reminder time", callback_data="settings:time"),
                InlineKeyboardButton("Reminder offsets", callback_data="settings:offsets"),
            ],
            [InlineKeyboardButton("Done", callback_data="settings:done")],
        ]
    )


LIST_CATEGORIES = {
    "active": ("Active", lambda item: item.type == "active" and item.status == "active"),
    "trials": ("Trials", lambda item: item.type == "trial" and item.status in {"active", "needs_confirmation"}),
    "watchlist": ("Watchlist", lambda item: item.type == "interested" and item.status == "active"),
    "cancelled": ("Cancelled", lambda item: item.status == "cancelled"),
}


def list_overview_markup(service: SubShelfService, user):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows = []
    items = service.store.list_items(user.id)
    for category, (label, predicate) in LIST_CATEGORIES.items():
        if category == "cancelled":
            continue
        count = sum(1 for item in items if predicate(item))
        if count:
            rows.append([InlineKeyboardButton(f"Manage {label.lower()} ({count})", callback_data=f"listcat:{category}")])
    return InlineKeyboardMarkup(rows) if rows else None


def manage_category_text(service: SubShelfService, user, category: str) -> str:
    label, _ = LIST_CATEGORIES.get(category, ("Items", lambda item: False))
    items = category_items(service, user, category)
    if not items:
        return f"{label}\n\nNo items here."
    lines = [label]
    today = local_today(user)
    for item in items:
        if item.status == "cancelled":
            lines.append(f"{item.id}. {cancelled_item_line(service, item, today)}")
        elif item.type == "active":
            lines.append(f"{item.id}. {service.format_active_line(item, today)}")
        elif item.type == "trial":
            lines.append(f"{item.id}. {service.format_trial_line(item, today)}")
        else:
            lines.append(f"{item.id}. {service.format_interested_line(item, today)}")
    return "\n".join(lines)


def category_items_markup(service: SubShelfService, user, category: str):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows = []
    for item in category_items(service, user, category):
        rows.append([InlineKeyboardButton(f"Manage {item.id}. {item.name}", callback_data=f"manageitem:{item.id}:{category}")])
    rows.append([InlineKeyboardButton("Back to summary", callback_data="listcat:summary")])
    return InlineKeyboardMarkup(rows)


def category_items(service: SubShelfService, user, category: str):
    _, predicate = LIST_CATEGORIES.get(category, ("Items", lambda item: False))
    return [
        item
        for item in service.store.list_items(user.id, include_cancelled=(category == "cancelled"))
        if predicate(item)
    ]


def single_item_markup(item, back_category: str | None = None):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows = []
    if item.status == "cancelled":
        rows.append([InlineKeyboardButton(restore_button_label(item), callback_data=f"restoreitem:{item.id}")])
    else:
        rows.append([InlineKeyboardButton("Edit", callback_data=f"edit:{item.id}")])
    if item.type == "interested" and item.status != "cancelled":
        rows.append([InlineKeyboardButton("Convert to active", callback_data=f"convert:{item.id}")])
    if item.status == "cancelled":
        rows.append([InlineKeyboardButton("Delete", callback_data=f"deleteitem:{item.id}")])
    else:
        rows.append(
            [
                InlineKeyboardButton(cancel_button_label(item), callback_data=f"cancelitem:{item.id}"),
                InlineKeyboardButton("Delete", callback_data=f"deleteitem:{item.id}"),
            ]
        )
    if back_category:
        rows.append([InlineKeyboardButton("Back to section", callback_data=f"listcat:{back_category}")])
    return InlineKeyboardMarkup(rows)


def cancel_button_label(item) -> str:
    if item.type == "trial":
        return "Cancel trial"
    if item.type == "interested":
        return "Stop watching"
    return "Cancel subscription"


def restore_button_label(item) -> str:
    if item.type == "trial":
        return "Restore trial"
    if item.type == "interested":
        return "Restore watchlist item"
    return "Restore subscription"


def cancelled_item_line(service: SubShelfService, item, today: date) -> str:
    item_type = {
        "active": "subscription",
        "trial": "trial",
        "interested": "watchlist item",
    }.get(item.type, item.type)
    amount = service.amount_for_item_on_date(item, today) or item.decimal_amount
    amount_text = f" - {item.currency} {format_money(amount)}" if amount is not None and item.currency else ""
    return f"{item.name} - cancelled {item_type}{amount_text}"


def edit_field_markup(item):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows = [
        [InlineKeyboardButton("Name", callback_data="editfield:name"), InlineKeyboardButton("Amount", callback_data="editfield:amount")],
        [InlineKeyboardButton("Currency", callback_data="editfield:currency"), InlineKeyboardButton("Billing schedule", callback_data="editfield:cadence")],
    ]
    if item.type in {"active", "trial"}:
        rows.append([InlineKeyboardButton("Date", callback_data="editfield:date"), InlineKeyboardButton("Reminders", callback_data="editfield:reminders")])
    return InlineKeyboardMarkup(rows)


def item_details(service: SubShelfService, user, item) -> str:
    cadence = item.cadence.display() if item.cadence else "none"
    today = local_today(user)
    is_cancelled = item.status == "cancelled"
    due = service.next_due_for_item(item, today) if not is_cancelled else None
    current_amount = service.amount_for_item_on_date(item, today)
    next_amount = service.amount_for_item_on_date(item, due) if due else None
    lines = [
        item.name,
        f"Type: {item.type}",
        f"Status: {item.status}",
        f"Billing schedule: {cadence}",
    ]
    if current_amount is not None:
        label = "Last known amount" if is_cancelled else "Current amount"
        lines.append(f"{label}: {item.currency} {format_money(current_amount)}")
    elif item.amount:
        lines.append(f"Amount: {item.currency or ''} {item.amount}".strip())
    if next_amount is not None and due is not None:
        lines.append(f"Next renewal amount: {item.currency} {format_money(next_amount)} on {due.isoformat()}")
    if item.start_date:
        lines.append(f"Start date: {item.start_date}")
    if item.next_due_date and item.status != "cancelled":
        lines.append(f"Next due: {item.next_due_date}")
    if item.trial_end_date:
        lines.append(f"Trial end: {item.trial_end_date}")
    if item.reminder_offsets and item.status != "cancelled":
        lines.append("Reminders: " + ", ".join(OFFSET_LABELS.get(offset, f"{offset} days before") for offset in item.reminder_offsets))
    price_lines = service.price_history_lines(item)
    if price_lines:
        lines.append("Amount history:")
        lines.extend(f"- {line}" for line in price_lines)
    return "\n".join(lines)


async def _reminder_loop(application, service: SubShelfService) -> None:
    application.bot_data["service"] = service
    while True:
        try:
            await send_due_reminders(application, service)
        except Exception:
            logger.exception("Reminder loop failed")
        await asyncio.sleep(60)


async def send_due_reminders(application, service: SubShelfService) -> None:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    for work in service.reminder_work_due(now):
        if not service.store.record_reminder_event(work.item.id, work.due_date.isoformat(), work.offset_days):
            continue
        if work.kind == "trial" and work.offset_days == 0:
            amount = service.amount_for_item_on_date(work.item, work.due_date)
            text = (
                f"Trial ends today\n\n{work.item.name} ends today.\n"
                f"Paid plan: {work.item.currency} {format_money(amount) if amount is not None else work.item.amount} {work.item.cadence.display() if work.item.cadence else ''}"
            )
            markup = trial_end_markup(work.item.id)
        elif work.kind == "trial":
            amount = service.amount_for_item_on_date(work.item, work.due_date)
            text = (
                f"Trial coming up\n\n{work.item.name} trial ends in {work.offset_days} days.\n"
                f"Date: {work.due_date.isoformat()}\n"
                f"Then: {work.item.currency} {format_money(amount) if amount is not None else work.item.amount}"
            )
            markup = reminder_markup(work.item.id, work.item.type)
        else:
            amount = service.amount_for_item_on_date(work.item, work.due_date)
            when = "today" if work.offset_days == 0 else f"in {work.offset_days} days"
            text = (
                f"Renewal coming up\n\n{work.item.name} renews {when}.\n"
                f"Amount: {work.item.currency} {format_money(amount) if amount is not None else work.item.amount}\n"
                f"Date: {work.due_date.isoformat()}"
            )
            markup = reminder_markup(work.item.id, work.item.type)
        await application.bot.send_message(chat_id=work.user.telegram_chat_id, text=text, reply_markup=markup)

    for row in service.store.list_due_snoozes(utc_now_iso()):
        item = service.store.item_from_snooze_row(row)
        chat_id = service.store.chat_id_from_snooze_row(row)
        await application.bot.send_message(
            chat_id=chat_id,
            text=f"Reminder\n\n{item.name} needs your attention.",
            reply_markup=reminder_markup(item.id, item.type) if item.type != "trial" else trial_end_markup(item.id),
        )
        service.store.delete_snooze(row["snooze_id"])


def reminder_markup(item_id: int, item_type: str = "active"):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    cancel_label = "Cancel trial" if item_type == "trial" else "Cancel subscription"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(cancel_label, callback_data=f"cancelitem:{item_id}"),
                InlineKeyboardButton("Remind tomorrow", callback_data=f"snooze:{item_id}"),
            ],
            [
                InlineKeyboardButton("Edit", callback_data=f"edit:{item_id}"),
            ]
        ]
    )


def trial_end_markup(item_id: int):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("I cancelled trial", callback_data=f"trialcancel:{item_id}"),
                InlineKeyboardButton("I continued trial", callback_data=f"trialcontinue:{item_id}"),
            ],
            [InlineKeyboardButton("Remind me tomorrow", callback_data=f"snooze:{item_id}")],
        ]
    )
