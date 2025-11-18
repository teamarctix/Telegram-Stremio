import asyncio
import aiofiles
import aiohttp
import random
import string
from os import path as ospath
from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)
from pyrogram.errors import MessageNotModified
from Backend.helper.custom_filter import CustomFilters
from Backend.logger import LOGGER

# -------------------------------
# CONFIGURABLE CONSTANTS
# -------------------------------
CHUNK_SIZE = 3500
MAX_PASTE_PAGES = 100
LOG_FILE_PATH = ospath.abspath("log.txt")
MAX_CHARS = 100000

# -------------------------------
# UTILITY FUNCTIONS
# -------------------------------
def trim_content(content: str) -> str:
    return content[-MAX_CHARS:] if len(content) > MAX_CHARS else content

async def generate_random_string(length=32):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

async def paste_to_spacebin(content: str):
    content = trim_content(content)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post("https://spaceb.in/api/v1/documents", data={"content": content, "extension": "txt"}) as r:
                if r.status == 201:
                    data = await r.json()
                    doc_id = data.get("payload", {}).get("id")
                    LOGGER.info(f"Spacebin paste success: {doc_id}")
                    return f"https://spaceb.in/{doc_id}"
                else:
                    try:
                        error_msg = (await r.json()).get('error', 'Unknown error')
                    except Exception:
                        error_msg = f"HTTP {r.status}"
                    LOGGER.warning(f"Spacebin paste failed: {error_msg}")
                    return f"Error: {error_msg}"
    except Exception as e:
        LOGGER.exception(f"Exception in paste_to_spacebin: {e}")
        return f"Error: {e}"

async def paste_to_yaso(content: str):
    content = trim_content(content)
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
            async with session.post("https://api.yaso.su/v1/auth/guest") as auth:
                auth.raise_for_status()
                LOGGER.info("Yaso guest auth successful")

            async with session.post(
                "https://api.yaso.su/v1/records",
                json={
                    "captcha": await generate_random_string(64),
                    "codeLanguage": "auto",
                    "content": content,
                    "extension": "txt",
                    "expirationTime": 1000000,
                },
            ) as paste:
                paste.raise_for_status()
                result = await paste.json()
                url = result.get("url")
                LOGGER.info(f"Yaso paste successful: {url}")
                return f"https://yaso.su/raw/{url}"
    except Exception as e:
        LOGGER.exception(f"Exception in paste_to_yaso: {e}")
        return f"Error: {e}"

async def paste_to_fragbin(content: str, title: str = "Log"):
    content = content[-20480:]

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://www.fragbin.com/api/pastes",
                json={
                    "title": title,
                    "content": content,
                    "language": "text",
                    "expiresAt": "never",
                    "isPrivate": False,
                    "password": None
                },
            ) as resp:
                resp.raise_for_status()
                result = await resp.json()
                paste_id = result.get("id")
                url = f"https://www.fragbin.com/r/{paste_id}"
                LOGGER.info(f"FragBin paste successful: {url}")
                return url
    except Exception as e:
        LOGGER.exception(f"Exception in paste_to_fragbin: {e}")
        return f"Error: {e}"


def get_total_pages(file_path: str, chunk_size=CHUNK_SIZE) -> int:
    file_size = ospath.getsize(file_path)
    return (file_size + chunk_size - 1) // chunk_size

async def get_page(file_path: str, page_index: int, chunk_size=CHUNK_SIZE) -> str:
    async with aiofiles.open(file_path, "r") as f:
        await f.seek(page_index * chunk_size)
        return await f.read(chunk_size)

# -------------------------------
# LOG CACHE
# -------------------------------
LOG_CACHE = {}  # message_id -> dict with file_path, total_pages, url, index, view_mode, selector_start, range_index

# -------------------------------
# SAFE ANSWER
# -------------------------------
async def safe_answer(query: CallbackQuery, text: str = None, show_alert: bool = False):
    try:
        await query.answer(text=text, show_alert=show_alert)
    except Exception as e:
        LOGGER.debug(f"safe_answer failed: {e}")

