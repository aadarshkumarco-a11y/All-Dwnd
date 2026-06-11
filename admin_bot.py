"""
Admin Panel Bot – manages subscriptions for the main downloader bot.
Only admin (user_id: 7552634255) can use this bot.
"""
import os
import time
import logging
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler, filters
)
from telegram.constants import ParseMode

import subscriptions as subs

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

ADMIN_BOT_TOKEN = os.environ.get(
    "ADMIN_BOT_TOKEN",
    "8795852596:AAELZvvesE0LelS1gO946t09T_HSa2uXr5k"
)
ADMIN_ID = 7552634255
DOWNLOADER_BOT = "@"  # filled at runtime from getMe


def is_admin(update: Update) -> bool:
    return update.effective_user.id == ADMIN_ID


def admin_only(func):
    """Decorator: ignore non-admin users silently."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update):
            await update.message.reply_text("🚫 Access Denied.")
            return
        return await func(update, context)
    return wrapper


# ── /start ──────────────────────────────────────────────────────────────────

@admin_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_subs = subs.get_all()
    active = sum(
        1 for s in all_subs.values()
        if s.get("expires") is None or s.get("expires", 0) > time.time()
    )

    text = (
        "┌────────────────────┐\n"
        "║   *Admin Panel* 🐒          ║\n"
        "└────────────────────┘\n\n"
        f"👑 Welcome, *Aadarsh!*\n"
        f"👥 Active Subscribers: *{active}*\n\n"
        "────────────────────\n"
        "📋 *Available Commands:*\n\n"
        "➕ `/add <user_id> <days>` – Add user for N days\n"
        "   _Example: `/add 123456789 30`_\n"
        "➕ `/add <user_id>` – Add permanently\n\n"
        "➖ `/remove <user_id>` – Remove user\n\n"
        "📋 `/list` – Show all subscribers\n\n"
        "🔍 `/check <user_id>` – Check user status\n\n"
        "📢 `/broadcast <message>` – Message all subscribers\n\n"
        "📊 `/stats` – Total subscriber stats\n"
        "────────────────────"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ── /add ────────────────────────────────────────────────────────────────────

@admin_only
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "⚠️ Usage:\n"
            "`/add <user_id> <days>` – N days\n"
            "`/add <user_id>` – Permanent",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")
        return

    days = None
    if len(args) >= 2:
        try:
            days = int(args[1])
            if days <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Days must be a positive number.")
            return

    # Try to get user's name from Telegram
    name = ""
    try:
        member = await context.bot.get_chat(user_id)
        name = member.full_name or member.username or ""
    except Exception:
        pass

    record = subs.add_user(user_id, days=days, name=name)
    expiry = subs.remaining_str(record)

    if days:
        expire_date = datetime.fromtimestamp(
            record["expires"], tz=timezone.utc
        ).strftime("%d %b %Y")
        msg = (
            f"✅ *User Added!*\n\n"
            f"👤 User ID: `{user_id}`\n"
            f"🪪 Name: {name or 'Unknown'}\n"
            f"📅 Duration: *{days} days*\n"
            f"📅 Expires: *{expire_date}*\n"
            f"{expiry}"
        )
    else:
        msg = (
            f"✅ *User Added (Permanent)!*\n\n"
            f"👤 User ID: `{user_id}`\n"
            f"🪪 Name: {name or 'Unknown'}\n"
            f"♾️ Access: *Lifetime*"
        )

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    # Notify the user on the downloader bot side
    try:
        duration_text = f"{days} days" if days else "lifetime"
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"🎉 *Subscription Activated!*\n\n"
                f"Tumhara *Downloader Bot* subscription activate ho gaya hai!\n"
                f"⏳ Duration: *{duration_text}*\n\n"
                f"Ab jao aur use karo 👇\n"
                f"_Start the bot and enjoy!_ 🐒"
            ),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception:
        pass  # User might not have started the bot yet


# ── /remove ─────────────────────────────────────────────────────────────────

@admin_only
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "⚠️ Usage: `/remove <user_id>`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    if subs.remove_user(user_id):
        await update.message.reply_text(
            f"✅ User `{user_id}` removed successfully.",
            parse_mode=ParseMode.MARKDOWN
        )
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "Renew karne ke liye contact karo:\n"
                    "👤 @aadi4uuu"
                ),
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass
    else:
        await update.message.reply_text(
            f"⚠️ User `{user_id}` not found in subscribers list.",
            parse_mode=ParseMode.MARKDOWN
        )


# ── /list ────────────────────────────────────────────────────────────────────

@admin_only
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_subs = subs.get_all()
    if not all_subs:
        await update.message.reply_text("📋 No subscribers yet.")
        return

    now = time.time()
    active_lines = []
    expired_lines = []

    for uid, record in all_subs.items():
        expires = record.get("expires")
        name = record.get("name") or "Unknown"
        remaining = subs.remaining_str(record)

        if expires is not None and expires < now:
            expired_lines.append(f"❌ `{uid}` – {name}")
        else:
            active_lines.append(f"✅ `{uid}` – {name}\n{remaining}")

    text = f"📋 *Subscriber List* ({len(all_subs)} total)\n"
    text += "────────────────────\n\n"

    if active_lines:
        text += f"*Active ({len(active_lines)}):*\n"
        text += "\n\n".join(active_lines)

    if expired_lines:
        text += f"\n\n*Expired ({len(expired_lines)}):*\n"
        text += "\n".join(expired_lines)

    # Split if too long
    if len(text) > 4000:
        text = text[:3900] + "\n\n_...and more_"

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ── /check ───────────────────────────────────────────────────────────────────

@admin_only
async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "⚠️ Usage: `/check <user_id>`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    if user_id == ADMIN_ID:
        await update.message.reply_text(
            f"👑 `{user_id}` is the *Admin* – always has access.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    record = subs.get_user(user_id)
    if not record:
        await update.message.reply_text(
            f"❌ User `{user_id}` has *no subscription*.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    name = record.get("name") or "Unknown"
    remaining = subs.remaining_str(record)
    added = datetime.fromtimestamp(
        record.get("added_at", 0), tz=timezone.utc
    ).strftime("%d %b %Y %H:%M UTC")

    authorized = subs.is_authorized(user_id)

    await update.message.reply_text(
        f"🔍 *User Status*\n\n"
        f"👤 User ID: `{user_id}`\n"
        f"🪪 Name: {name}\n"
        f"📅 Added: {added}\n"
        f"{remaining}\n"
        f"{'✅ Active' if authorized else '❌ Expired'}",
        parse_mode=ParseMode.MARKDOWN
    )


# ── /stats ───────────────────────────────────────────────────────────────────

@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_subs = subs.get_all()
    now = time.time()
    total = len(all_subs)
    active = sum(
        1 for s in all_subs.values()
        if s.get("expires") is None or s.get("expires", 0) > now
    )
    expired = total - active
    permanent = sum(
        1 for s in all_subs.values()
        if s.get("expires") is None
    )

    await update.message.reply_text(
        f"📊 *Subscriber Stats*\n\n"
        f"👥 Total: *{total}*\n"
        f"✅ Active: *{active}*\n"
        f"♾️ Permanent: *{permanent}*\n"
        f"❌ Expired: *{expired}*",
        parse_mode=ParseMode.MARKDOWN
    )


# ── /broadcast ───────────────────────────────────────────────────────────────

@admin_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: `/broadcast <message>`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    message = " ".join(context.args)
    all_subs = subs.get_all()
    sent = 0
    failed = 0

    status_msg = await update.message.reply_text(
        f"📢 Broadcasting to {len(all_subs)} users...",
    )

    for uid in all_subs:
        try:
            await context.bot.send_message(
                chat_id=int(uid),
                text=f"📢 *Message from Admin:*\n\n{message}",
                parse_mode=ParseMode.MARKDOWN
            )
            sent += 1
        except Exception:
            failed += 1

    await status_msg.edit_text(
        f"✅ *Broadcast Complete!*\n\n"
        f"📤 Sent: *{sent}*\n"
        f"❌ Failed: *{failed}*",
        parse_mode=ParseMode.MARKDOWN
    )


# ── Unknown commands ─────────────────────────────────────────────────────────

@admin_only
async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ Unknown command. Send /start to see all commands."
    )


# ── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    if not ADMIN_BOT_TOKEN:
        logger.error("ADMIN_BOT_TOKEN nahi mila!")
        return

    app = Application.builder().token(ADMIN_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    logger.info("Admin Bot chal raha hai... 👑")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
