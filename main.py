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
EXPIRY_MINUTES = int(os.getenv("EXPIRY_MINUTES", 30))
log.info(f"Loaded ADMIN_IDS: {ADMIN_IDS} | Expiry: {EXPIRY_MINUTES}m")

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

    # 2. If unlocked, check for expiry
    if status == "unlocked":
        expires_at_str = session.get("expires_at")
        if expires_at_str:
            expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
            file_expiry = file_data.get("expiry_minutes") or EXPIRY_MINUTES
            
            if datetime.now(timezone.utc) > expires_at:
                exp_msg = await message.answer(f"⏰ Your {file_expiry}-minute access to this file has expired.\n\nRenewing access...")
                
                # Reset session to pending in DB
                try:
                    supabase.table("user_sessions").update({
                        "status": "pending",
                        "ads_watched": 0,
                        "unlocked_at": None,
                        "expires_at": None,
                        "file_message_id": None
                    }).eq("id", session_id).execute()
                    
                    status = "pending"
                    ads_watched = 0
                    
                    # Delete the expired message after 3 seconds
                    await asyncio.sleep(3)
                    try:
                        await exp_msg.delete()
                    except:
                        pass
                        
                except Exception as e:
                    log.error(f"Error resetting session: {e}")
                    return

    # 3. If pending and 0 ads, auto-unlock
    if status == "pending" and required_ads == 0:
        now = datetime.now(timezone.utc)
        file_expiry = file_data.get("expiry_minutes") or EXPIRY_MINUTES
        expires_at = now + timedelta(minutes=file_expiry)
        
        supabase.table("user_sessions").update({
            "status": "unlocked",
            "unlocked_at": now.isoformat(),
            "expires_at": expires_at.isoformat()
        }).eq("id", session_id).execute()
        
        status = "unlocked"

    # 4. If unlocked, send the file
    if status == "unlocked":
        file_expiry = file_data.get("expiry_minutes") or EXPIRY_MINUTES
        try:
            sent_video = await bot.copy_message(
                chat_id=user_id,
                from_chat_id=PRIVATE_CHANNEL_ID,
                message_id=file_data["message_id"],
                caption=f"🎉 *Unlocked!* Here is your file.\n\n⚠️ Expires in {file_expiry} minutes.",
                parse_mode="Markdown"
            )
            
            supabase.table("user_sessions").update({
                "file_message_id": sent_video.message_id
            }).eq("id", session_id).execute()
            
            return
        except Exception as e:
            log.error(f"Error sending file: {e}")
            await message.answer("❌ Could not send the file. Contact admin.")
            return

    # 5. Still pending, show ad button

    # Still pending - show ad button
    app_url = f"{WEB_DOMAIN}/?session={session_id}"
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"🎁 Watch Ad ({ads_watched}/{required_ads})",
            web_app=WebAppInfo(url=app_url)
        )
    ]])
    locked_text = (
        f"🔒 *File Locked!*\n\n"
        f"Watch *{required_ads} ad(s)* to unlock this file.\n"
        f"Progress: {ads_watched}/{required_ads} ads watched.\n\n"
        f"Tap the button below to watch an ad 👇"
    )

    existing_msg_id = session.get("bot_message_id")
    if existing_msg_id:
        # Message already sent — just edit it (prevents double messages)
        try:
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=existing_msg_id,
                text=locked_text,
                parse_mode="Markdown",
                reply_markup=markup
            )
            return
        except Exception:
            pass  # If edit fails, fall through and send a new message

    # Send fresh message
    sent = await message.answer(locked_text, parse_mode="Markdown", reply_markup=markup)
    # Save message_id so we can edit it later
    try:
        supabase.table("user_sessions").update(
            {"bot_message_id": sent.message_id}
        ).eq("id", session_id).execute()
    except Exception as e:
        log.warning(f"Could not save bot_message_id: {e}")


# ── Help Command ────────────────────────────────────────────────────────────
@dp.message(Command("help"))
async def handle_help(message: types.Message):
    user_id = message.from_user.id
    # Ensure ADMIN_IDS is a set for faster lookup
    is_admin = user_id in ADMIN_IDS

    if is_admin:
        help_text = (
            "🛠 *Admin Help Menu*\n\n"
            "*Management Commands:*\n"
            "• `/files` - List all uploaded files & links\n"
            "• `/del <code>` - Delete a file\n"
            "• `/stats` - Real-time bot statistics\n\n"
            "*Uploading:*\n"
            "• Just send a video/file to start the upload process.\n\n"
            "*User Commands:*\n"
            "• `/start` - Welcome message\n"
            "• `/start <code>` - Request a file"
        )
    else:
        help_text = (
            "👤 *User Help Menu*\n\n"
            "• `/start` - Welcome message\n"
            "• `/start <code>` - Use a link to get a file\n"
            "• `/help` - Show this menu\n\n"
            f"Note: Files expire {EXPIRY_MINUTES} minutes after unlocking."
        )
    
    await message.answer(help_text, parse_mode="Markdown")