# -------------------------------
# MARKUPS
# -------------------------------
def build_main_markup(index: int, total: int, url: str, view_mode: str):
    buttons = []

    page_row = [InlineKeyboardButton(f"📘 Page {index + 1} / {total}", callback_data="log_selector")]
    buttons.append(page_row)

    nav_row = []
    if index > 0:
        nav_row.append(InlineKeyboardButton("⏮️ First", callback_data="log_first"))
        nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data="log_prev"))
    if index < total - 1:
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data="log_next"))
        nav_row.append(InlineKeyboardButton("Last ⏭️", callback_data="log_last"))
    if nav_row:
        buttons.append(nav_row)

    jump_row = []
    if index > 1:
        jump_row.append(InlineKeyboardButton("⏪", callback_data="log_prev2"))
    if index < total - 2:
        jump_row.append(InlineKeyboardButton("⏩", callback_data="log_next2"))
    if jump_row:
        buttons.append(jump_row)

    actions_row = [
        InlineKeyboardButton("🔁 Refresh", callback_data="log_refresh"),
        InlineKeyboardButton(f"{'📥 Tail' if view_mode == 'tail' else '📤 Head'}", callback_data="log_toggle_view_mode"),
        InlineKeyboardButton("📤 Export", callback_data="log_sendfile"),
    ]
    buttons.append(actions_row)

    footer_row = [InlineKeyboardButton("🌍 URL", url=url), InlineKeyboardButton("🚫 Close", callback_data="log_close")]
    buttons.append(footer_row)

    return InlineKeyboardMarkup(buttons)

def build_selector_markup(msg_id: int, page_range_start: int = -1):
    data = LOG_CACHE.get(msg_id)
    if not data:
        return None

    total_pages = data["total_pages"]
    buttons = []

    if total_pages <= 50:
        window_size = 25
        start = data.get("selector_start", 0)
        end = min(start + window_size, total_pages)
        row = []
        for i in range(start, end):
            row.append(InlineKeyboardButton(f"📄 {i + 1}", callback_data=f"log_page_{i}"))
            if len(row) == 5:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        nav_row = []
        if start > 0:
            nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data="selector_prev"))
        nav_row.append(InlineKeyboardButton("🔙 Back", callback_data="selector_back"))
        if end < total_pages:
            nav_row.append(InlineKeyboardButton("Next ➡️", callback_data="selector_next"))
        buttons.append(nav_row)
        return InlineKeyboardMarkup(buttons)

    # Range selector for larger logs
    pages_per_range = 50
    if page_range_start != -1:
        start = page_range_start
        end = min(start + pages_per_range, total_pages)
        row = []
        for i in range(start, end):
            row.append(InlineKeyboardButton(f"📄 {i + 1}", callback_data=f"log_page_{i}"))
            if len(row) == 5:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton("🔙 Back to Ranges", callback_data="log_selector")])
        return InlineKeyboardMarkup(buttons)

    ranges_per_page = 12
    total_ranges = (total_pages + pages_per_range - 1) // pages_per_range
    range_index = data.get("range_index", 0)
    range_index = max(0, min(range_index, total_ranges - 1))
    data["range_index"] = range_index
    start_range = range_index * ranges_per_page
    end_range = min(start_range + ranges_per_page, total_ranges)
    row = []
    for r in range(start_range, end_range):
        start_page = r * pages_per_range + 1
        end_page = min((r + 1) * pages_per_range, total_pages)
        row.append(InlineKeyboardButton(f"📚 {start_page}-{end_page}", callback_data=f"log_range_{r * pages_per_range}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    nav = []
    if range_index > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data="range_prev"))
    nav.append(InlineKeyboardButton("🔙 Back", callback_data="selector_back"))
    if end_range < total_ranges:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data="range_next"))
    buttons.append(nav)
    return InlineKeyboardMarkup(buttons)

