import os
import sys
import json
import logging
import traceback
from datetime import datetime, timedelta, timezone, time as dtime
from functools import lru_cache
from threading import Thread
from time import sleep
from typing import Any, Dict, List, Optional, Tuple
from dotenv import load_dotenv

# Configure logging early
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("AIChatPalWeb")

# Load .env for local development and container setups
load_dotenv()

# Globals lazily initialized
_DB_CLIENT = None  # type: ignore[var-annotated]
_DB_IS_MOCK = False
_DB_NAME = "chat_history_db"
_COL_USERS = None
_COL_HISTORY = None
_COL_KEYS_IN_USE = None
_COL_CONVERSATIONS = None
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
LOCK_FILE = "bot.lock"  # no longer used, kept for compatibility


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
        try:
            db["history"].create_index("conversation_id")
        except Exception:
            pass
        try:
            db["conversations"].create_index([("user_id", 1), ("updated_at", -1)])
        except Exception:
            pass
    except Exception as e:
        _log_admin(f"Index creation failed: {e}")


def _create_mongo_client() -> Tuple[Any, bool]:
    """Return (client, is_mock). Fallback transparently to mongomock if needed."""
    global _DB_CLIENT, _DB_IS_MOCK, _COL_USERS, _COL_HISTORY, _COL_KEYS_IN_USE, _COL_CONVERSATIONS

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
            _COL_CONVERSATIONS = db["conversations"]
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
        _COL_CONVERSATIONS = db["conversations"]
        _ensure_indexes(db)
        _log_admin("Using in-memory mongomock database")
        return _DB_CLIENT, _DB_IS_MOCK

    # As a last resort, create a minimal in-memory stub if mongomock is not present
    raise RuntimeError("No database backend available (mongomock missing and MongoDB unreachable)")


def _get_db_collections() -> Tuple[Any, Any, Any, Any]:
    global _COL_USERS, _COL_HISTORY, _COL_KEYS_IN_USE, _COL_CONVERSATIONS
    if _COL_USERS is None or _COL_HISTORY is None or _COL_KEYS_IN_USE is None or _COL_CONVERSATIONS is None:
        _create_mongo_client()
    return _COL_USERS, _COL_HISTORY, _COL_KEYS_IN_USE, _COL_CONVERSATIONS


@lru_cache(maxsize=4096)
def load_conversation_history(user_id: int, conversation_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Load conversation history for a user and conversation. Returns a new list copy.

    The result is cached. Clear the entire cache after updates.
    """
    try:
        _, col_history, _, _ = _get_db_collections()
        query: Dict[str, Any] = {"user_id": user_id}
        if conversation_id is not None:
            query["conversation_id"] = conversation_id
        doc = col_history.find_one(query)
        if not doc and conversation_id is not None:
            # Fallback to legacy single-history doc
            doc = col_history.find_one({"user_id": user_id, "conversation_id": {"$exists": False}})
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


def _save_conversation_history(user_id: int, history: List[Dict[str, Any]], conversation_id: Optional[str] = None) -> None:
    try:
        history = history[-HISTORY_MAX_MESSAGES:]
        col_users, col_history, _, _ = _get_db_collections()
        update_filter: Dict[str, Any] = {"user_id": user_id}
        if conversation_id is not None:
            update_filter["conversation_id"] = conversation_id
        col_history.update_one(
            update_filter,
            {"$set": {"user_id": user_id, "conversation_id": conversation_id, "conversation_history": history}},
            upsert=True,
        )
        load_conversation_history.cache_clear()
    except Exception as e:
        _log_admin(f"DB error saving history for {user_id}: {e}")


def _increment_message_count(user_id: int) -> int:
    try:
        col_users, _, _, _ = _get_db_collections()
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
        col_users, _, _, _ = _get_db_collections()
        doc = col_users.find_one({"user_id": user_id})
        return int(doc.get("message_count", 0)) if doc else 0
    except Exception:
        return 0


def _reset_all_message_counts() -> None:
    try:
        col_users, _, _, _ = _get_db_collections()
        col_users.update_many({}, {"$set": {"message_count": 0}})
        _log_admin("Daily message counts reset to 0 for all users")
    except Exception as e:
        _log_admin(f"DB error during daily reset: {e}")


def _has_active_key(user_id: int) -> bool:
    try:
        _, _, col_keys, _ = _get_db_collections()
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
        _, _, col_keys, _ = _get_db_collections()
        col_keys.update_one(
            {"user_id": user_id},
            {"$set": {"user_id": user_id, "key": key, "valid_until": valid_until}},
            upsert=True,
        )
    except Exception as e:
        _log_admin(f"DB error setting active key for {user_id}: {e}")


def _logout_key(user_id: int) -> bool:
    try:
        _, _, col_keys, _ = _get_db_collections()
        res = col_keys.delete_one({"user_id": user_id})
        return bool(res.deleted_count)
    except Exception as e:
        _log_admin(f"DB error logging out key for {user_id}: {e}")
        return False


def _build_gemini_contents(conversation_history: List[Dict[str, Any]], latest_user_prompt: Optional[str] = None, latest_attachments: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    contents: List[Dict[str, Any]] = []
    window = conversation_history[-HISTORY_MAX_MESSAGES:]
    for idx, msg in enumerate(window):
        role = msg.get("role", "user")
        role = "model" if role == "assistant" else "user"
        parts: List[Dict[str, Any]] = [{"text": str(msg.get("content", ""))}]
        # If this is the latest user message, append any provided attachments as inline_data parts
        if idx == len(window) - 1 and role == "user" and latest_attachments:
            for att in latest_attachments:
                # Expecting items like {"inline_data": {"mime_type": ..., "data": ...}}
                try:
                    inline_data = att.get("inline_data") if isinstance(att, dict) else None
                    if inline_data and isinstance(inline_data, dict) and inline_data.get("data"):
                        parts.append({"inline_data": {"mime_type": str(inline_data.get("mime_type") or inline_data.get("mimeType") or "application/octet-stream"), "data": str(inline_data.get("data"))}})
                except Exception:
                    pass
        contents.append({"role": role, "parts": parts})
    if latest_user_prompt is not None:
        parts2: List[Dict[str, Any]] = [{"text": latest_user_prompt}]
        if latest_attachments:
            for att in latest_attachments:
                try:
                    inline_data = att.get("inline_data") if isinstance(att, dict) else None
                    if inline_data and isinstance(inline_data, dict) and inline_data.get("data"):
                        parts2.append({"inline_data": {"mime_type": str(inline_data.get("mime_type") or inline_data.get("mimeType") or "application/octet-stream"), "data": str(inline_data.get("data"))}})
                except Exception:
                    pass
        contents.append({"role": "user", "parts": parts2})
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
        # Prefer streaming (aggregated server-side for now)
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


def _estimate_base64_bytes(data_b64: str) -> int:
    """Estimate decoded bytes of a base64 string without allocating large buffers.

    Uses length-based estimation: floor(n * 3 / 4) minus padding. Returns 0 on error.
    """
    try:
        s = data_b64.strip()
        n = len(s)
        padding = 2 if s.endswith("==") else (1 if s.endswith("=") else 0)
        return max(0, (n * 3) // 4 - padding)
    except Exception:
        return 0


def _start_daily_reset_thread_if_enabled() -> None:
    """Start a background thread to reset daily free message counts at local midnight.

    Controlled by ENABLE_DAILY_RESET_THREAD env (default enabled).
    """
    flag = os.getenv("ENABLE_DAILY_RESET_THREAD", "1").lower()
    if flag not in ("1", "true", "yes", "on"):  # disabled
        return

    def _worker() -> None:
        while True:
            try:
                now = datetime.now()
                tomorrow = now.date() + timedelta(days=1)
                next_midnight = datetime.combine(tomorrow, dtime(0, 0))
                sleep_secs = max(1, int((next_midnight - now).total_seconds()))
                sleep(sleep_secs)
                _reset_all_message_counts()
            except Exception as e:
                _log_admin(f"Daily reset thread error: {e}")
                sleep(3600)

    Thread(target=_worker, daemon=True).start()

# -------------------------- Web App --------------------------
from flask import Flask, request, jsonify, make_response, Response, stream_with_context
import secrets


HTML_INDEX = """
<!doctype html>
<html lang="en" class="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content">
  <title>AIChatPal</title>
  <meta name="description" content="AIChatPal — Clean, fast AI chat powered by Gemini.">
  <meta property="og:title" content="AIChatPal"/>
  <meta property="og:description" content="Clean, fast AI chat powered by Gemini."/>
  <meta property="og:type" content="website"/>
  <meta property="og:image" content="/icon.svg"/>
  <meta name="twitter:card" content="summary_large_image"/>
  <meta name="theme-color" content="#0c0c0f">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <script src="https://cdn.tailwindcss.com?plugins=typography,forms"></script>
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        fontFamily: { sans: ['Inter','ui-sans-serif','system-ui','Segoe UI','Roboto','Helvetica','Arial'] },
        extend: {
          colors: {
            ink: '#0c0c0f',
            luxe: {
              50: '#f6f6f7',
              100: '#e7e7ea',
              900: '#0b0b10'
            }
          },
          boxShadow: {
            raised: '0 8px 24px -10px rgba(0,0,0,.6)',
            luxe: '0 12px 40px -12px rgba(0,0,0,.65)'
          },
          keyframes: {
            fadeIn: { '0%': { opacity: 0 }, '100%': { opacity: 1 } },
            slideUp: { '0%': { transform: 'translateY(6px)', opacity: 0 }, '100%': { transform: 'translateY(0)', opacity: 1 } },
            slideInLeft: { '0%': { transform: 'translateX(-8px)', opacity: 0 }, '100%': { transform: 'translateX(0)', opacity: 1 } }
          },
          animation: {
            'fade-in': 'fadeIn 180ms ease-out',
            'slide-up': 'slideUp 200ms cubic-bezier(.2,.8,.2,1)',
            'slide-in-left': 'slideInLeft 220ms cubic-bezier(.2,.8,.2,1)'
          }
        }
      }
    };
  </script>
  <link rel="manifest" href="/manifest.json"/>
  <link rel="icon" href="/icon.svg" sizes="any" type="image/svg+xml"/>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/themes/prism-tomorrow.min.css"/>
  <style>
    :root { color-scheme: dark; -webkit-text-size-adjust: 100%; }
    html, body { height: 100%; }
    body { background:
      radial-gradient(80rem 40rem at 20% -10%, rgba(80,80,120,.16), transparent 45%),
      radial-gradient(60rem 30rem at 110% 10%, rgba(60,90,60,.14), transparent 40%),
      linear-gradient(#0b0b10, #09090c);
      position: relative;
      overflow: hidden;
    }
    body::before { /* subtle noise overlay */
      content: "";
      position: fixed; inset: 0; pointer-events: none; z-index: 0;
      background-image: radial-gradient(rgba(255,255,255,0.03) 1px, transparent 1px);
      background-size: 3px 3px;
      opacity: .5;
    }
    .safe-bottom { padding-bottom: max(env(safe-area-inset-bottom), 0px); }
    .msg { max-width: 72ch; }
    .skeleton { position: relative; overflow: hidden; }
    .skeleton::after { content: ""; position: absolute; inset: 0; background: linear-gradient(110deg, transparent 0%, rgba(255,255,255,.06) 40%, rgba(255,255,255,.12) 60%, transparent 100%); animation: shimmer 1.1s linear infinite; }
    @keyframes shimmer { 0% { transform: translateX(-100%);} 100% { transform: translateX(100%);} }
    pre { position: relative; }
    pre code { white-space: pre-wrap; word-break: break-word; }
    .copy-btn { position: absolute; top: .5rem; right: .5rem; }
    .gradient-border { position: relative; }
    .gradient-border::before { content: ""; position: absolute; inset: -1px; border-radius: inherit; padding: 1px; background: linear-gradient(135deg, rgba(62,206,153,.8), rgba(88,88,120,.7), rgba(62,206,153,.8)); -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0); -webkit-mask-composite: xor; mask-composite: exclude; pointer-events: none; }
    @media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }
  </style>
