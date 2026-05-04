import asyncio
import os
import threading
import time
import urllib.request
import logging
import shortuuid
from datetime import datetime, timezone, timedelta

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiohttp import web
from dotenv import load_dotenv
from supabase import create_client, Client as SupabaseClient

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger("MAIN")

load_dotenv()

BOT_TOKEN          = os.getenv("BOT_TOKEN", "8611336689:AAFhZoLT8X0_Ip0PFmRuCdMs_hKe94rs_eA")
SUPABASE_URL       = os.getenv("SUPABASE_URL", "https://zknqlbvxtujuylfzvkrz.supabase.co")
SUPABASE_KEY       = os.getenv("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InprbnFsYnZ4dHVqdXlsZnp2a3J6Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3NzgxNTA3MCwiZXhwIjoyMDkzMzkxMDcwfQ.HuOHXkZdalLHd1ApIjeShsqipPQvbECQU09v2Q-MeFs")
PRIVATE_CHANNEL_ID = int(os.getenv("PRIVATE_CHANNEL_ID", "-1003728263573"))
WEB_DOMAIN         = os.getenv("WEB_DOMAIN", "https://your-domain.com")
PORT               = int(os.getenv("PORT", 8080))

admin_ids_str = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip().isdigit()]
log.info(f"Loaded ADMIN_IDS: {ADMIN_IDS}")

# ── Init Bot, Dispatcher, Supabase ──────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

try:
    supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)
    log.info("Supabase connected ✅")
except Exception as e:
    log.error(f"Supabase Init Error: {e}")
    supabase = None

# ── FSM States ───────────────────────────────────────────────────────────────
class AdminStates(StatesGroup):
    waiting_for_ads_count = State()

# ==========================================
# BOT HANDLERS
# ==========================================

@dp.message(CommandStart())
async def handle_start(message: types.Message):
    if not supabase:
        await message.answer("❌ Database not configured.")
        return

    args = message.text.split(" ", 1)
    if len(args) < 2:
        await message.answer("👋 Welcome! This is a private file sharing bot.")
        return

    file_code = args[1].strip()
    user_id   = message.from_user.id

    # Find file in DB
    try:
        res = supabase.table("files").select("*").eq("file_code", file_code).execute()
        if not res.data:
            await message.answer("❌ Invalid or expired link.")
            return
        file_data = res.data[0]
    except Exception as e:
        log.error(f"Error fetching file: {e}")
        await message.answer("❌ An error occurred.")
        return

    # Get or create user session
    try:
        session_res = supabase.table("user_sessions").select("*")\
            .eq("user_id", user_id).eq("file_code", file_code).execute()

        if not session_res.data:
            inserted = supabase.table("user_sessions").insert({
                "user_id":    user_id,
                "file_code":  file_code,
                "ads_watched": 0,
                "status":     "pending"
            }).execute()
            session = inserted.data[0]
        else:
            session = session_res.data[0]
    except Exception as e:
        log.error(f"Error handling session: {e}")
        await message.answer("❌ An error occurred.")
        return

    session_id   = session["id"]
    status       = session["status"]
    ads_watched  = session["ads_watched"]
    required_ads = file_data["required_ads"]

    if status == "unlocked":
        expires_at_str = session.get("expires_at")
        if expires_at_str:
            expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > expires_at:
                await message.answer("⏰ Your 30-minute access to this file has expired.")
                return
        # Send the file
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=PRIVATE_CHANNEL_ID,
                message_id=file_data["message_id"]
            )
        except Exception as e:
            log.error(f"Error sending file: {e}")
            await message.answer("❌ Could not send the file. Contact admin.")
        return

    # Still pending - show ad button
    app_url = f"{WEB_DOMAIN}/?session={session_id}"
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"🎁 Watch Ad ({ads_watched}/{required_ads})",
            web_app=WebAppInfo(url=app_url)
        )
    ]])
    await message.answer(
        f"🔒 *File Locked!*\n\n"
        f"Watch *{required_ads} ad(s)* to unlock this file.\n"
        f"Progress: {ads_watched}/{required_ads} ads watched.\n\n"
        f"Tap the button below to watch an ad 👇",
        parse_mode="Markdown",
        reply_markup=markup
    )


# ── Admin: Receive file ───────────────────────────────────────────────────────
@dp.message(F.from_user.id.in_(set(ADMIN_IDS)) & (F.video | F.document | F.audio | F.photo))
async def handle_admin_file(message: types.Message, state: FSMContext):
    log.info(f"Admin {message.from_user.id} sent a file")
    await state.update_data(original_message_id=message.message_id)
    await state.set_state(AdminStates.waiting_for_ads_count)
    await message.answer(
        "✅ File received!\n\n"
        "Reply with the number of ads required to unlock this file.\n"
        "Example: `1`, `2`, `3` or `5`",
        parse_mode="Markdown"
    )