# -------------------------------
# LOG COMMAND
# -------------------------------
@Client.on_message(filters.command(["log", "logs"]) & filters.private & CustomFilters.owner, group=10)
async def log_command(client: Client, message: Message):
    try:
        file_path = LOG_FILE_PATH
        if not ospath.exists(file_path) or ospath.getsize(file_path) == 0:
            return await message.reply_text("> Log file not found or is empty.")

        total_pages = get_total_pages(file_path)
        async with aiofiles.open(file_path, 'r') as f:
            await f.seek(0, 2)
            size = await f.tell()
            await f.seek(max(0, size - MAX_PASTE_PAGES * CHUNK_SIZE), 0)
            paste_content = await f.read()

        yaso_url = await paste_to_yaso(paste_content)
        paste_url = yaso_url if not yaso_url.startswith("Error") else await paste_to_fragbin(paste_content)

        view_mode = 'tail'
        index = total_pages - 1
        temp_cache = {"file_path": file_path, "total_pages": total_pages, "url": paste_url,
                      "index": index, "selector_start": 0, "view_mode": view_mode}

        if total_pages == 1:
            minimal_markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔁 Refresh", callback_data="log_refresh")],
                 [InlineKeyboardButton("🌍 URL", url=paste_url)]])
            sent_msg = await message.reply_text(f"<pre>{await get_page(file_path, 0)}</pre>", reply_markup=minimal_markup)
            LOG_CACHE[sent_msg.id] = temp_cache
            return

        initial_page_content = await get_page(file_path, index)
        markup = build_main_markup(index, total_pages, paste_url, view_mode)
        sent_msg = await message.reply_text(f"<pre>{initial_page_content}</pre>", reply_markup=markup, quote=True)
        LOG_CACHE[sent_msg.id] = temp_cache
    except Exception as e:
        LOGGER.exception(f"Error in /log command: {e}")
        await message.reply_text(f"Error: {e}")

# -------------------------------
# CALLBACK HANDLERS
# -------------------------------
# Full set of navigation, selector, toggle, refresh, send file, close
# Using regenerate_expired_log(query) if data is None

# Navigation
@Client.on_callback_query(filters.regex(r"^log_(prev|next|first|last|prev2|next2)$"))
async def navigation_handler(client, query: CallbackQuery):
    msg_id = query.message.id
    data = LOG_CACHE.get(msg_id)
    if not data:
        return await regenerate_expired_log(query)

    action = query.data.split("_")[-1]
    total_pages = data["total_pages"]

    if action == "first":
        if data["index"] == 0:
            return await safe_answer(query, "You are already on the first page.")
        data["index"] = 0
    elif action == "last":
        if data["index"] == total_pages - 1:
            return await safe_answer(query, "You are already on the last page.")
        data["index"] = total_pages - 1
    elif action == "prev":
        if data["index"] == 0:
            return await safe_answer(query, "You are already on the first page.")
        data["index"] -= 1
    elif action == "next":
        if data["index"] == total_pages - 1:
            return await safe_answer(query, "You are already on the last page.")
        data["index"] += 1
    elif action == "prev2":
        data["index"] = max(0, data["index"] - 2)
    elif action == "next2":
        data["index"] = min(total_pages - 1, data["index"] + 2)

    page_content = await get_page(data["file_path"], data["index"])
    markup = build_main_markup(data["index"], total_pages, data["url"], data["view_mode"])
    await query.message.edit_text(f"<pre>{page_content}</pre>", reply_markup=markup)
    await safe_answer(query)