# ── Admin: Receive file ───────────────────────────────────────────────────────
@dp.message(F.from_user.id.in_(set(ADMIN_IDS)) & (F.video | F.document | F.audio | F.photo))
async def handle_admin_file(message: types.Message, state: FSMContext):
    log.info(f"Admin {message.from_user.id} sent a file")
    
    # Extract file name
    file_name = "Unknown File"
    if message.video:
        file_name = message.video.file_name or message.caption or "Video"
    elif message.document:
        file_name = message.document.file_name or message.caption or "Document"
    elif message.audio:
        file_name = message.audio.title or message.audio.file_name or "Audio"
    elif message.photo:
        file_name = message.caption or "Photo"

    await state.update_data(original_message_id=message.message_id, file_name=file_name)
    await state.set_state(AdminStates.waiting_for_ads_count)
    await message.answer(
        f"✅ *File Received!* \n📄 Name: `{file_name}`\n\n"
        "Reply with the number of ads required to unlock this file.\n"
        "Example: `1`, `2`, `3` or `5`",
        parse_mode="Markdown"
    )


# ── Admin: Set ads count ──────────────────────────────────────────────────────
@dp.message(F.from_user.id.in_(set(ADMIN_IDS)), AdminStates.waiting_for_ads_count)
async def handle_ads_count(message: types.Message, state: FSMContext):
    input_text = message.text.strip() if message.text else ""
    
    # Support "Ads | Name | Time" format
    parts = [p.strip() for p in input_text.split("|")]
    ads_part = parts[0] if len(parts) > 0 else ""
    custom_name = parts[1] if len(parts) > 1 else None
    custom_expiry = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else EXPIRY_MINUTES

    if not ads_part.isdigit():
        await message.answer(
            "❌ Please send a valid format.\n"
            "• `3` (Only Ads)\n"
            "• `3 | Movie Name` (Ads + Name)\n"
            "• `3 | Movie Name | 60` (Ads + Name + Expiry in minutes)",
            parse_mode="Markdown"
        )
        return

    required_ads = int(ads_part)
    data = await state.get_data()
    original_message_id = data.get("original_message_id")
    file_name = custom_name if custom_name else data.get("file_name", "Unknown")
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
            "required_ads": required_ads,
            "file_name":    file_name,
            "expiry_minutes": custom_expiry
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


