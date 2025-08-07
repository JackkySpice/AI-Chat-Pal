import os
import sys
import json
import logging
import traceback
from datetime import datetime, timedelta, timezone, time as dtime
from functools import lru_cache
from threading import Thread
from typing import Any, Dict, List, Optional, Tuple

# Configure logging early
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("AIChatPalBot")

# Globals lazily initialized
_DB_CLIENT = None  # type: ignore[var-annotated]
_DB_IS_MOCK = False
_DB_NAME = "chat_history_db"
_COL_USERS = None
_COL_HISTORY = None
_COL_KEYS_IN_USE = None
_GEMINI_CLIENT = None  # type: ignore[var-annotated]

# Simple in-memory rolling logs for /adminJackLogs
_ADMIN_LOGS: List[str] = []
_MAX_ADMIN_LOGS = 500

ADMIN_USERNAME = "Torionllm"

# Constants
FREE_DAILY_LIMIT = 3
HISTORY_MAX_MESSAGES = 20
NEW_CHAT_PROMPT_MINUTES = 5
THINKING_PLACEHOLDER = "Thinking…"
TELEGRAM_CHUNK_LIMIT = 4000
LOCK_FILE = "bot.lock"


def _log_admin(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    entry = f"{ts} | {msg}"
    _ADMIN_LOGS.append(entry)
    if len(_ADMIN_LOGS) > _MAX_ADMIN_LOGS:
        del _ADMIN_LOGS[: len(_ADMIN_LOGS) - _MAX_ADMIN_LOGS]
    logger.info(msg)


def _safe_import_pymongo() -> Tuple[Optional[Any], Optional[Any]]:
    try:
        import pymongo  # type: ignore
        from pymongo import MongoClient  # type: ignore

        return pymongo, MongoClient
    except Exception:
        return None, None


def _safe_import_mongomock() -> Optional[Any]:
    try:
        import mongomock  # type: ignore

        return mongomock
    except Exception:
        return None


def _ensure_indexes(db: Any) -> None:
    try:
        db["history"].create_index("user_id")
    except Exception as e:
        _log_admin(f"Index creation failed: {e}")


def _create_mongo_client() -> Tuple[Any, bool]:
    """Return (client, is_mock). Fallback transparently to mongomock if needed."""
    global _DB_CLIENT, _DB_IS_MOCK, _COL_USERS, _COL_HISTORY, _COL_KEYS_IN_USE

    if _DB_CLIENT is not None:
        return _DB_CLIENT, _DB_IS_MOCK

    uri = os.getenv("MONGODB_URI")
    pymongo, MongoClient = _safe_import_pymongo()
    mongomock = _safe_import_mongomock()

    if uri and MongoClient is not None:
        try:
            client = MongoClient(uri, serverSelectionTimeoutMS=3000)
            client.admin.command("ping")
            _DB_CLIENT = client
            _DB_IS_MOCK = False
            db = client[_DB_NAME]
            _COL_USERS = db["users"]
            _COL_HISTORY = db["history"]
            _COL_KEYS_IN_USE = db["keys_in_use"]
            _ensure_indexes(db)
            _log_admin("Connected to MongoDB")
            return _DB_CLIENT, _DB_IS_MOCK
        except Exception as e:
            _log_admin(f"MongoDB connection failed, using mongomock fallback: {e}")

    if mongomock is not None:
        client = mongomock.MongoClient()
        _DB_CLIENT = client
        _DB_IS_MOCK = True
        db = client[_DB_NAME]
        _COL_USERS = db["users"]
        _COL_HISTORY = db["history"]
        _COL_KEYS_IN_USE = db["keys_in_use"]
        _ensure_indexes(db)
        _log_admin("Using in-memory mongomock database")
        return _DB_CLIENT, _DB_IS_MOCK

    # As a last resort, create a minimal in-memory stub if mongomock is not present
    raise RuntimeError("No database backend available (mongomock missing and MongoDB unreachable)")


def _get_db_collections() -> Tuple[Any, Any, Any]:
    global _COL_USERS, _COL_HISTORY, _COL_KEYS_IN_USE
    if _COL_USERS is None or _COL_HISTORY is None or _COL_KEYS_IN_USE is None:
        _create_mongo_client()
    return _COL_USERS, _COL_HISTORY, _COL_KEYS_IN_USE


@lru_cache(maxsize=4096)
def load_conversation_history(user_id: int) -> List[Dict[str, Any]]:
    """Load conversation history for a user. Returns a new list copy.

    The result is cached. Clear the entire cache after updates.
    """
    try:
        _, col_history, _ = _get_db_collections()
        doc = col_history.find_one({"user_id": user_id})
        if not doc:
            return []
        history = doc.get("conversation_history", [])
        # Ensure each timestamp is a datetime (if stored as string)
        normalized: List[Dict[str, Any]] = []
        for m in history:
            ts = m.get("timestamp")
            if isinstance(ts, str):
                try:
                    ts_dt = datetime.fromisoformat(ts)
                except Exception:
                    ts_dt = datetime.now(timezone.utc)
            elif isinstance(ts, datetime):
                ts_dt = ts
            else:
                ts_dt = datetime.now(timezone.utc)
            normalized.append({
                "role": m.get("role", "user"),
                "content": m.get("content", ""),
                "timestamp": ts_dt,
            })
        return list(normalized)
    except Exception as e:
        _log_admin(f"DB error loading history for {user_id}: {e}")
        return []


def _save_conversation_history(user_id: int, history: List[Dict[str, Any]]) -> None:
    try:
        history = history[-HISTORY_MAX_MESSAGES:]
        col_users, col_history, _ = _get_db_collections()
        col_history.update_one(
            {"user_id": user_id},
            {"$set": {"user_id": user_id, "conversation_history": history}},
            upsert=True,
        )
        load_conversation_history.cache_clear()
    except Exception as e:
        _log_admin(f"DB error saving history for {user_id}: {e}")


def _increment_message_count(user_id: int) -> int:
    try:
        col_users, _, _ = _get_db_collections()
        doc = col_users.find_one({"user_id": user_id})
        current = int(doc.get("message_count", 0)) if doc else 0
        new_count = current + 1
        col_users.update_one(
            {"user_id": user_id},
            {"$set": {"user_id": user_id, "message_count": new_count}},
            upsert=True,
        )
        return new_count
    except Exception as e:
        _log_admin(f"DB error incrementing message_count for {user_id}: {e}")
        return 10**9  # block on error


def _get_message_count(user_id: int) -> int:
    try:
        col_users, _, _ = _get_db_collections()
        doc = col_users.find_one({"user_id": user_id})
        return int(doc.get("message_count", 0)) if doc else 0
    except Exception:
        return 0


def _reset_all_message_counts() -> None:
    try:
        col_users, _, _ = _get_db_collections()
        col_users.update_many({}, {"$set": {"message_count": 0}})
        _log_admin("Daily message counts reset to 0 for all users")
    except Exception as e:
        _log_admin(f"DB error during daily reset: {e}")


def _has_active_key(user_id: int) -> bool:
    try:
        _, _, col_keys = _get_db_collections()
        now = datetime.now(timezone.utc)
        doc = col_keys.find_one({"user_id": user_id})
        if not doc:
            return False
        valid_until = doc.get("valid_until")
        if isinstance(valid_until, str):
            try:
                valid_until = datetime.fromisoformat(valid_until)
            except Exception:
                return False
        if not isinstance(valid_until, datetime):
            return False
        return valid_until >= now
    except Exception as e:
        _log_admin(f"DB error checking active key for {user_id}: {e}")
        return False


def _set_active_key(user_id: int, key: str, valid_until: datetime) -> None:
    try:
        _, _, col_keys = _get_db_collections()
        col_keys.update_one(
            {"user_id": user_id},
            {"$set": {"user_id": user_id, "key": key, "valid_until": valid_until}},
            upsert=True,
        )
    except Exception as e:
        _log_admin(f"DB error setting active key for {user_id}: {e}")


def _logout_key(user_id: int) -> bool:
    try:
        _, _, col_keys = _get_db_collections()
        res = col_keys.delete_one({"user_id": user_id})
        return bool(res.deleted_count)
    except Exception as e:
        _log_admin(f"DB error logging out key for {user_id}: {e}")
        return False


def _chunk_text(text: str, limit: int = TELEGRAM_CHUNK_LIMIT) -> List[str]:
    chunks: List[str] = []
    pos = 0
    n = len(text)
    while pos < n:
        chunks.append(text[pos : pos + limit])
        pos += limit
    if not chunks:
        chunks = [""]
    return chunks


def _build_gemini_contents(conversation_history: List[Dict[str, Any]], latest_user_prompt: Optional[str] = None) -> List[Dict[str, Any]]:
    contents: List[Dict[str, Any]] = []
    for msg in conversation_history[-HISTORY_MAX_MESSAGES:]:
        role = msg.get("role", "user")
        # Map assistant -> model
        role = "model" if role == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": str(msg.get("content", ""))}]})
    if latest_user_prompt is not None:
        contents.append({"role": "user", "parts": [{"text": latest_user_prompt}]})
    return contents


def _get_gemini_client():
    global _GEMINI_CLIENT
    if _GEMINI_CLIENT is not None:
        return _GEMINI_CLIENT

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        from google import genai  # type: ignore

        _GEMINI_CLIENT = genai.Client(api_key=api_key)
        return _GEMINI_CLIENT
    except Exception as e:
        _log_admin(f"Failed to initialize Gemini client: {e}")
        return None


def _extract_text_from_response(resp: Any) -> str:
    # Best-effort extraction across possible SDK shapes
    try:
        txt = getattr(resp, "text", None)
        if txt:
            return str(txt)
        txt = getattr(resp, "output_text", None)
        if txt:
            return str(txt)
        # Try candidates tree
        candidates = getattr(resp, "candidates", None)
        if candidates:
            for cand in candidates:
                content = getattr(cand, "content", None) or cand.get("content") if isinstance(cand, dict) else None
                if content:
                    parts = getattr(content, "parts", None) or content.get("parts") if isinstance(content, dict) else None
                    if parts:
                        for p in parts:
                            text_val = getattr(p, "text", None) or (p.get("text") if isinstance(p, dict) else None)
                            if text_val:
                                return str(text_val)
    except Exception:
        pass
    return ""


def _stream_gemini_response(contents: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    """Return (text, error)."""
    client = _get_gemini_client()
    if client is None:
        return None, "Gemini is not configured. Please set GEMINI_API_KEY."

    model = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
    system_prompt = os.getenv("GEMINI_SYSTEM_PROMPT")

    cfg = None
    try:
        # Newer SDKs provide typed configs; fall back to dict if not available
        from google.genai import types as genai_types  # type: ignore

        thinking_cfg = None
        try:
            # Some SDKs may name it ThinkingConfig; accept failures silently
            thinking_cfg = genai_types.ThinkingConfig(budget_tokens=-1)
        except Exception:
            try:
                thinking_cfg = {"thinking_budget": -1}
            except Exception:
                thinking_cfg = None

        cfg = genai_types.GenerateContentConfig(
            system_instruction=system_prompt if system_prompt else None,
            thinking_config=thinking_cfg,
        )
    except Exception:
        # Fallback plain dict config
        cfg = {"system_instruction": system_prompt} if system_prompt else None

    try:
        # Prefer streaming
        stream = client.models.generate_content_stream(
            model=model, contents=contents, config=cfg
        )
        aggregated = []
        for chunk in stream:
            try:
                text_piece = getattr(chunk, "text", None)
                if text_piece:
                    aggregated.append(str(text_piece))
            except Exception:
                pass
        final_text = "".join(aggregated).strip()
        if not final_text:
            # Fallback non-stream call
            resp = client.models.generate_content(
                model=model, contents=contents, config=cfg
            )
            final_text = _extract_text_from_response(resp)
        if not final_text:
            final_text = "(No response)"
        return final_text, None
    except Exception as e:
        err = f"Gemini error: {e}"
        _log_admin(err)
        return None, err


def _send_large_message(context: Any, chat_id: int, text: str, placeholder_message: Any) -> None:
    try:
        chunks = _chunk_text(text)
        # Edit placeholder with the first chunk
        first = chunks[0]
        context.bot.edit_message_text(
            chat_id=chat_id, message_id=placeholder_message.message_id, text=first
        )
        # Send remaining chunks
        for extra in chunks[1:]:
            context.bot.send_message(chat_id=chat_id, text=extra)
    except Exception as e:
        _log_admin(f"Failed to send large message: {e}")


def _should_ask_new_chat(history: List[Dict[str, Any]]) -> bool:
    if not history:
        return False
    last_ts = history[-1].get("timestamp")
    if not isinstance(last_ts, datetime):
        return False
    now = datetime.now(timezone.utc)
    return (now - last_ts) > timedelta(minutes=NEW_CHAT_PROMPT_MINUTES)


def _start_web_server_background() -> Thread:
    def run_flask():
        try:
            from flask import Flask

            app = Flask(__name__)

            @app.route("/")
            def index():
                return "Hello, world!"

            # Suppress werkzeug noisy logs in production
            log = logging.getLogger("werkzeug")
            log.setLevel(logging.WARNING)

            app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False)
        except Exception as e:
            _log_admin(f"Flask server failed: {e}")

    t = Thread(target=run_flask, name="flask-server", daemon=True)
    t.start()
    _log_admin("Flask server started on 0.0.0.0:8080")
    return t


def _run_bot() -> None:
    # Import telegram-bot libs lazily to keep import main lightweight
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from telegram.ext import (
            Updater,
            CommandHandler,
            MessageHandler,
            Filters,
            CallbackQueryHandler,
            CallbackContext,
        )
    except Exception as e:
        _log_admin(f"python-telegram-bot import failed: {e}")
        return

    # Handlers
    def start(update, context: CallbackContext):
        update.message.reply_text(
            "Welcome to AIChatPalBot! Use /help to see available commands."
        )

    def help_command(update, context: CallbackContext):
        help_text = (
            "Commands:\n"
            "/start - Welcome message\n"
            "/help - Show this help\n"
            "/key <your_key> - Activate unlimited access until key expiry\n"
            "/newchat - Start a new conversation\n"
            "/refresh - Refresh caches and connections\n"
            "/logout - Remove active key\n"
            "/adminJackLogs - Admin only\n"
            "Free users get 3 messages/day."
        )
        update.message.reply_text(help_text)

    def refresh(update, context: CallbackContext):
        global _GEMINI_CLIENT
        _GEMINI_CLIENT = None
        load_conversation_history.cache_clear()
        update.message.reply_text("Refreshed.")

    def newchat(update, context: CallbackContext):
        user_id = update.effective_user.id
        _save_conversation_history(user_id, [])
        update.message.reply_text("Started a new chat.")

    def admin_logs(update, context: CallbackContext):
        user = update.effective_user
        if not user or (user.username != ADMIN_USERNAME):
            update.message.reply_text("Unauthorized.")
            return
        try:
            col_users, col_history, col_keys = _get_db_collections()
            users_count = col_users.count_documents({})
            history_count = col_history.count_documents({})
            keys_count = col_keys.count_documents({})
        except Exception:
            users_count = history_count = keys_count = -1
        tail = "\n".join(_ADMIN_LOGS[-30:]) if _ADMIN_LOGS else "(no logs)"
        msg = (
            f"DB: users={users_count}, history={history_count}, keys_in_use={keys_count}\n\n"
            f"Recent logs:\n{tail}"
        )
        update.message.reply_text(msg)

    def logout(update, context: CallbackContext):
        user_id = update.effective_user.id
        removed = _logout_key(user_id)
        if removed:
            update.message.reply_text("Key removed.")
        else:
            update.message.reply_text("No active key to remove.")

    def key(update, context: CallbackContext):
        # /key <your_key>
        try:
            from keys import DEMO_KEYS
        except Exception:
            DEMO_KEYS = {}
        args = context.args or []
        if not args:
            update.message.reply_text("Usage: /key <your_key>")
            return
        provided = args[0].strip()
        valid_until = DEMO_KEYS.get(provided)
        if not valid_until:
            update.message.reply_text("Invalid key.")
            return
        user_id = update.effective_user.id
        _set_active_key(user_id, provided, valid_until)
        update.message.reply_text(
            f"Key activated. Valid until: {valid_until.isoformat()}"
        )

    def _rate_limited(user_id: int) -> bool:
        if _has_active_key(user_id):
            return False
        # Free user
        current = _get_message_count(user_id)
        if current >= FREE_DAILY_LIMIT:
            return True
        # Increment now to count this attempt
        _increment_message_count(user_id)
        return False

    def ask_for_new_chat(update, context: CallbackContext, pending_text: str):
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Yes", callback_data="yes_newchat"),
                InlineKeyboardButton("No, continue", callback_data="no_continue"),
            ]
        ])
        context.user_data["pending_prompt"] = pending_text
        update.message.reply_text(
            "It has been a while. Start a new chat?", reply_markup=kb
        )

    def button(update, context: CallbackContext):
        query = update.callback_query
        user_id = query.from_user.id
        data = query.data or ""
        query.answer()
        if data == "yes_newchat":
            _save_conversation_history(user_id, [])
            context.user_data.pop("pending_prompt", None)
            query.edit_message_text("Started a new chat. Send your message again.")
        elif data == "no_continue":
            # Continue with pending prompt if any
            pending = context.user_data.pop("pending_prompt", None)
            if pending:
                # Simulate as if user just sent it
                class FakeMsg:
                    def __init__(self, chat_id, message_id):
                        self.chat_id = chat_id
                        self.message_id = message_id
                # We can't reuse the same message, just instruct user
                query.edit_message_text("Okay, continuing. Processing your last message…")
                _process_user_message(query.message.chat_id, user_id, pending, context)
            else:
                query.edit_message_text("Okay, continuing.")
        else:
            query.edit_message_text("Unknown action.")

    def _process_user_message(chat_id: int, user_id: int, text: str, context: CallbackContext):
        if _rate_limited(user_id):
            context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "Daily free limit reached (3/day). Use /key to unlock unlimited access."
                ),
            )
            return

        history = load_conversation_history(user_id)
        now = datetime.now(timezone.utc)
        history.append({"role": "user", "content": text, "timestamp": now})

        # Prepare and send placeholder
        placeholder = context.bot.send_message(chat_id=chat_id, text=THINKING_PLACEHOLDER)

        # Call Gemini
        contents = _build_gemini_contents(history)
        reply_text, err = _stream_gemini_response(contents)
        if err or not reply_text:
            err_text = err or "Unknown error"
            try:
                context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=placeholder.message_id,
                    text=f"Error: {err_text}",
                )
            except Exception:
                context.bot.send_message(chat_id=chat_id, text=f"Error: {err_text}")
            return

        # Save assistant message and persist history
        history.append({
            "role": "assistant",
            "content": reply_text,
            "timestamp": datetime.now(timezone.utc),
        })
        _save_conversation_history(user_id, history)

        # Send final text, chunked; edit the placeholder for first chunk
        _send_large_message(context, chat_id, reply_text, placeholder)

    def respond(update, context: CallbackContext):
        if not update.message or not update.message.text:
            return
        user_id = update.effective_user.id
        text = update.message.text
        history = load_conversation_history(user_id)
        if _should_ask_new_chat(history):
            ask_for_new_chat(update, context, text)
            return
        _process_user_message(update.message.chat_id, user_id, text, context)

    def reset_daily_limit(context: CallbackContext):
        _reset_all_message_counts()

    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        _log_admin("TELEGRAM_TOKEN not set. Bot will not start.")
        return

    # Single-instance guard
    if os.path.exists(LOCK_FILE):
        _log_admin("bot.lock exists. Another instance may be running. Exiting.")
        return

    with open(LOCK_FILE, "w") as f:
        f.write(f"PID={os.getpid()}\nSTART={datetime.now(timezone.utc).isoformat()}\n")

    updater = None
    try:
        updater = Updater(token=token, use_context=True)
        dp = updater.dispatcher

        # Register handlers
        dp.add_handler(CommandHandler("start", start))
        dp.add_handler(CommandHandler("help", help_command))
        dp.add_handler(CommandHandler("refresh", refresh))
        dp.add_handler(CommandHandler("newchat", newchat))
        dp.add_handler(CommandHandler("logout", logout))
        dp.add_handler(CommandHandler("key", key, pass_args=True))
        dp.add_handler(CommandHandler("adminJackLogs", admin_logs))
        dp.add_handler(CallbackQueryHandler(button))
        dp.add_handler(MessageHandler(Filters.text & ~Filters.command, respond))

        # Schedule daily reset at 00:00 server time
        try:
            local_midnight = dtime(hour=0, minute=0)  # server local time
            updater.job_queue.run_daily(lambda ctx: reset_daily_limit(ctx), time=local_midnight)
        except Exception as e:
            _log_admin(f"Failed to schedule daily reset: {e}")

        _log_admin("Starting bot polling…")
        updater.start_polling(clean=True)
        updater.idle()
    finally:
        try:
            if os.path.exists(LOCK_FILE):
                os.remove(LOCK_FILE)
        except Exception:
            pass


def main() -> None:
    # Start Flask first
    flask_thread = _start_web_server_background()

    # Start bot if token present
    if os.getenv("TELEGRAM_TOKEN"):
        _run_bot()
    else:
        _log_admin("Running Flask only (no TELEGRAM_TOKEN provided)")
        # Keep process alive by joining Flask thread
        try:
            flask_thread.join()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()