# ── Admin: Set ads count ──────────────────────────────────────────────────────
@dp.message(F.from_user.id.in_(set(ADMIN_IDS)), AdminStates.waiting_for_ads_count)
async def handle_ads_count(message: types.Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        await message.answer("❌ Please send a valid number (e.g. `1`, `2`, `3`).", parse_mode="Markdown")
        return

    required_ads = int(message.text.strip())
    data = await state.get_data()
    original_message_id = data.get("original_message_id")
    await state.clear()

    # Forward file to private channel
    try:
        copied = await bot.copy_message(
            chat_id=PRIVATE_CHANNEL_ID,
            from_chat_id=message.chat.id,
            message_id=original_message_id
        )
        channel_message_id = copied.message_id
    except Exception as e:
        log.error(f"Error copying to private channel: {e}")
        await message.answer(f"❌ Failed to upload to private channel: {e}")
        return

    # Save to Supabase
    file_code = shortuuid.uuid()[:8]
    try:
        supabase.table("files").insert({
            "file_code":    file_code,
            "message_id":   channel_message_id,
            "required_ads": required_ads
        }).execute()

        me = await bot.get_me()
        link = f"https://t.me/{me.username}?start={file_code}"
        await message.answer(
            f"✅ *File Uploaded Successfully!*\n\n"
            f"📢 Required Ads: *{required_ads}*\n"
            f"🔗 Share Link:\n`{link}`",
            parse_mode="Markdown"
        )
    except Exception as e:
        log.error(f"Supabase Insert Error: {e}")
        await message.answer("❌ Failed to save to database.")


# ── Catch-all for unauthorized users ─────────────────────────────────────────
@dp.message(~F.from_user.id.in_(set(ADMIN_IDS)))
async def handle_unauthorized(message: types.Message):
    if message.text and message.text.startswith("/start"):
        return  # Let /start handle it
    await message.answer(
        f"⚠️ You are not an admin.\nYour Telegram ID: `{message.from_user.id}`",
        parse_mode="Markdown"
    )


# ==========================================
# WEB SERVER
# ==========================================

async def handle_index(request):
    return web.FileResponse('index.html')


async def handle_ad_completed(request):
    if not supabase:
        return web.json_response({"status": "error", "message": "Database not configured"}, status=500)
    try:
        data       = await request.json()
        session_id = data.get("session")

        if not session_id:
            return web.json_response({"status": "error", "message": "Session missing"}, status=400)

        res = supabase.table("user_sessions").select("*").eq("id", session_id).execute()
        if not res.data:
            return web.json_response({"status": "error", "message": "Invalid session"}, status=400)

        session = res.data[0]

        if session["status"] == "unlocked":
            return web.json_response({"status": "already_used", "message": "Already unlocked!"}, status=400)

        user_id      = session["user_id"]
        file_code    = session["file_code"]
        ads_watched  = session["ads_watched"] + 1

        file_res = supabase.table("files").select("*").eq("file_code", file_code).execute()
        if not file_res.data:
            return web.json_response({"status": "error", "message": "File not found"}, status=400)

        file_data    = file_res.data[0]
        required_ads = file_data["required_ads"]

        if ads_watched >= required_ads:
            now        = datetime.now(timezone.utc)
            expires_at = now + timedelta(minutes=30)

            supabase.table("user_sessions").update({
                "ads_watched": ads_watched,
                "status":      "unlocked",
                "unlocked_at": now.isoformat(),
                "expires_at":  expires_at.isoformat()
            }).eq("id", session_id).execute()

            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=PRIVATE_CHANNEL_ID,
                    message_id=file_data["message_id"],
                    caption="🎉 *Unlocked!* Here is your file.\n\n⚠️ Expires in 30 minutes.",
                    parse_mode="Markdown"
                )
            except Exception as e:
                log.error(f"Error sending file: {e}")

            return web.json_response({"status": "success", "action": "unlocked"})
        else:
            supabase.table("user_sessions").update({
                "ads_watched": ads_watched
            }).eq("id", session_id).execute()

            try:
                app_url = f"{WEB_DOMAIN}/?session={session_id}"
                markup = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text=f"🎁 Watch Ad ({ads_watched}/{required_ads})",
                        web_app=WebAppInfo(url=app_url)
                    )
                ]])
                await bot.send_message(
                    chat_id=user_id,
                    text=f"✅ Ad {ads_watched}/{required_ads} done! Watch {required_ads - ads_watched} more to unlock.",
                    reply_markup=markup
                )
            except Exception as e:
                log.error(f"Error notifying progress: {e}")

            return web.json_response({"status": "success", "action": "progress",
                                      "watched": ads_watched, "required": required_ads})
    except Exception as e:
        log.error(f"API Error: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def start_web_server():
    webapp = web.Application()
    webapp.add_routes([
        web.get('/',              handle_index),
        web.post('/ad-completed', handle_ad_completed),
    ])
    runner = web.AppRunner(webapp)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    log.info(f"🚀 Web server started on port {PORT}")


def keep_awake_pinger():
    my_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not my_url:
        return
    log.info(f"✅ Auto-pinger started for {my_url}")
    while True:
        try:
            time.sleep(10 * 60)
            urllib.request.urlopen(my_url)
        except Exception:
            pass


async def main():
    threading.Thread(target=keep_awake_pinger, daemon=True).start()
    await start_web_server()
    log.info(f"🤖 Bot starting... (Admins: {ADMIN_IDS})")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