# ── Admin: List Files ───────────────────────────────────────────────────────
@dp.message(F.from_user.id.in_(set(ADMIN_IDS)), Command("files"))
async def handle_list_files(message: types.Message):
    try:
        res = supabase.table("files").select("*").order("created_at", desc=True).execute()
        if not res.data:
            await message.answer("📂 No files found in database.")
            return

        text = "📂 *Your Uploaded Files:*\n\n"
        me = await bot.get_me()
        for f in res.data:
            link = f"https://t.me/{me.username}?start={f['file_code']}"
            name = f.get("file_name") or "Unnamed File"
            text += f"📄 *{name}*\n"
            text += f"• Code: `{f['file_code']}` | Ads: *{f['required_ads']}*\n🔗 [Link]({link})\n\n"

        # Split if text too long
        if len(text) > 4000:
            for i in range(0, len(text), 4000):
                await message.answer(text[i:i+4000], parse_mode="Markdown", disable_web_page_preview=True)
        else:
            await message.answer(text, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        log.error(f"Error listing files: {e}")
        await message.answer("❌ Error fetching files.")


# ── Admin: Delete File ──────────────────────────────────────────────────────
@dp.message(F.from_user.id.in_(set(ADMIN_IDS)), Command("del"))
async def handle_delete_file(message: types.Message):
    args = message.text.split(" ", 1)
    if len(args) < 2:
        await message.answer("⚠️ Usage: `/del <file_code>`", parse_mode="Markdown")
        return

    file_code = args[1].strip()
    try:
        # Check if exists
        res = supabase.table("files").select("*").eq("file_code", file_code).execute()
        if not res.data:
            await message.answer(f"❌ File with code `{file_code}` not found.")
            return

        file_data = res.data[0]
        channel_message_id = file_data.get("message_id")

        # 1. Try to delete from the private channel
        if channel_message_id:
            try:
                await bot.delete_message(chat_id=PRIVATE_CHANNEL_ID, message_id=channel_message_id)
                log.info(f"Deleted message {channel_message_id} from private channel")
            except Exception as e:
                log.warning(f"Could not delete message {channel_message_id} from channel: {e}")

        # 2. Delete from database
        supabase.table("files").delete().eq("file_code", file_code).execute()
        
        await message.answer(f"✅ File `{file_code}` deleted from both database and private channel.")
    except Exception as e:
        log.error(f"Error deleting file: {e}")
        await message.answer("❌ Error deleting file.")


# ── Admin: Stats ────────────────────────────────────────────────────────────
@dp.message(F.from_user.id.in_(set(ADMIN_IDS)), Command("stats"))
async def handle_stats(message: types.Message):
    try:
        # Get counts
        files_res = supabase.table("files").select("id", count="exact").execute()
        sessions_res = supabase.table("user_sessions").select("user_id, status", count="exact").execute()

        total_files = files_res.count
        all_sessions = sessions_res.data or []

        # Calculate unique users
        unique_users = len(set(s["user_id"] for s in all_sessions))

        # Calculate status breakdown
        pending_count = sum(1 for s in all_sessions if s["status"] == "pending")
        unlocked_count = sum(1 for s in all_sessions if s["status"] == "unlocked")

        await message.answer(
            f"📊 *Real-time Bot Statistics:*\n\n"
            f"👥 Total Unique Users: *{unique_users}*\n"
            f"⏳ Users Currently Waiting (Pending): *{pending_count}*\n"
            f"🔓 Total Successful Unlocks: *{unlocked_count}*\n\n"
            f"📁 Total Files Uploaded: *{total_files}*",
            parse_mode="Markdown"
        )
    except Exception as e:
        log.error(f"Error fetching stats: {e}")
        await message.answer("❌ Error fetching statistics.")




# ── Catch-all for unauthorized users ─────────────────────────────────────────
@dp.message(~F.from_user.id.in_(set(ADMIN_IDS)))
async def handle_unauthorized(message: types.Message):
    if message.text and (message.text.startswith("/start") or message.text.startswith("/stats") or message.text.startswith("/files") or message.text.startswith("/del") or message.text.startswith("/help")):
        if message.text.startswith("/start") or message.text.startswith("/help"):
            return
        await message.answer(
            f"⚠️ You are not an admin.\nYour Telegram ID: `{message.from_user.id}`",
            parse_mode="Markdown"
        )
        return
    # Silently ignore other messages or send warning


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
            file_expiry = file_data.get("expiry_minutes") or EXPIRY_MINUTES
            expires_at = now + timedelta(minutes=file_expiry)

            supabase.table("user_sessions").update({
                "ads_watched": ads_watched,
                "status":      "unlocked",
                "unlocked_at": now.isoformat(),
                "expires_at":  expires_at.isoformat()
            }).eq("id", session_id).execute()

            try:
                # Delete the "File Locked" message first
                bot_message_id = session.get("bot_message_id")
                if bot_message_id:
                    try:
                        await bot.delete_message(chat_id=user_id, message_id=bot_message_id)
                    except Exception:
                        pass  # Ignore if already deleted

                # Send the unlocked video
                sent_video = await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=PRIVATE_CHANNEL_ID,
                    message_id=file_data["message_id"],
                    caption=f"🎉 *Unlocked!* Here is your file.\n\n⚠️ Expires in {EXPIRY_MINUTES} minutes.",
                    parse_mode="Markdown"
                )

                # Save the video message ID for later deletion
                supabase.table("user_sessions").update({
                    "file_message_id": sent_video.message_id
                }).eq("id", session_id).execute()
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
                bot_message_id = session.get("bot_message_id")
                if bot_message_id:
                    # Edit the existing message instead of sending a new one
                    await bot.edit_message_text(
                        chat_id=user_id,
                        message_id=bot_message_id,
                        text=f"🔒 *File Locked!*\n\n"
                             f"Progress: {ads_watched}/{required_ads} ads watched.\n"
                             f"✅ Ad {ads_watched} done! Watch {required_ads - ads_watched} more to unlock.\n\n"
                             f"Tap the button below to watch the next ad 👇",
                        parse_mode="Markdown",
                        reply_markup=markup
                    )
                else:
                    # Fallback: send new message if message_id not saved
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


async def deletion_cleanup_loop():
    """Background task to delete expired files from user chats."""
    log.info("🧹 Deletion cleanup loop started")
    while True:
        try:
            # Find unlocked sessions that have expired and have a message ID to delete
            now = datetime.now(timezone.utc).isoformat()
            res = supabase.table("user_sessions").select("*")\
                .eq("status", "unlocked")\
                .lt("expires_at", now)\
                .not_.is_("file_message_id", "null")\
                .execute()

            if res.data:
                for session in res.data:
                    user_id = session["user_id"]
                    msg_id = session["file_message_id"]
                    session_id = session["id"]

                    log.info(f"Attempting to delete expired message {msg_id} for user {user_id}")
                    try:
                        await bot.delete_message(chat_id=user_id, message_id=msg_id)
                    except Exception as de:
                        log.warning(f"Could not delete message {msg_id}: {de}")

                    # Clear the message ID in DB so we don't try again
                    supabase.table("user_sessions").update({
                        "file_message_id": None
                    }).eq("id", session_id).execute()

        except Exception as e:
            log.error(f"Error in deletion loop: {e}")

        await asyncio.sleep(60)  # Check every minute


async def main():
    threading.Thread(target=keep_awake_pinger, daemon=True).start()
    asyncio.create_task(deletion_cleanup_loop())
    await start_web_server()
    log.info(f"🤖 Bot starting... (Admins: {ADMIN_IDS})")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