</head>
<body class="font-sans bg-ink text-zinc-100">
  <div class="min-h-[100svh] relative z-10 flex">
    <aside id="sidebar" class="hidden md:flex fixed md:static left-0 top-0 bottom-0 w-72 bg-zinc-950/70 border-r border-zinc-900 backdrop-blur z-40 transform md:transform-none -translate-x-full md:translate-x-0 transition-transform">
      <div class="flex flex-col h-full w-full">
        <div class="h-14 px-3 flex items-center justify-between border-b border-zinc-900/70">
          <div class="text-sm text-zinc-300">Conversations</div>
          <button id="newChatBtn" class="px-2.5 py-1 rounded-lg text-xs bg-emerald-600/90 hover:bg-emerald-600 text-white shadow">New</button>
        </div>
        <div id="convoList" class="flex-1 overflow-auto p-2 space-y-1"></div>
        <div class="p-2 text-[11px] text-zinc-500 border-t border-zinc-900/70">AIChatPal Premier</div>
      </div>
    </aside>

    <div class="flex-1 min-w-0 flex flex-col">
      <header class="sticky top-0 z-30 h-14 px-3 flex items-center justify-between bg-gradient-to-b from-ink/90 to-transparent border-b border-zinc-900/50 backdrop-blur">
        <div class="flex items-center gap-2">
                     <button id="sidebarToggle" class="md:hidden px-2 py-1.5 rounded-lg border border-zinc-800 bg-zinc-950/50 text-zinc-300" aria-label="Toggle conversations" aria-controls="sidebar" aria-expanded="false">☰</button>
          <div class="px-3 py-1.5 rounded-full text-sm bg-zinc-900/60 border border-zinc-800 text-zinc-200 shadow backdrop-blur">✨ AIChatPal Premier</div>
        </div>
                   <div class="text-xs text-zinc-500 flex items-center gap-2"><span>Fast, polished AI chat</span><select id="modelSelect" class="bg-zinc-900/60 border border-zinc-800 rounded px-2 py-1 text-zinc-200 text-xs"><option value="gemini-2.5-pro">Accurate</option><option value="gemini-2.0-flash">Fast</option></select><button id="themeToggle" class="px-2 py-1 rounded border border-zinc-800 text-zinc-300">Theme</button></div>
      </header>

      <main class="flex-1">
        <div class="max-w-3xl mx-auto w-full px-3">
          <div id="chat" class="pt-4 pb-28 space-y-3" role="log" aria-live="polite" aria-relevant="additions"></div>
        </div>
      </main>

      <div class="fixed inset-x-0 bottom-0 z-40 safe-bottom bg-gradient-to-t from-ink to-ink/95 border-t border-zinc-900/70">
        <form id="composer" class="max-w-3xl mx-auto px-3 py-3">
          <div class="flex items-end gap-2">
                         <div id="composerBox" class="flex-1 rounded-2xl bg-zinc-950/60 border border-zinc-900 focus-within:border-zinc-700 transition-colors relative backdrop-blur gradient-border" role="group" aria-label="Message composer">
              <div id="attachmentPreview" class="px-3 pt-3 pb-0 hidden flex-wrap gap-2"></div>
              <div class="flex items-center">
                <button type="button" id="attachBtn" class="shrink-0 p-3 text-zinc-400 hover:text-zinc-200 transition-colors" title="Attach" aria-label="Attachments" aria-haspopup="menu" aria-expanded="false" aria-controls="attachMenu">＋</button>
                <textarea id="input" rows="1" placeholder="Ask anything" enterkeyhint="send" autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false" inputmode="text" aria-label="Message" class="flex-1 bg-transparent text-zinc-100 placeholder:text-zinc-500 p-3 focus:outline-none resize-none"></textarea>
              </div>
                             <div id="attachMenu" class="hidden absolute bottom-[54px] left-2 w-56 rounded-xl border border-zinc-800 bg-zinc-950/90 shadow-luxe backdrop-blur p-1 animate-slide-up" role="menu" aria-label="Attachment options">
                <button type="button" id="actionAddPhotos" role="menuitem" class="w-full flex items-center gap-2 px-3 py-2 rounded-lg hover:bg-zinc-900 text-zinc-200 transition-colors"><span>🖼️</span><span>Add photos</span></button>
                <button type="button" id="actionTakePhoto" role="menuitem" class="w-full flex items-center gap-2 px-3 py-2 rounded-lg hover:bg-zinc-900 text-zinc-200 transition-colors"><span>📷</span><span>Take photo</span></button>
                <button type="button" id="actionAddFiles" role="menuitem" class="w-full flex items-center gap-2 px-3 py-2 rounded-lg hover:bg-zinc-900 text-zinc-200 transition-colors"><span>📎</span><span>Add files</span></button>
                <input id="photosInput" type="file" accept="image/*" multiple class="hidden" />
                <input id="cameraInput" type="file" accept="image/*" capture="environment" class="hidden" />
                <input id="filesInput" type="file" multiple class="hidden" />
              </div>
            </div>
                            <button id="send" type="submit" class="h-12 w-12 rounded-full bg-emerald-600 hover:bg-emerald-500 text-white shadow-raised grid place-items-center transition-colors" aria-label="Send message">
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="w-5 h-5"><path d="M6 15l6-6 6 6"/></svg>
            </button>
          </div>
                     <div class="mt-2 text-[12px] text-zinc-500 flex items-center gap-2" id="limit"><span id="limitText"></span><button id="unlockBtn" type="button" class="px-2 py-1 rounded border border-emerald-700 text-emerald-400 hover:bg-emerald-700/10">Unlock unlimited</button></div>
        </form>
      </div>
    </div>
  </div>

  <div id="toasts" class="fixed bottom-[96px] left-1/2 -translate-x-1/2 space-y-2 z-50"></div>

  <dialog id="unlockDialog" class="rounded-xl border border-zinc-800 bg-zinc-950/95 p-4 text-sm text-zinc-200">
    <form method="dialog">
      <div class="mb-2 font-semibold">Unlock unlimited</div>
      <input id="unlockKey" type="text" placeholder="Enter access key" class="w-full bg-zinc-900/60 border border-zinc-800 rounded px-2 py-1 text-zinc-100 mb-2" />
      <div class="flex justify-end gap-2">
        <button type="button" id="unlockSubmit" class="px-3 py-1 rounded bg-emerald-600 text-white">Activate</button>
        <button value="cancel" class="px-3 py-1 rounded border border-zinc-700">Close</button>
      </div>
    </form>
  </dialog>

  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js" defer></script>
  <script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.7/dist/purify.min.js" defer></script>
  <script>
  window.addEventListener('DOMContentLoaded', () => {
  const chat = document.getElementById('chat');
  const input = document.getElementById('input');
  const sendBtn = document.getElementById('send');
  const limitP = document.getElementById('limitText');
  const attachBtn = document.getElementById('attachBtn');
  const attachMenu = document.getElementById('attachMenu');
  const photosInput = document.getElementById('photosInput');
  const cameraInput = document.getElementById('cameraInput');
  const filesInput = document.getElementById('filesInput');
  const attachmentPreview = document.getElementById('attachmentPreview');
  const composerBox = document.getElementById('composerBox');
  const sidebar = document.getElementById('sidebar');
  const sidebarToggle = document.getElementById('sidebarToggle');
  const convoList = document.getElementById('convoList');
  const newChatBtn = document.getElementById('newChatBtn');
  const modelSelect = document.getElementById('modelSelect');
  const themeToggle = document.getElementById('themeToggle');
  document.documentElement.style.setProperty('content-visibility', 'auto');

  const MAX_ATTACHMENTS = 5;
  const MAX_FILE_SIZE = 8 * 1024 * 1024; // 8MB per file
  const MAX_TOTAL_SIZE = 12 * 1024 * 1024; // 12MB per message
  let pendingAttachments = [];
  let readingCount = 0;
  let currentCid = null;
  let abortController = null;

  function showToast(message, variant = 'default', timeout = 2200) {
    const host = document.getElementById('toasts');
    const node = document.createElement('div');
    const colors = variant === 'success' ? 'from-emerald-600 to-green-600' : variant === 'error' ? 'from-rose-600 to-pink-600' : 'from-zinc-700 to-zinc-900';
    node.className = `text-sm text-white px-4 py-2 rounded-xl shadow-raised bg-gradient-to-r ${colors} animate-slide-up`;
    node.textContent = message;
    host.appendChild(node);
    setTimeout(() => { node.style.opacity = '0'; node.style.transform = 'translateY(6px)'; setTimeout(() => node.remove(), 180); }, timeout);
  }

  function autoResizeTextarea(el){ el.style.height = 'auto'; el.style.height = (el.scrollHeight) + 'px'; }

  function attachCopyHandlers(root = chat){
    (root.querySelectorAll('pre') || []).forEach(pre => {
      if (pre.dataset.copyReady) return;
      pre.dataset.copyReady = '1';
      pre.style.overflow = 'auto';
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'copy-btn px-2 py-1 rounded-md text-xs bg-black/70 text-white hover:brightness-110';
      btn.textContent = 'Copy';
      btn.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(pre.innerText.trim()); showToast('Copied','success'); }
        catch(e){ showToast('Copy failed','error'); }
      });
      pre.appendChild(btn);
    });
  }

  marked.setOptions({ breaks: true, gfm: true });
  let prismLoaded = false;
  async function ensurePrism(){
    if (prismLoaded) return;
    try {
      await new Promise((resolve, reject) => {
        const s = document.createElement('script'); s.src = 'https://cdn.jsdelivr.net/npm/prismjs@1.29.0/prism.min.js'; s.onload = resolve; s.onerror = reject; document.head.appendChild(s);
      });
      prismLoaded = true;
    } catch(_) {}
  }
  function renderMarkdownToHtml(md) {
    const dirty = marked.parse(md || '');
    const clean = DOMPurify.sanitize(dirty, { USE_PROFILES: { html: true } });
    const wrapper = document.createElement('div');
    wrapper.innerHTML = clean;
    wrapper.querySelectorAll('a').forEach(a => { a.target = '_blank'; a.rel = 'noopener noreferrer'; });
    wrapper.querySelectorAll('pre').forEach(p => p.classList.add('not-prose','rounded-lg','border','border-zinc-800'));
    if (!prismLoaded && wrapper.querySelector('pre code')){ ensurePrism().then(()=>{ if (window.Prism && window.Prism.highlightAllUnder) { window.Prism.highlightAllUnder(wrapper); } }); }
    else if (window.Prism && window.Prism.highlightAllUnder) { window.Prism.highlightAllUnder(wrapper); }
    return wrapper.innerHTML;
  }

  function renderAttachmentTiles(attachments){
    if (!attachments || !attachments.length) return null;
    const grid = document.createElement('div');
    grid.className = 'grid grid-cols-3 gap-2 mb-2 animate-fade-in';
    attachments.forEach(a => {
      if (a.kind === 'image'){
        const img = document.createElement('img');
        img.src = a.base64; img.alt = a.name; img.loading = 'lazy';
        img.className = 'w-full h-24 object-cover rounded-lg border border-zinc-800';
        grid.appendChild(img);
      } else {
        const card = document.createElement('div');
        card.className = 'rounded-lg border border-zinc-800 bg-zinc-900/60 p-2 text-xs text-zinc-300 flex items-center gap-2';
        const icon = document.createElement('div'); icon.textContent = '📎';
        const label = document.createElement('div'); label.className = 'truncate'; label.textContent = a.name || 'file';
        card.appendChild(icon); card.appendChild(label);
        grid.appendChild(card);
      }
    });
    return grid;
  }

  function bubble(role, content, attachments){
    const row = document.createElement('div');
    row.className = 'w-full flex items-start gap-3 ' + (role === 'user' ? 'justify-end' : 'justify-start');
    const isUser = role === 'user';
    const bubble = document.createElement('div');
    bubble.className = 'msg rounded-2xl px-4 py-3 ' + (isUser ? 'bg-emerald-600 text-white shadow-raised' : 'bg-zinc-900/70 border border-zinc-800 backdrop-blur');
    const inner = document.createElement('div');
    if (isUser && attachments && attachments.length){
      const tiles = renderAttachmentTiles(attachments);
      if (tiles) inner.appendChild(tiles);
    }
    inner.innerHTML += isUser ? `<div class=\"tracking-tight\">${(content || '').replace(/</g,'&lt;')}</div>` : `<div class=\"prose prose-invert max-w-none\">${renderMarkdownToHtml(content)}</div>`;
    if (!isUser){
      const actions = document.createElement('div'); actions.className = 'mt-2 flex items-center gap-2';
      const copyBtn = document.createElement('button'); copyBtn.type = 'button'; copyBtn.className = 'px-2 py-1 rounded bg-zinc-800 text-xs'; copyBtn.textContent = 'Copy answer';
      const regenBtn = document.createElement('button'); regenBtn.type = 'button'; regenBtn.className = 'px-2 py-1 rounded bg-zinc-800 text-xs'; regenBtn.textContent = 'Regenerate';
      copyBtn.addEventListener('click', async () => { try { await navigator.clipboard.writeText(inner.innerText.trim()); showToast('Copied','success'); } catch(e){ showToast('Copy failed','error'); } });
      regenBtn.addEventListener('click', () => { input.value = content || ''; autoResizeTextarea(input); input.focus(); });
      actions.appendChild(copyBtn); actions.appendChild(regenBtn); inner.appendChild(actions);
    }
    bubble.appendChild(inner);
    bubble.style.contentVisibility = 'auto';
    row.appendChild(bubble);
    chat.appendChild(row);
    if (!isUser) attachCopyHandlers(bubble);
    chat.scrollTop = chat.scrollHeight;
    return { row, bubble };
  }

  function createThinkingBubble(){
    const row = document.createElement('div');
    row.className = 'w-full flex items-start gap-3 justify-start';
    const b = document.createElement('div');
    b.className = 'msg rounded-2xl px-4 py-3 bg-zinc-900/70 border border-zinc-800 backdrop-blur';
    const stop = document.createElement('button');
    stop.type = 'button';
    stop.className = 'ml-2 text-xs px-2 py-0.5 rounded bg-zinc-800 text-zinc-200';
    stop.textContent = 'Stop';
    stop.addEventListener('click', () => { try { abortController?.abort(); } catch(e){} });
    b.innerHTML = '<div id="streamTarget" class="prose prose-invert max-w-none"></div>';
    b.appendChild(stop);
    row.appendChild(b);
    chat.appendChild(row);
    chat.scrollTop = chat.scrollHeight;
    return { row, target: b.querySelector('#streamTarget') };
  }

  // Always dark by default; preserved for potential future toggle
  function setTheme(on){ document.documentElement.classList.toggle('dark', on); try { localStorage.setItem('theme', on ? 'dark' : 'light'); } catch(e){} }
  if (localStorage.getItem('theme') !== 'light'){ setTheme(true); }
  themeToggle?.addEventListener('click', () => { const on = document.documentElement.classList.contains('dark'); setTheme(!on); });
  try { const savedModel = localStorage.getItem('model'); if (savedModel) modelSelect.value = savedModel; } catch(_) {}
  modelSelect?.addEventListener('change', () => { try { localStorage.setItem('model', modelSelect.value); } catch(_) {} });

  async function loadHistory(){
    const res = await fetch('/api/history');
    const data = await res.json();
    chat.innerHTML = '';
    const items = data.history || [];
    if (items.length === 0){
      chat.innerHTML = `
        <div class=\"w-full grid place-items-center pt-6\">
          <div class=\"text-center space-y-3\">
            <h2 class=\"text-3xl sm:text-4xl font-extrabold tracking-tight text-zinc-100\">What's on the agenda today?</h2>
            <p class=\"text-sm text-zinc-400\">Ask anything below.</p>
            <div class=\"flex flex-wrap gap-2 justify-center mt-3\">
              <button class=\"px-3 py-1.5 rounded-full bg-zinc-900/60 border border-zinc-800 text-zinc-200 text-xs suggestion\">Brainstorm startup ideas</button>
              <button class=\"px-3 py-1.5 rounded-full bg-zinc-900/60 border border-zinc-800 text-zinc-200 text-xs suggestion\">Summarize this article</button>
              <button class=\"px-3 py-1.5 rounded-full bg-zinc-900/60 border border-zinc-800 text-zinc-200 text-xs suggestion\">Explain a concept simply</button>
              <button class=\"px-3 py-1.5 rounded-full bg-zinc-900/60 border border-zinc-800 text-zinc-200 text-xs suggestion\">Draft an email</button>
              <button class=\"px-3 py-1.5 rounded-full bg-zinc-900/60 border border-zinc-800 text-zinc-200 text-xs suggestion\">Write a function</button>
            </div>
          </div>
        </div>`;
      (chat.querySelectorAll('.suggestion')||[]).forEach(b=>{ b.addEventListener('click', () => { input.value = b.textContent; autoResizeTextarea(input); input.focus(); }); });
    } else {
      items.forEach(m => bubble(m.role, m.content));
  try { const savedModel = localStorage.getItem('model'); if (savedModel) modelSelect.value = savedModel; } catch(_) {}
  
    }
    if (data.left !== undefined){
      if (data.left < 0) { limitP.textContent = 'Unlimited access active'; }
      else { limitP.textContent = `Free messages left today: ${data.left}`; }
    }
    attachCopyHandlers();
  }

  async   function createToolbar(){
    const toolbar = document.createElement('div');
    toolbar.className = 'flex items-center gap-2 px-3 py-2 text-[12px] text-zinc-400';
    const exportBtn = document.createElement('button'); exportBtn.type = 'button'; exportBtn.className = 'px-2 py-1 rounded border border-zinc-800 hover:bg-zinc-900'; exportBtn.textContent = 'Export';
    const clearBtn = document.createElement('button'); clearBtn.type = 'button'; clearBtn.className = 'px-2 py-1 rounded border border-rose-800 text-rose-300 hover:bg-rose-900/10'; clearBtn.textContent = 'Clear all';
    exportBtn.addEventListener('click', async () => {
      try { const res = await fetch('/api/export'); const data = await res.json(); const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }); const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = 'aichatpal_export.json'; a.click(); URL.revokeObjectURL(url); showToast('Exported','success'); } catch(e){ showToast('Export failed','error'); }
    });
    clearBtn.addEventListener('click', async () => {
      if (!confirm('Clear all conversations?')) return;
      try { const res = await fetch('/api/clear_all', { method: 'DELETE' }); const data = await res.json(); if (!res.ok || !data.ok) throw new Error('Failed'); showToast('Cleared','success'); await loadConversations(); await loadHistory(); } catch(e){ showToast('Clear failed','error'); }
    });
    toolbar.appendChild(exportBtn); toolbar.appendChild(clearBtn);
    return toolbar;
  }

  function loadConversations(){
    try {
      const res = await fetch('/api/conversations');
      const data = await res.json();
      const list = data.conversations || [];
      currentCid = data.current || currentCid;
      convoList.innerHTML = '';
      try { convoList.setAttribute('role','listbox'); } catch(_) {}
      convoList.appendChild(createToolbar());
      list.forEach(it => {
        const item = document.createElement('div');
        const active = it.id === currentCid;
        item.className = 'group rounded-lg px-2 py-2 flex items-center justify-between gap-2 cursor-pointer ' + (active ? 'bg-zinc-900/70 border border-zinc-800' : 'hover:bg-zinc-900/40');
        item.setAttribute('tabindex','0'); item.setAttribute('role','option'); item.setAttribute('aria-selected', String(active));
        const left = document.createElement('div');
        left.className = 'min-w-0';
        const title = document.createElement('div');
        title.className = 'truncate text-sm ' + (active ? 'text-zinc-100' : 'text-zinc-200');
        title.textContent = it.title || 'New chat';
        try { title.setAttribute('aria-label', `Conversation: ${title.textContent}`); } catch(_) {}
        const ts = document.createElement('div');
        ts.className = 'text-[10px] text-zinc-500';
        try { ts.textContent = new Date(it.updated_at).toLocaleString(); } catch(e) { ts.textContent = ''; }
        left.appendChild(title); left.appendChild(ts);
        const actions = document.createElement('div');
        actions.className = 'flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity';
        const renameBtn = document.createElement('button'); renameBtn.type = 'button'; renameBtn.title = 'Rename'; renameBtn.className = 'px-1.5 py-1 text-zinc-400 hover:text-zinc-100'; renameBtn.textContent = '✎';
        const delBtn = document.createElement('button'); delBtn.type = 'button'; delBtn.title = 'Delete'; delBtn.className = 'px-1.5 py-1 text-zinc-400 hover:text-rose-400'; delBtn.textContent = '🗑️';
        actions.appendChild(renameBtn); actions.appendChild(delBtn);
        item.appendChild(left); item.appendChild(actions);
        item.addEventListener('click', async (e) => {
          if (e.target === renameBtn || e.target === delBtn) return;
          if (it.id === currentCid) return;
          await selectConversation(it.id);
        });
        item.addEventListener('keydown', async (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (it.id !== currentCid) await selectConversation(it.id); }});
        renameBtn.addEventListener('click', (e) => { e.stopPropagation(); startInlineRename(item, it); });
        delBtn.addEventListener('click', async (e) => { e.stopPropagation(); await deleteConversation(it.id); });
        convoList.appendChild(item);
      });
    } catch(e){ /* ignore */ }
  }

  async function selectConversation(id){
    try{
      await fetch('/api/select_conversation', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ id }) });
      currentCid = id; await loadConversations(); await loadHistory();
      // Close sidebar on mobile
      sidebar.classList.add('-translate-x-full');
    }catch(e){ showToast('Failed to switch chat','error'); }
  }

  async function createNewChat(){
    try{
      const res = await fetch('/api/conversations', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({}) });
      const data = await res.json();
      if (data && data.id){ currentCid = data.id; await loadConversations(); await loadHistory(); }
    }catch(e){ showToast('Failed to create chat','error'); }
  }

  function startInlineRename(item, it){
    const left = item.firstChild; // min-w-0 wrapper
    const actions = item.lastChild;
    const prev = left.firstChild; // title div
    const input = document.createElement('input');
    input.type = 'text'; input.value = it.title || 'New chat';
    input.className = 'w-full bg-zinc-900/60 border border-zinc-800 rounded px-2 py-1 text-sm text-zinc-100';
    left.replaceChild(input, prev);
    input.focus(); input.select();
    function finish(ok){
      const newTitle = input.value.trim();
      if (ok && newTitle && newTitle !== it.title){ renameConversation(it.id, newTitle); }
      // Restore
      const title = document.createElement('div'); title.className = 'truncate text-sm ' + (it.id === currentCid ? 'text-zinc-100':'text-zinc-200'); title.textContent = it.title = newTitle || it.title || 'New chat';
      left.replaceChild(title, input);
    }
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter'){ finish(true); } else if (e.key === 'Escape'){ finish(false); }});
    input.addEventListener('blur', () => finish(true));
  }

  async function renameConversation(id, title){
    try{ await fetch(`/api/conversations/${id}`, { method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ title }) }); await loadConversations(); }
    catch(e){ showToast('Rename failed','error'); }
  }

  async function deleteConversation(id){
    if (!confirm('Delete this conversation?')) return;
    try{
      const res = await fetch(`/api/conversations/${id}`, { method:'DELETE' });
      const data = await res.json();
      currentCid = data.current || currentCid;
      await loadConversations(); await loadHistory();
    }catch(e){ showToast('Delete failed','error'); }
  }

  function bytesToSize(bytes){
    const units = ['B','KB','MB','GB'];
    let i = 0; let num = bytes;
    while (num >= 1024 && i < units.length-1){ num /= 1024; i++; }
    return `${num.toFixed(num >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
  }

  function renderAttachmentPreview(){
    if (!pendingAttachments.length){ attachmentPreview.classList.add('hidden'); attachmentPreview.innerHTML = ''; return; }
    attachmentPreview.classList.remove('hidden');
    const frag = document.createDocumentFragment();
    pendingAttachments.forEach((a, idx) => {
      const chip = document.createElement('div');
      chip.className = 'inline-flex items-center gap-2 px-2 py-1 rounded-lg border border-zinc-800 bg-zinc-900/70 text-xs text-zinc-200 animate-fade-in';
      if (a.kind === 'image'){
        const img = document.createElement('img'); img.src = a.base64 || ''; img.alt = a.name; img.className = 'h-7 w-7 rounded object-cover'; chip.appendChild(img);
      } else {
        const span = document.createElement('span'); span.textContent = '📎'; chip.appendChild(span);
      }
      const label = document.createElement('span'); label.className = 'truncate max-w-[10rem]'; label.textContent = `${a.name} • ${bytesToSize(a.size)}`; chip.appendChild(label);
      if (a.progress !== undefined && a.progress < 100){
        const bar = document.createElement('div'); bar.className = 'h-1 w-full bg-zinc-800 rounded';
        const fill = document.createElement('div'); fill.className = 'h-1 bg-emerald-500 rounded'; fill.style.width = `${a.progress}%`;
        const wrap = document.createElement('div'); wrap.className = 'w-24'; wrap.appendChild(bar); bar.appendChild(fill); chip.appendChild(wrap);
      }
      const x = document.createElement('button'); x.type = 'button'; x.className = 'ml-1 text-zinc-400 hover:text-zinc-200'; x.textContent = '✕';
      x.addEventListener('click', () => { pendingAttachments.splice(idx,1); renderAttachmentPreview(); });
      chip.appendChild(x);
      frag.appendChild(chip);
    });
    attachmentPreview.innerHTML = '';
    attachmentPreview.appendChild(frag);
  }

  function readFileWithProgress(file, onDone){
    const reader = new FileReader();
    readingCount++;
    const att = { name: file.name, mime: file.type || 'application/octet-stream', size: file.size, base64: '', kind: (file.type || '').startsWith('image/') ? 'image' : 'file', progress: 0 };
    pendingAttachments.push(att);
    reader.onprogress = (e) => { if (e.lengthComputable){ att.progress = Math.round((e.loaded / e.total) * 100); renderAttachmentPreview(); } };
    reader.onload = () => { att.base64 = String(reader.result || ''); att.progress = 100; renderAttachmentPreview(); if (onDone) onDone(); readingCount--; };
    reader.onerror = () => { showToast('File read failed','error'); const i = pendingAttachments.indexOf(att); if (i>=0) pendingAttachments.splice(i,1); renderAttachmentPreview(); readingCount--; };
    reader.readAsDataURL(file);
  }

  function addFiles(files){
    const arr = Array.from(files || []);
    if (!arr.length) return;
    if (pendingAttachments.length + arr.length > MAX_ATTACHMENTS){ showToast('Too many attachments','error'); return; }
    const currentTotal = pendingAttachments.reduce((s,a)=>s+a.size,0);
    const newTotal = currentTotal + arr.reduce((s,f)=>s+f.size,0);
    if (newTotal > MAX_TOTAL_SIZE){ showToast('Attachments too large','error'); return; }
    arr.forEach(file => {
      if (file.size > MAX_FILE_SIZE){ showToast(`${file.name} is too large`,'error'); return; }
      readFileWithProgress(file);
    });
  }

  function toggleAttachMenu(show){
    const willShow = show === undefined ? attachMenu.classList.contains('hidden') : !!show;
    attachMenu.classList.toggle('hidden', !willShow);
    attachBtn.setAttribute('aria-expanded', String(willShow));
  }

  async function sendMessage(){
    const text = input.value.trim();
    if (readingCount > 0){ showToast('Still processing files…','error'); return; }
    if (!text && !pendingAttachments.length) return;
    const attachmentsForBubble = pendingAttachments.map(a => ({ kind: a.kind, base64: a.base64, name: a.name, size: a.size }));
    input.value = '';
    autoResizeTextarea(input);
    bubble('user', text || (pendingAttachments.length ? '(Sent attachments)' : ''), attachmentsForBubble);
    const payloadAttachments = pendingAttachments.map(a => ({ name: a.name, mime: a.mime, size: a.size, data: (a.base64.split(',')[1] || '') }));
    pendingAttachments = []; renderAttachmentPreview(); toggleAttachMenu(false);
    sendBtn.disabled = true;
    const prev = sendBtn.innerHTML;
    sendBtn.innerHTML = '<span class="opacity-80">…</span>';
    const thinking = createThinkingBubble();
    chat.scrollTop = chat.scrollHeight;
    try{
      abortController = new AbortController();
      const res = await fetch('/api/chat_stream', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({message: text, attachments: payloadAttachments, model: (modelSelect?.value || undefined)}), signal: abortController.signal });
      if (!res.ok){
        thinking.row.remove();
        let errText = 'Network error';
        try { const e = await res.json(); errText = e.error || errText; } catch(_) {}
        bubble('assistant', `Error: ${errText}`);
        showToast(errText, 'error');
        if (res.status === 429 || /Daily free limit/i.test(errText)){
          try { input.disabled = true; input.placeholder = 'Daily free limit reached. Unlock unlimited to continue'; sendBtn.disabled = true; } catch(_) {}
          try { document.getElementById('unlockDialog').showModal(); } catch(_) {}
        }
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let done = false;
      let acc = '';
      while (!done){
        const { value, done: doneRead } = await reader.read();
        if (value){ acc += decoder.decode(value, { stream: true }); thinking.target.innerHTML = renderMarkdownToHtml(acc); chat.scrollTop = chat.scrollHeight; }
        done = doneRead;
      }
      attachCopyHandlers();
      // update left
              try { const left = res.headers.get('x-usage-left'); if (left !== null){ const n = parseInt(left, 10); document.getElementById('limitText').textContent = (n < 0) ? 'Unlimited access active' : `Free messages left today: ${n}`; } } catch(_) {}
    }catch(e){ thinking.row.remove(); if (e.name !== 'AbortError'){ bubble('assistant', 'Network error.'); showToast('Network error','error'); } }
    finally { sendBtn.disabled = false; sendBtn.innerHTML = prev || 'Send'; abortController = null; input.focus(); }
  }

  document.getElementById('composer').addEventListener('submit', (e) => { e.preventDefault(); sendMessage(); });
  input.addEventListener('keydown', (e) => { if ((e.key === 'Enter' && !e.shiftKey) || (e.key === 'Enter' && (e.metaKey || e.ctrlKey))){ e.preventDefault(); sendMessage(); }});
  input.addEventListener('input', () => autoResizeTextarea(input));
  input.addEventListener('focus', () => { setTimeout(() => { chat.scrollTop = chat.scrollHeight; }, 50); });

  // Drag & drop attachments
  ;['dragenter','dragover'].forEach(eventName => composerBox.addEventListener(eventName, (e) => { e.preventDefault(); e.stopPropagation(); composerBox.classList.add('ring-2','ring-emerald-600'); }));
  ;['dragleave','drop'].forEach(eventName => composerBox.addEventListener(eventName, (e) => { e.preventDefault(); e.stopPropagation(); composerBox.classList.remove('ring-2','ring-emerald-600'); }));
  composerBox.addEventListener('keydown', (e) => { if (e.key === 'Escape') { toggleAttachMenu(false); } });
  composerBox.addEventListener('drop', (e) => { const dt = e.dataTransfer; if (dt && dt.files) addFiles(dt.files); });

  // Sidebar toggle
  sidebarToggle?.addEventListener('click', () => { const isHidden = sidebar.classList.contains('-translate-x-full'); sidebar.classList.toggle('-translate-x-full', !isHidden); sidebar.classList.remove('hidden'); sidebarToggle.setAttribute('aria-expanded', String(!isHidden)); });

  // Attachments menu and actions
  attachBtn?.addEventListener('click', (e) => { e.stopPropagation(); toggleAttachMenu(); });
  document.addEventListener('click', (e) => { if (!attachMenu.contains(e.target) && e.target !== attachBtn) toggleAttachMenu(false); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape'){ toggleAttachMenu(false); }});
  document.getElementById('actionAddPhotos')?.addEventListener('click', () => photosInput.click());
  document.getElementById('actionTakePhoto')?.addEventListener('click', () => cameraInput.click());
  document.getElementById('actionAddFiles')?.addEventListener('click', () => filesInput.click());
  photosInput.addEventListener('change', (e) => { addFiles(e.target.files); photosInput.value = ''; toggleAttachMenu(false); });
  cameraInput.addEventListener('change', (e) => { addFiles(e.target.files); cameraInput.value = ''; toggleAttachMenu(false); });
  filesInput.addEventListener('change', (e) => { addFiles(e.target.files); filesInput.value = ''; toggleAttachMenu(false); });

  newChatBtn?.addEventListener('click', createNewChat);

  // Unlock modal logic
  const unlockBtn = document.getElementById('unlockBtn');
  const unlockDialog = document.getElementById('unlockDialog');
  const unlockKey = document.getElementById('unlockKey');
  const unlockSubmit = document.getElementById('unlockSubmit');
  unlockBtn?.addEventListener('click', () => { try { unlockDialog.showModal(); unlockKey.focus(); } catch(_) {} });
  unlockSubmit?.addEventListener('click', async () => {
    const key = (unlockKey.value || '').trim();
    if (!key) { showToast('Enter a key','error'); return; }
    try {
      const res = await fetch('/api/key', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ key }) });
      const data = await res.json();
      if (!res.ok || !data.ok) { showToast(data.error || 'Invalid key', 'error'); return; }
      showToast('Unlimited unlocked','success');
      limitP.textContent = 'Unlimited access active';
      try { unlockDialog.close(); } catch(_) {}
    } catch(e) { showToast('Activation failed','error'); }
  });

  // Optional: register service worker
  if ('serviceWorker' in navigator) {
    try { navigator.serviceWorker.register('/sw.js'); } catch(_) {}
  }

  loadConversations();
  loadHistory();
  });
  </script>
</body>
</html>
"""


def _create_flask_app() -> Flask:
    app = Flask(__name__)
    # Secret key for cookies
    app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev_secret_change_me")

    # Compression
    try:
        from flask_compress import Compress  # type: ignore
        Compress(app)
    except Exception:
        pass

    # Security headers
    @app.after_request
    def add_security_headers(resp: Response) -> Response:
        try:
            resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
            resp.headers.setdefault("X-Content-Type-Options", "nosniff")
            resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
            resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
            # Minimal CSP allowing inline styles from Tailwind CDN artifacts safely omitted; keep relaxed for now
            resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        except Exception:
            pass
        return resp

    def _get_or_create_user_id() -> Tuple[int, Optional[Response]]:
        uid_cookie = request.cookies.get("uid")
        response: Optional[Response] = None
        try:
            if uid_cookie is None:
                import secrets as _secrets
                uid_val = _secrets.randbits(63)
                response = make_response()
                response.set_cookie(
                    "uid",
                    str(uid_val),
                    max_age=60*60*24*365,
                    httponly=True,
                    samesite="Lax",
                )
                return int(uid_val), response
            else:
                return int(uid_cookie), None
        except Exception:
            # Fallback generate if cookie corrupted
            import secrets as _secrets
            uid_val = _secrets.randbits(63)
            response = make_response()
            response.set_cookie(
                "uid",
                str(uid_val),
                max_age=60*60*24*365,
                httponly=True,
                samesite="Lax",
            )
            return int(uid_val), response

    def _is_admin_request() -> bool:
        try:
            return request.cookies.get("admin") == "1"
        except Exception:
            return False

    def _generate_conversation_id() -> str:
        return secrets.token_hex(8)

    def _ensure_current_conversation(user_id: int) -> Tuple[str, Optional[Response]]:
        cid_cookie = request.cookies.get("cid")
        if cid_cookie:
            return cid_cookie, None
        # create a new conversation and set cookie
        cid = _generate_conversation_id()
        _, _, _, col_convos = _get_db_collections()
        now = datetime.now(timezone.utc)
        try:
            col_convos.insert_one({
                "user_id": user_id,
                "id": cid,
                "title": "New chat",
                "created_at": now,
                "updated_at": now,
            })
        except Exception as e:
            _log_admin(f"DB error creating conversation: {e}")
        _save_conversation_history(user_id, [], cid)
        response = make_response()
        response.set_cookie(
            "cid",
            cid,
            max_age=60*60*24*365,
            httponly=True,
            samesite="Lax",
        )
        return cid, response

    def _free_left(user_id: int) -> int:
        if _is_admin_request() or _has_active_key(user_id):
            return -1
        used = _get_message_count(user_id)
        left = max(0, FREE_DAILY_LIMIT - used)
        return left

    def _update_conversation_timestamp(user_id: int, cid: str) -> None:
        try:
            _, _, _, col_convos = _get_db_collections()
            col_convos.update_one({"user_id": user_id, "id": cid}, {"$set": {"updated_at": datetime.now(timezone.utc)}})
        except Exception as e:
            _log_admin(f"DB error updating conversation timestamp: {e}")

    @app.get("/")
    def index() -> Response:
        user_id, resp = _get_or_create_user_id()
        cid, resp2 = _ensure_current_conversation(user_id)
        final_resp: Optional[Response] = resp or resp2
        if final_resp is None:
            return Response(HTML_INDEX, mimetype="text/html")
        final_resp.set_data(HTML_INDEX)
        final_resp.mimetype = "text/html"
        return final_resp

    @app.get("/manifest.json")
    def manifest() -> Response:
        manifest = {
            "name": "AIChatPal",
            "short_name": "AIChatPal",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#0b0b10",
            "theme_color": "#0c0c0f",
            "icons": [
                {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}
            ]
        }
        return Response(json.dumps(manifest), mimetype="application/manifest+json")

    @app.get("/sw.js")
    def service_worker() -> Response:
        sw = (
            "self.addEventListener('install', e => { self.skipWaiting(); });\n"
            "self.addEventListener('activate', e => { self.clients.claim(); });\n"
            "self.addEventListener('fetch', e => { /* passthrough */ });\n"
        )
        return Response(sw, mimetype="application/javascript")

    @app.get("/icon.svg")
    def icon_svg() -> Response:
        svg = (
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
            "<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop offset='0%' stop-color='#34d399'/><stop offset='100%' stop-color='#818cf8'/></linearGradient></defs>"
            "<rect x='4' y='4' width='56' height='56' rx='14' fill='url(#g)'/>"
            "<path d='M18 34c6 6 22 6 28 0' stroke='white' stroke-width='4' fill='none' stroke-linecap='round'/>"
            "<circle cx='24' cy='26' r='3' fill='white'/><circle cx='40' cy='26' r='3' fill='white'/>"
            "</svg>"
        )
        return Response(svg, mimetype="image/svg+xml")

    @app.get("/api/history")
    def api_history():
        user_id, resp = _get_or_create_user_id()
        cid, resp2 = _ensure_current_conversation(user_id)
        history = load_conversation_history(user_id, cid)
        payload = {
            "history": [{"role": m.get("role"), "content": m.get("content")} for m in history],
            "left": _free_left(user_id),
        }
        combined_resp = resp or resp2
        if combined_resp is None:
            return jsonify(payload)
        combined_resp.set_data(json.dumps(payload))
        combined_resp.mimetype = "application/json"
        return combined_resp

    @app.get("/api/conversations")
    def api_conversations():
        user_id, resp = _get_or_create_user_id()
        cid, resp2 = _ensure_current_conversation(user_id)
        try:
            _, _, _, col_convos = _get_db_collections()
            items = list(col_convos.find({"user_id": user_id}).sort("updated_at", -1))
            convos = [{"id": it.get("id"), "title": it.get("title", "New chat"), "updated_at": (it.get("updated_at") or datetime.now(timezone.utc)).isoformat()} for it in items]
        except Exception as e:
            _log_admin(f"DB error listing conversations: {e}")
            convos = []
        payload = {"conversations": convos, "current": cid, "is_admin": request.cookies.get("admin") == "1"}
        combined_resp = resp or resp2
        if combined_resp is None:
            return jsonify(payload)
        combined_resp.set_data(json.dumps(payload))
        combined_resp.mimetype = "application/json"
        return combined_resp

    @app.get("/api/export")
    def api_export():
        user_id, _ = _get_or_create_user_id()
        try:
            _, col_history, _, _ = _get_db_collections()
            docs = list(col_history.find({"user_id": user_id}))
            for d in docs:
                d.pop("_id", None)
            return jsonify({"ok": True, "data": docs})
        except Exception as e:
            _log_admin(f"DB error export: {e}")
            return jsonify({"ok": False, "error": "DB error"}), 500

    @app.delete("/api/clear_all")
    def api_clear_all():
        user_id, _ = _get_or_create_user_id()
        try:
            col_users, col_history, _, col_convos = _get_db_collections()
            col_history.delete_many({"user_id": user_id})
            col_convos.delete_many({"user_id": user_id})
            col_users.update_one({"user_id": user_id}, {"$set": {"message_count": 0}}, upsert=True)
            return jsonify({"ok": True})
        except Exception as e:
            _log_admin(f"DB error clear all: {e}")
            return jsonify({"ok": False, "error": "DB error"}), 500

    @app.post("/api/conversations")
    def api_conversations_create():
        user_id, _ = _get_or_create_user_id()
        data = request.get_json(silent=True) or {}
        title = str(data.get("title") or "New chat").strip() or "New chat"
        cid = secrets.token_hex(8)
        _, _, _, col_convos = _get_db_collections()
        now = datetime.now(timezone.utc)
        try:
            col_convos.insert_one({
                "user_id": user_id,
                "id": cid,
                "title": title,
                "created_at": now,
                "updated_at": now,
            })
            _save_conversation_history(user_id, [], cid)
        except Exception as e:
            _log_admin(f"DB error creating conversation: {e}")
        resp = jsonify({"ok": True, "id": cid})
        resp.set_cookie("cid", cid, max_age=60*60*24*365, httponly=True, samesite="Lax")
        return resp

    @app.post("/api/select_conversation")
    def api_select_conversation():
        user_id, _ = _get_or_create_user_id()
        data = request.get_json(silent=True) or {}
        cid = str(data.get("id") or "").strip()
        if not cid:
            return jsonify({"ok": False, "error": "Missing id"}), 400
        try:
            _, _, _, col_convos = _get_db_collections()
            exists = col_convos.find_one({"user_id": user_id, "id": cid})
            if not exists:
                return jsonify({"ok": False, "error": "Not found"}), 404
        except Exception:
            pass
        resp = jsonify({"ok": True})
        resp.set_cookie("cid", cid, max_age=60*60*24*365, httponly=True, samesite="Lax")
        return resp

    @app.put("/api/conversations/<cid>")
    def api_conversations_rename(cid: str):
        user_id, _ = _get_or_create_user_id()
        data = request.get_json(silent=True) or {}
        title = str(data.get("title") or "").strip()
        if not title:
            return jsonify({"ok": False, "error": "Missing title"}), 400
        try:
            _, _, _, col_convos = _get_db_collections()
            col_convos.update_one({"user_id": user_id, "id": cid}, {"$set": {"title": title, "updated_at": datetime.now(timezone.utc)}})
            return jsonify({"ok": True})
        except Exception as e:
            _log_admin(f"DB error renaming conversation: {e}")
            return jsonify({"ok": False, "error": "DB error"}), 500

    @app.delete("/api/conversations/<cid>")
    def api_conversations_delete(cid: str):
        user_id, _ = _get_or_create_user_id()
        try:
            col_users, col_history, _, col_convos = _get_db_collections()
            col_convos.delete_one({"user_id": user_id, "id": cid})
            col_history.delete_one({"user_id": user_id, "conversation_id": cid})
        except Exception as e:
            _log_admin(f"DB error deleting conversation: {e}")
            return jsonify({"ok": False, "error": "DB error"}), 500
        # Select another conversation if any
        try:
            items = list(col_convos.find({"user_id": user_id}).sort("updated_at", -1))
            new_cid = items[0]["id"] if items else secrets.token_hex(8)
            if not items:
                # create an empty one
                now = datetime.now(timezone.utc)
                col_convos.insert_one({"user_id": user_id, "id": new_cid, "title": "New chat", "created_at": now, "updated_at": now})
                _save_conversation_history(user_id, [], new_cid)
        except Exception:
            new_cid = secrets.token_hex(8)
        resp = jsonify({"ok": True, "current": new_cid})
        resp.set_cookie("cid", new_cid, max_age=60*60*24*365, httponly=True, samesite="Lax")
        return resp

    @app.post("/api/newchat")
    def api_newchat():
        user_id, _ = _get_or_create_user_id()
        # Create a new conversation and set as current
        cid = secrets.token_hex(8)
        _, _, _, col_convos = _get_db_collections()
        now = datetime.now(timezone.utc)
        try:
            col_convos.insert_one({"user_id": user_id, "id": cid, "title": "New chat", "created_at": now, "updated_at": now})
            _save_conversation_history(user_id, [], cid)
        except Exception as e:
            _log_admin(f"DB error creating new chat: {e}")
        resp = jsonify({"ok": True, "id": cid})
        resp.set_cookie("cid", cid, max_age=60*60*24*365, httponly=True, samesite="Lax")
        return resp

    @app.post("/api/key")
    def api_key():
        user_id, _ = _get_or_create_user_id()
        try:
            from keys import DEMO_KEYS
        except Exception:
            DEMO_KEYS = {}
        data = request.get_json(silent=True) or {}
        provided = str(data.get("key", "")).strip()
        if not provided:
            return jsonify({"ok": False, "error": "Missing key"}), 400
        valid_until = DEMO_KEYS.get(provided)
        if not valid_until:
            return jsonify({"ok": False, "error": "Invalid key"}), 400
        _set_active_key(user_id, provided, valid_until)
        return jsonify({"ok": True, "valid_until": valid_until.isoformat()})

    @app.post("/api/login")
    def api_login():
        data = request.get_json(silent=True) or {}
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "").strip()
        if username == "admin123" and password == "admin123":
            resp = jsonify({"ok": True})
            resp.set_cookie("admin", "1", max_age=60*60*24*7, httponly=True, samesite="Lax")
            return resp
        return jsonify({"ok": False, "error": "Invalid credentials"}), 401

    @app.post("/api/logout")
    def api_logout():
        resp = jsonify({"ok": True})
        resp.delete_cookie("admin")
        return resp

    @app.post("/api/chat_stream")
    def api_chat_stream():
        user_id, _ = _get_or_create_user_id()
        cid, _ = _ensure_current_conversation(user_id)
        data = request.get_json(silent=True) or {}
        text = str(data.get("message", "")).strip()
        model_override = str(data.get("model") or "").strip() or None
        if not text and not data.get("attachments"):
            return jsonify({"error": "Empty message"}), 400

        # Rate limit for free users
        if not _is_admin_request() and not _has_active_key(user_id):
            current = _get_message_count(user_id)
            if current >= FREE_DAILY_LIMIT:
                return jsonify({"error": "Daily free limit reached (3/day). Use a key to unlock unlimited.", "left": 0}), 429
            _increment_message_count(user_id)

        history = load_conversation_history(user_id, cid)
        now = datetime.now(timezone.utc)

        # Parse attachments
        raw_attachments = data.get("attachments") or []
        attachment_parts: List[Dict[str, Any]] = []
        attachment_names: List[str] = []
        try:
            if isinstance(raw_attachments, list):
                if len(raw_attachments) > 5:
                    return jsonify({"error": "Too many attachments (max 5)", "left": _free_left(user_id)}), 400
                total_size = 0
                for a in raw_attachments:
                    if not isinstance(a, dict):
                        continue
                    name = str(a.get("name") or "attachment")
                    mime = str(a.get("mime") or "application/octet-stream")
                    data_b64 = str(a.get("data") or "")
                    if not data_b64:
                        continue
                    size_bytes = _estimate_base64_bytes(data_b64)
                    if size_bytes > 8 * 1024 * 1024:
                        return jsonify({"error": f"{name} is too large (max 8MB)", "left": _free_left(user_id)}), 400
                    total_size += size_bytes
                    attachment_parts.append({"inline_data": {"mime_type": mime, "data": data_b64}})
                    attachment_names.append(name)
                if total_size > 12 * 1024 * 1024:
                    return jsonify({"error": "Attachments too large (max 12MB total)", "left": _free_left(user_id)}), 400
        except Exception:
            pass

        user_content = text
        if attachment_names:
            preview = ", ".join(attachment_names[:3]) + ("…" if len(attachment_names) > 3 else "")
            user_content = (text + ("\n\n(Attached: " + preview + ")" if text else f"(Attached: {preview})")).strip()
        history.append({"role": "user", "content": user_content, "timestamp": now})

        # Build contents and stream
        contents = _build_gemini_contents(history, latest_attachments=attachment_parts)

        client = _get_gemini_client()
        if client is None:
            return jsonify({"error": "Gemini is not configured. Please set GEMINI_API_KEY."}), 503

        def generate():
            text_acc = []
            model = model_override or os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
            system_prompt = os.getenv("GEMINI_SYSTEM_PROMPT")
            cfg = None
            try:
                from google.genai import types as genai_types  # type: ignore
                thinking_cfg = None
                try:
                    thinking_cfg = genai_types.ThinkingConfig(budget_tokens=-1)
                except Exception:
                    thinking_cfg = None
                cfg = genai_types.GenerateContentConfig(
                    system_instruction=system_prompt if system_prompt else None,
                    thinking_config=thinking_cfg,
                )
            except Exception:
                cfg = {"system_instruction": system_prompt} if system_prompt else None

            try:
                stream = client.models.generate_content_stream(model=model, contents=contents, config=cfg)
                for chunk in stream:
                    try:
                        text_piece = getattr(chunk, "text", None)
                        if text_piece:
                            s = str(text_piece)
                            text_acc.append(s)
                            yield s
                    except Exception:
                        pass
                final_text = "".join(text_acc).strip() or "(No response)"
            except Exception as e:
                final_text = ""
                err = f"Gemini error: {e}"
                _log_admin(err)
                yield f"Error: {err}"

            # Save history if we have content
            if final_text:
                history.append({"role": "assistant", "content": final_text, "timestamp": datetime.now(timezone.utc)})
                _save_conversation_history(user_id, history, cid)
                _update_conversation_timestamp(user_id, cid)
                try:
                    _, _, _, col_convos = _get_db_collections()
                    doc = col_convos.find_one({"user_id": user_id, "id": cid})
                    if doc and (not doc.get("title") or doc.get("title") == "New chat"):
                        preview = (text or user_content).strip().split("\n")[0][:50]
                        col_convos.update_one({"user_id": user_id, "id": cid}, {"$set": {"title": preview or "New chat"}})
                except Exception:
                    pass

        gen = generate()
        if isinstance(gen, tuple):
            # Early error response path
            return gen
        resp = Response(stream_with_context(gen), mimetype="text/plain")
        # Return usage left in header
        try:
            left = _free_left(user_id)
            resp.headers["x-usage-left"] = str(left)
        except Exception:
            pass
        return resp

    # Optional admin logs endpoint: now requires admin
    @app.get("/adminJackLogs")
    def admin_logs():
        if not _is_admin_request():
            return Response("Forbidden", status=403, mimetype="text/plain")
        try:
            col_users, col_history, col_keys, col_convos = _get_db_collections()
            users_count = col_users.count_documents({})
            history_count = col_history.count_documents({})
            keys_count = col_keys.count_documents({})
            conv_count = col_convos.count_documents({})
        except Exception:
            users_count = history_count = keys_count = conv_count = -1
        tail = "\n".join(_ADMIN_LOGS[-30:]) if _ADMIN_LOGS else "(no logs)"
        msg = (
            f"DB: users={users_count}, history={history_count}, keys_in_use={keys_count}, conversations={conv_count}\n\n"
            f"Recent logs:\n{tail}"
        )
        return Response(msg, mimetype="text/plain")

    # Daily reset job: naive timer loop if desired (skipped; relies on external cron in prod)

    return app



def main() -> None:
    app = _create_flask_app()
    # Suppress werkzeug noisy logs in production
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.WARNING)

    port = int(os.getenv("PORT", "8080"))
    _log_admin("Starting Flask web server…")
    _start_daily_reset_thread_if_enabled()
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()