# Selector / Range navigation
@Client.on_callback_query(filters.regex(r"^(log_selector|log_range_\d+|log_page_\d+|selector_prev|selector_next|selector_back|range_prev|range_next)$"))
async def selector_range_handler(client, query: CallbackQuery):
    msg_id = query.message.id
    data = LOG_CACHE.get(msg_id)
    if not data:
        return await regenerate_expired_log(query)

    if query.data == "log_selector":
        markup = build_selector_markup(msg_id)
        if markup:
            await query.message.edit_reply_markup(markup)
        return await safe_answer(query, "Select a page or range")

    if query.data.startswith("log_range_"):
        start = int(query.data.split("_")[-1])
        markup = build_selector_markup(msg_id, page_range_start=start)
        if markup:
            await query.message.edit_reply_markup(markup)
        return await safe_answer(query, f"Showing pages {start+1}-{start+50}")

    if query.data.startswith("log_page_"):
        page_index = int(query.data.split("_")[-1])
        data["index"] = page_index
        page_content = await get_page(data["file_path"], page_index)
        markup = build_main_markup(page_index, data["total_pages"], data["url"], data["view_mode"])
        await query.message.edit_text(f"<pre>{page_content}</pre>", reply_markup=markup)
        return await safe_answer(query, f"Page {page_index+1}")

    # Selector / range navigation buttons
    action = query.data
    if action in ["selector_prev", "selector_next", "selector_back", "range_prev", "range_next"]:
        if action == "selector_prev":
            data["selector_start"] = max(0, data["selector_start"] - 25)
        elif action == "selector_next":
            data["selector_start"] = min(data["selector_start"] + 25, data["total_pages"] - 25)
        elif action == "selector_back":
            markup = build_main_markup(data["index"], data["total_pages"], data["url"], data["view_mode"])
            await query.message.edit_reply_markup(markup)
            return await safe_answer(query)
        elif action == "range_prev":
            data["range_index"] = max(0, data.get("range_index", 0) - 1)
        elif action == "range_next":
            data["range_index"] = data.get("range_index", 0) + 1
        markup = build_selector_markup(msg_id)
        if markup:
            await query.message.edit_reply_markup(markup)
        await safe_answer(query)

# Toggle view mode
@Client.on_callback_query(filters.regex("^log_toggle_view_mode$"))
async def toggle_view_mode(client, query: CallbackQuery):
    msg_id = query.message.id
    data = LOG_CACHE.get(msg_id)
    if not data:
        return await regenerate_expired_log(query)
    if data["view_mode"] == "tail":
        data["view_mode"] = "head"
        data["index"] = 0
    else:
        data["view_mode"] = "tail"
        data["index"] = data["total_pages"] - 1
    page_content = await get_page(data["file_path"], data["index"])
    markup = build_main_markup(data["index"], data["total_pages"], data["url"], data["view_mode"])
    await query.message.edit_text(f"<pre>{page_content}</pre>", reply_markup=markup)
    await safe_answer(query, f"Switched to {'Head' if data['view_mode']=='head' else 'Tail'} mode")

# Refresh
@Client.on_callback_query(filters.regex(r"^log_refresh$"))
async def unified_log_refresh_handler(client, query: CallbackQuery):
    msg_id = query.message.id
    data = LOG_CACHE.get(msg_id)

    # If log context is missing (small log or expired), regenerate
    if not data:
        return await regenerate_expired_log(query)

    try:
        # Reload log file
        file_path = data["file_path"]
        total_pages = get_total_pages(file_path)
        if total_pages == 0:
            await query.message.edit_text("> Log file is empty after refresh.")
            return await safe_answer(query)

        async with aiofiles.open(file_path, 'r') as f:
            await f.seek(0, 2)
            size = await f.tell()
            await f.seek(max(0, size - MAX_PASTE_PAGES * CHUNK_SIZE), 0)
            paste_content = await f.read()

        # Update paste URL
        yaso_url = await paste_to_yaso(paste_content)
        paste_url = yaso_url if not yaso_url.startswith("Error") else await paste_to_fragbin(paste_content)
        data["total_pages"] = total_pages
        data["url"] = paste_url

        # Determine current page index
        if data["view_mode"] == "tail":
            data["index"] = total_pages - 1
        else:
            data["index"] = min(data["index"], total_pages - 1)

        # --- Use minimal markup for single-page logs ---
        if total_pages == 1:
            minimal_markup = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔁 Refresh", callback_data="log_refresh")],
                    [InlineKeyboardButton("🌍 URL", url=paste_url)]
                ]
            )
            page_content = await get_page(file_path, 0)
            await query.message.edit_text(f"<pre>{page_content}</pre>", reply_markup=minimal_markup)
            return await safe_answer(query, "Log refreshed successfully")

        # --- Full markup for multi-page logs ---
        # Show refreshing state for multi-page logs
        markup = build_main_markup(data["index"], data["total_pages"], data["url"], data["view_mode"])
        for row in markup.inline_keyboard:
            for btn in row:
                if btn.callback_data and btn.callback_data.startswith("log_refresh"):
                    btn.text = "Refreshing..."
        await query.message.edit_reply_markup(markup)

        page_content = await get_page(file_path, data["index"])
        final_markup = build_main_markup(data["index"], data["total_pages"], data["url"], data["view_mode"])
        await query.message.edit_text(f"<pre>{page_content}</pre>", reply_markup=final_markup)

        await safe_answer(query, "Log refreshed successfully")

    except Exception as e:
        LOGGER.exception(f"Error in unified_log_refresh_handler: {e}")
        await safe_answer(query, "⚠️ Failed to refresh log.", show_alert=True)


# Send log file
@Client.on_callback_query(filters.regex("^log_sendfile$"))
async def send_log_file(client, query: CallbackQuery):
    msg_id = query.message.id
    data = LOG_CACHE.get(msg_id)
    if not data:
        return await regenerate_expired_log(query)
    path = LOG_FILE_PATH
    if not ospath.exists(path):
        return await safe_answer(query, "❌ Log file not found.", show_alert=True)
    await query.message.reply_document(path, caption="📄 Full log file")
    await safe_answer(query, "Sent log file!")

# Close
@Client.on_callback_query(filters.regex("^log_close$"))
async def log_close_handler(client, query: CallbackQuery):
    msg_id = query.message.id
    LOG_CACHE.pop(msg_id, None)
    try:
        await query.message.delete()
    except Exception:
        pass
    await safe_answer(query, "Closed.")

# Regenerate expired log
async def regenerate_expired_log(query: CallbackQuery):
    await safe_answer(query, "♻️ Regenerating log...", show_alert=True)
    await asyncio.sleep(1)
    
    if not ospath.exists(LOG_FILE_PATH):
        return await query.message.reply_text("> ❌ Log file not found.")

    async with aiofiles.open(LOG_FILE_PATH, "r") as f:
        content = await f.read()

    pages = [content[i:i+CHUNK_SIZE] for i in range(0, len(content), CHUNK_SIZE)]
    paste_content = "".join(pages[-MAX_PASTE_PAGES:]) if len(pages) > MAX_PASTE_PAGES else content

    yaso_url = await paste_to_yaso(paste_content)
    paste_url = yaso_url if not yaso_url.startswith("Error") else await paste_to_fragbin(paste_content)

    total_pages = len(pages)
    index = total_pages - 1

    # --- Single-page minimal UI ---
    if total_pages == 1:
        minimal_markup = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔁 Refresh", callback_data="log_refresh")],
                [InlineKeyboardButton("🌍 URL", url=paste_url)]
            ]
        )
        sent_msg = await query.message.reply_text(f"<pre>{pages[0]}</pre>", reply_markup=minimal_markup, quote=True)

    # --- Multi-page full UI ---
    else:
        markup = build_main_markup(index, total_pages, paste_url, "tail")
        preview_text = "<pre>" + "\n".join(content.strip().splitlines()[-20:]) + "</pre>"
        sent_msg = await query.message.reply_text(preview_text, reply_markup=markup, quote=True)

    # --- Add to LOG_CACHE ---
    LOG_CACHE[sent_msg.id] = {
        "file_path": LOG_FILE_PATH,
        "total_pages": total_pages,
        "url": paste_url,
        "index": index,
        "selector_start": 0,
        "view_mode": "tail"
    }

    try:
        await query.message.delete()  # delete old expired message
    except Exception:
        pass

    LOGGER.info(f"New log regenerated and sent for message_id {sent_msg.id}")
