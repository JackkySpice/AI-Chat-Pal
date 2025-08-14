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
logger = logging.getLogger("AIChatPalWeb")

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


# -------------------------- Web App --------------------------
from flask import Flask, request, jsonify, make_response, Response
import secrets


HTML_INDEX = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content, maximum-scale=1">
  <title>AIChatPal · Mobile</title>
  <meta name="description" content="AIChatPal — Clean, fast, mobile-first AI chat powered by Gemini.">
  <meta name="theme-color" content="#0b1020" media="(prefers-color-scheme: dark)">
  <meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
  <meta name="format-detection" content="telephone=no">
  <meta name="HandheldFriendly" content="true">
  <meta name="google" content="notranslate">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <script src="https://cdn.tailwindcss.com?plugins=typography,forms"></script>
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        fontFamily: {
          sans: ['Inter','ui-sans-serif','system-ui','Segoe UI','Roboto','Helvetica','Arial']
        },
        extend: {
          colors: {
            brand: { 50:'#eef2ff',100:'#e0e7ff',200:'#c7d2fe',300:'#a5b4fc',400:'#818cf8',500:'#6366f1',600:'#4f46e5',700:'#4338ca',800:'#3730a3',900:'#312e81' },
            ink: '#0b1020'
          },
          boxShadow: {
            sheet: '0 -8px 30px -10px rgba(2,6,23,.35)',
            elevated: '0 10px 30px -12px rgba(2,6,23,.35)'
          }
        }
      }
    };
  </script>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/themes/prism-tomorrow.min.css"/>
  <style>
    :root { color-scheme: light dark; -webkit-text-size-adjust: 100%; }
    * { -webkit-tap-highlight-color: transparent; }
    html, body { height: 100%; }
    body { overscroll-behavior-y: contain; }
    .glass { backdrop-filter: saturate(140%) blur(10px); background: rgba(255,255,255,0.55); }
    .dark .glass { background: rgba(17,24,39,0.55); }
    .safe-bottom { padding-bottom: max(env(safe-area-inset-bottom), 0px); }
    .composer-safe-bottom { bottom: calc(env(safe-area-inset-bottom) + 0px); }
    .scroll-smooth { scroll-behavior: smooth; }
    .msg { max-width: 72ch; }
    .skeleton { position: relative; overflow: hidden; }
    .skeleton::after { content: ""; position: absolute; inset: 0; background: linear-gradient(110deg, transparent 0%, rgba(255,255,255,.08) 40%, rgba(255,255,255,.18) 60%, transparent 100%); animation: shimmer 1.1s linear infinite; }
    @keyframes shimmer { 0% { transform: translateX(-100%);} 100% { transform: translateX(100%);} }
    .dark .skeleton::after { background: linear-gradient(110deg, transparent 0%, rgba(255,255,255,.06) 40%, rgba(255,255,255,.12) 60%, transparent 100%); }
    pre { position: relative; }
    pre code { white-space: pre-wrap; word-break: break-word; }
    .copy-btn { position: absolute; top: .5rem; right: .5rem; }
    @media (prefers-reduced-motion: reduce) {
      * { animation-duration: 0.01ms !important; animation-iteration-count: 1 !important; transition-duration: 0.01ms !important; }
      .glass { backdrop-filter: none; }
    }
  </style>
</head>
<body class="font-sans bg-white dark:bg-ink text-slate-900 dark:text-slate-100">
  <div class="min-h-[100svh] flex flex-col">
    <header class="sticky top-0 z-20 glass border-b border-slate-200/70 dark:border-slate-800/70">
      <div class="px-4 py-3 flex items-center justify-between">
        <div class="flex items-center gap-3">
          <button id="openSheet" class="md:hidden px-3 py-2 rounded-lg bg-slate-900/5 dark:bg-white/10" aria-label="Open menu">
            <svg xmlns="http://www.w3.org/2000/svg" class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="18" x2="20" y2="18"/></svg>
          </button>
          <div class="h-8 w-8 rounded-xl bg-gradient-to-br from-brand-500 to-purple-600 shadow-elevated"></div>
          <div>
            <div class="font-extrabold tracking-tight">AIChatPal</div>
            <div class="text-[11px] text-slate-500 dark:text-slate-400">Gemini powered</div>
          </div>
        </div>
        <div class="flex items-center gap-2">
          <button id="newChatTop" class="hidden md:inline-flex px-3 py-2 rounded-lg bg-gradient-to-r from-brand-600 to-purple-600 text-white shadow-elevated hover:brightness-110">New chat</button>
          <button id="toggleTheme" class="px-3 py-2 rounded-lg bg-slate-900/5 dark:bg-white/10" aria-label="Toggle theme"><span id="themeIcon">🌙</span></button>
        </div>
      </div>
    </header>

    <main class="flex-1 relative">
      <div id="chat" class="scroll-smooth overflow-y-auto px-3 pt-3 pb-28 md:pb-4 max-w-3xl mx-auto w-full" role="log" aria-live="polite" aria-relevant="additions"></div>

      <button id="scrollBottom" class="hidden fixed right-4 bottom-28 md:bottom-24 z-30 h-10 w-10 rounded-full text-white bg-gradient-to-r from-brand-600 to-purple-600 shadow-elevated" aria-label="Scroll to bottom">
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="w-5 h-5 m-auto"><path d="M6 9l6 6 6-6"/></svg>
      </button>

      <button id="fabNewChat" class="md:hidden fixed right-4 bottom-[92px] z-30 h-12 w-12 rounded-full text-white bg-gradient-to-r from-brand-600 to-purple-600 shadow-elevated" aria-label="New chat">
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="w-6 h-6 m-auto"><path d="M12 5v14M5 12h14"/></svg>
      </button>

      <div class="fixed inset-x-0 composer-safe-bottom z-40">
        <div class="mx-auto max-w-3xl px-3 pb-3">
          <form id="composer" class="rounded-2xl glass border border-slate-200/70 dark:border-slate-800/70 shadow-elevated p-3">
            <div class="flex items-end gap-2">
              <div class="flex-1">
                <textarea id="input" rows="1" placeholder="Type your message" enterkeyhint="send" autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false" inputmode="text" class="w-full resize-none rounded-xl border border-slate-200 dark:border-slate-700 bg-white/80 dark:bg-slate-900/60 p-3 focus:outline-none focus:ring-2 focus:ring-brand-500 placeholder:text-slate-400" ></textarea>
                <div class="mt-2 flex items-center justify-between text-xs text-slate-500">
                  <span class="hidden sm:inline">Enter to send, Shift+Enter for new line</span>
                  <p id="limit" class="px-2 py-0.5 rounded-full bg-slate-100 dark:bg-slate-800 border border-slate-200/70 dark:border-slate-700/70 text-slate-600 dark:text-slate-300"></p>
                </div>
              </div>
              <button id="send" type="submit" class="h-12 px-5 rounded-xl bg-gradient-to-r from-brand-600 to-purple-600 text-white font-semibold hover:brightness-110 disabled:opacity-50 shadow-elevated flex items-center gap-2">
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="w-5 h-5"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/></svg>
                <span>Send</span>
              </button>
            </div>
          </form>
        </div>
      </div>
    </main>

    <footer class="hidden md:block text-center text-xs text-slate-500 py-4">AIChatPal — Clean, fast, mobile-first</footer>
  </div>

  <div id="toasts" class="fixed bottom-[88px] left-1/2 -translate-x-1/2 space-y-2 z-50"></div>

  <div id="sheetBackdrop" class="fixed inset-0 z-40 bg-black/40 hidden"></div>
  <aside id="sheet" class="fixed inset-x-0 bottom-0 z-50 hidden rounded-t-2xl glass border-t border-slate-200/70 dark:border-slate-800/70 shadow-sheet p-4 max-h-[80svh] overflow-y-auto">
    <div class="flex items-center justify-between pb-2">
      <div class="text-sm font-semibold text-slate-600 dark:text-slate-300">Conversations</div>
      <div class="flex items-center gap-2">
        <button id="newChatSheet" class="px-3 py-1.5 rounded-lg bg-gradient-to-r from-brand-600 to-purple-600 text-white shadow">New</button>
        <button id="closeSheet" class="px-2 py-1 rounded-lg border border-slate-200/70 dark:border-slate-700/70">Close</button>
      </div>
    </div>
    <div class="relative mb-3">
      <input id="convSearch" placeholder="Search chats" class="w-full px-10 py-2 rounded-xl bg-white/80 dark:bg-slate-900/60 border border-slate-200 dark:border-slate-700 focus:outline-none focus:ring-2 focus:ring-brand-500" />
      <div class="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400">🔎</div>
    </div>
    <nav id="convoList" class="space-y-1"></nav>
    <div class="mt-4 grid grid-cols-2 gap-2 text-sm">
      <button id="keyBtn" class="px-3 py-2 rounded-xl border border-slate-200/70 dark:border-slate-700/70">Activate key</button>
      <button id="loginBtn" class="px-3 py-2 rounded-xl border border-slate-200/70 dark:border-slate-700/70">Admin login</button>
      <button id="logoutBtn" class="hidden px-3 py-2 rounded-xl border border-slate-200/70 dark:border-slate-700/70">Logout</button>
    </div>
  </aside>

  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.7/dist/purify.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/prism.min.js"></script>
  <script>
  const chat = document.getElementById('chat');
  const input = document.getElementById('input');
  const sendBtn = document.getElementById('send');
  const limitP = document.getElementById('limit');
  const scrollBottomBtn = document.getElementById('scrollBottom');
  const toggleTheme = document.getElementById('toggleTheme');
  const openSheetBtn = document.getElementById('openSheet');
  const closeSheetBtn = document.getElementById('closeSheet');
  const sheet = document.getElementById('sheet');
  const sheetBackdrop = document.getElementById('sheetBackdrop');
  const convoList = document.getElementById('convoList');
  const convSearch = document.getElementById('convSearch');
  const keyBtn = document.getElementById('keyBtn');
  const loginBtn = document.getElementById('loginBtn');
  const logoutBtn = document.getElementById('logoutBtn');
  const fabNewChat = document.getElementById('fabNewChat');
  const newChatTop = document.getElementById('newChatTop');
  const newChatSheet = document.getElementById('newChatSheet');
  const composer = document.getElementById('composer');
  const themeIcon = document.getElementById('themeIcon');

  let state = { conversations: [], current: null, is_admin: false };

  function showToast(message, variant = 'default', timeout = 2500) {
    const host = document.getElementById('toasts');
    const node = document.createElement('div');
    const colors = variant === 'success' ? 'from-emerald-600 to-green-600' : variant === 'error' ? 'from-rose-600 to-pink-600' : 'from-slate-700 to-slate-900';
    node.className = `text-sm text-white px-4 py-2 rounded-xl shadow-elevated bg-gradient-to-r ${colors}`;
    node.textContent = message;
    host.appendChild(node);
    setTimeout(() => { node.style.opacity = '0'; node.style.transform = 'translateY(6px)'; setTimeout(() => node.remove(), 200); }, timeout);
  }

  function autoResizeTextarea(el){ el.style.height = 'auto'; el.style.height = (el.scrollHeight) + 'px'; }

  function attachCopyHandlers(root = chat){
    (root.querySelectorAll('pre') || []).forEach(pre => {
      if (pre.dataset.copyReady) return;
      pre.dataset.copyReady = '1';
      pre.style.overflow = 'auto';
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'copy-btn px-2 py-1 rounded-md text-xs bg-slate-900/80 text-white dark:bg-white/20 hover:brightness-110';
      btn.textContent = 'Copy';
      btn.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(pre.innerText.trim()); showToast('Copied','success'); }
        catch(e){ showToast('Copy failed','error'); }
      });
      pre.appendChild(btn);
    });
  }

  marked.setOptions({ breaks: true, gfm: true });
  function renderMarkdownToHtml(md) {
    const dirty = marked.parse(md || '');
    const clean = DOMPurify.sanitize(dirty, { USE_PROFILES: { html: true } });
    const wrapper = document.createElement('div');
    wrapper.innerHTML = clean;
    wrapper.querySelectorAll('a').forEach(a => { a.target = '_blank'; a.rel = 'noopener noreferrer'; });
    wrapper.querySelectorAll('pre').forEach(p => p.classList.add('not-prose','rounded-lg','border','border-slate-200/50','dark:border-slate-700/50'));
    Prism.highlightAllUnder(wrapper);
    return wrapper.innerHTML;
  }

  function updateScrollBtn(){
    const nearBottom = (chat.scrollHeight - chat.scrollTop - chat.clientHeight) < 120;
    scrollBottomBtn.classList.toggle('hidden', nearBottom);
  }

  function bubble(role, content){
    const row = document.createElement('div');
    row.className = 'w-full flex items-start gap-3 ' + (role === 'user' ? 'justify-end' : 'justify-start');
    const bubble = document.createElement('div');
    const isUser = role === 'user';
    bubble.className = 'msg rounded-2xl px-4 py-3 shadow ' + (isUser ? 'text-white bg-gradient-to-r from-brand-600 to-purple-600 shadow-elevated' : 'bg-white/80 dark:bg-slate-900/60 border border-slate-200/60 dark:border-slate-700/60 backdrop-blur');
    bubble.innerHTML = isUser ? `<div class="tracking-tight">${content.replace(/</g,'&lt;')}</div>` : `<div class="prose prose-slate dark:prose-invert max-w-none">${renderMarkdownToHtml(content)}</div>`;
    row.appendChild(bubble);
    chat.appendChild(row);
    if (!isUser) attachCopyHandlers(bubble);
    row.classList.add('animate-[fadeIn_.2s_ease-out]');
    chat.scrollTop = chat.scrollHeight;
    updateScrollBtn();
    return { row, bubble };
  }

  function createThinkingBubble(){
    const row = document.createElement('div');
    row.className = 'w-full flex items-start gap-3 justify-start';
    const b = document.createElement('div');
    b.className = 'msg rounded-2xl px-4 py-3 bg-white/80 dark:bg-slate-900/60 border border-slate-200/60 dark:border-slate-700/60 backdrop-blur skeleton';
    b.innerHTML = '<div class="h-4 w-3/4 mb-2 bg-slate-200/60 dark:bg-slate-700/60 rounded"></div><div class="h-4 w-5/6 bg-slate-200/60 dark:bg-slate-700/60 rounded"></div>';
    row.appendChild(b);
    chat.appendChild(row);
    chat.scrollTop = chat.scrollHeight;
    return row;
  }

  function setTheme(on){ document.documentElement.classList.toggle('dark', on); localStorage.setItem('theme', on ? 'dark' : 'light'); themeIcon.textContent = on ? '☀️' : '🌙'; }
  if (localStorage.getItem('theme') === 'dark'){ setTheme(true); } else { themeIcon.textContent = '🌙'; }
  toggleTheme.addEventListener('click', () => setTheme(!document.documentElement.classList.contains('dark')));

  function openSheet(){ sheet.classList.remove('hidden'); sheetBackdrop.classList.remove('hidden'); document.body.classList.add('overflow-hidden'); }
  function closeSheet(){ sheet.classList.add('hidden'); sheetBackdrop.classList.add('hidden'); document.body.classList.remove('overflow-hidden'); }
  openSheetBtn?.addEventListener('click', openSheet);
  closeSheetBtn?.addEventListener('click', closeSheet);
  sheetBackdrop?.addEventListener('click', closeSheet);

  function renderConversations(){
    convoList.innerHTML = '';
    const q = (convSearch?.value || '').toLowerCase();
    state.conversations
      .filter(c => !q || (c.title||'').toLowerCase().includes(q))
      .forEach(c => {
        const row = document.createElement('div');
        row.className = 'flex items-center justify-between px-3 py-2 rounded-xl hover:bg-slate-100/70 dark:hover:bg-slate-800/60';
        const left = document.createElement('button');
        left.className = 'text-left flex-1';
        left.innerHTML = `<div class="text-sm font-medium truncate">${(c.title || 'Untitled')}</div><div class="text-[11px] text-slate-500">${new Date(c.updated_at).toLocaleString()}</div>`;
        left.addEventListener('click', async () => { await selectConversation(c.id); closeSheet(); });
        const actions = document.createElement('div');
        actions.className = 'flex items-center gap-2';
        const rename = document.createElement('button');
        rename.className = 'px-2 py-1 text-xs rounded-lg border border-slate-200/70 dark:border-slate-700/70';
        rename.textContent = 'Rename';
        rename.addEventListener('click', async () => {
          const title = await showPrompt({ title: 'Rename conversation', placeholder: 'New title', confirmText: 'Rename' });
          if (!title) return;
          await fetch(`/api/conversations/${c.id}`, { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify({title}) });
          await loadConversations();
        });
        const del = document.createElement('button');
        del.className = 'px-2 py-1 text-xs rounded-lg border border-rose-200/70 text-rose-600 dark:border-rose-700/70';
        del.textContent = 'Delete';
        del.addEventListener('click', async () => {
          const ok = await showConfirm({ title: 'Delete conversation', description: 'This action cannot be undone.', confirmText: 'Delete' });
          if (!ok) return;
          await fetch(`/api/conversations/${c.id}`, { method: 'DELETE' });
          await loadConversations();
          await loadHistory();
          showToast('Conversation deleted','success');
        });
        actions.appendChild(rename);
        actions.appendChild(del);
        row.appendChild(left);
        row.appendChild(actions);
        convoList.appendChild(row);
      });
  }

  async function loadConversations(){
    const res = await fetch('/api/conversations');
    const data = await res.json();
    state.conversations = data.conversations || [];
    state.current = data.current || (state.conversations[0]?.id || null);
    state.is_admin = !!data.is_admin;
    loginBtn.classList.toggle('hidden', state.is_admin);
    logoutBtn.classList.toggle('hidden', !state.is_admin);
    renderConversations();
  }

  async function selectConversation(id){
    await fetch('/api/select_conversation', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({id}) });
    state.current = id;
    await Promise.all([loadConversations(), loadHistory()]);
  }

  async function createConversation(title){
    const res = await fetch('/api/conversations', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({title}) });
    const data = await res.json();
    state.current = data.id;
    await Promise.all([loadConversations(), loadHistory()]);
  }

  function showPrompt({title='Input', description='', placeholder='', confirmText='Confirm', type='text'}={}){
    return new Promise(resolve => {
      const overlay = document.createElement('div');
      overlay.className = 'fixed inset-0 z-50 grid place-items-center bg-black/40 p-4';
      overlay.innerHTML = `
        <div class="w-full max-w-md rounded-2xl glass border border-slate-200/70 dark:border-slate-800/70 shadow-elevated">
          <div class="p-4 border-b border-slate-200/70 dark:border-slate-800/70">
            <div class="text-lg font-semibold">${title}</div>
            <div class="text-sm text-slate-500">${description}</div>
          </div>
          <div class="p-4 space-y-3">
            <input id="_prompt_input" type="${type}" placeholder="${placeholder}" class="w-full px-3 py-2 rounded-xl bg-white/80 dark:bg-slate-900/60 border border-slate-200/70 dark:border-slate-700/70 focus:outline-none focus:ring-2 focus:ring-brand-500" />
            <div class="flex justify-end gap-2">
              <button id="_cancel" class="px-3 py-2 rounded-xl border border-slate-200/70 dark:border-slate-700/70">Cancel</button>
              <button id="_ok" class="px-3 py-2 rounded-xl text-white bg-gradient-to-r from-brand-600 to-purple-600">${confirmText}</button>
            </div>
          </div>
        </div>`;
      document.body.appendChild(overlay);
      const inputEl = overlay.querySelector('#_prompt_input');
      setTimeout(() => inputEl?.focus(), 50);
      overlay.querySelector('#_cancel').addEventListener('click', () => { overlay.remove(); resolve(null); });
      overlay.querySelector('#_ok').addEventListener('click', () => { const val = inputEl.value.trim(); overlay.remove(); resolve(val || null); });
      overlay.addEventListener('keydown', (e) => { if (e.key === 'Escape') { overlay.remove(); resolve(null); } });
    });
  }

  function showConfirm({title='Confirm', description='Are you sure?', confirmText='Confirm'}={}){
    return new Promise(resolve => {
      const overlay = document.createElement('div');
      overlay.className = 'fixed inset-0 z-50 grid place-items-center bg-black/40 p-4';
      overlay.innerHTML = `
        <div class="w-full max-w-md rounded-2xl glass border border-slate-200/70 dark:border-slate-800/70 shadow-elevated">
          <div class="p-4 border-b border-slate-200/70 dark:border-slate-800/70">
            <div class="text-lg font-semibold">${title}</div>
            <div class="text-sm text-slate-500">${description}</div>
          </div>
          <div class="p-4 flex justify-end gap-2">
            <button id="_cancel" class="px-3 py-2 rounded-xl border border-slate-200/70 dark:border-slate-700/70">Cancel</button>
            <button id="_ok" class="px-3 py-2 rounded-xl text-white bg-rose-600 hover:bg-rose-500">${confirmText}</button>
          </div>
        </div>`;
      document.body.appendChild(overlay);
      overlay.querySelector('#_cancel').addEventListener('click', () => { overlay.remove(); resolve(false); });
      overlay.querySelector('#_ok').addEventListener('click', () => { overlay.remove(); resolve(true); });
      overlay.addEventListener('keydown', (e) => { if (e.key === 'Escape') { overlay.remove(); resolve(false); } });
    });
  }

  async function loadHistory(){
    const res = await fetch('/api/history');
    const data = await res.json();
    chat.innerHTML = '';
    (data.history || []).forEach(m => bubble(m.role, m.content));
    if ((data.history || []).length === 0) {
      chat.innerHTML = `
        <div class="w-full grid place-items-center">
          <div class="max-w-xl text-center space-y-4 p-6 rounded-2xl glass border border-slate-200/70 dark:border-slate-800/70 shadow-elevated">
            <div class="inline-flex items-center justify-center h-12 w-12 rounded-xl bg-gradient-to-br from-brand-500 to-purple-600 text-white shadow-elevated">✨</div>
            <h2 class="text-xl font-extrabold bg-clip-text text-transparent bg-gradient-to-r from-brand-600 via-purple-600 to-pink-600">Welcome to AIChatPal</h2>
            <p class="text-slate-600 dark:text-slate-300">Start a conversation and experience fast, beautiful AI answers.</p>
            <div><button class="px-4 py-2 rounded-xl bg-gradient-to-r from-brand-600 to-purple-600 text-white" onclick="document.getElementById('input').focus()">Start typing</button></div>
          </div>
        </div>`;
    }
    if (data.left !== undefined){
      if (data.left < 0) { limitP.textContent = 'Unlimited access active'; }
      else { limitP.textContent = `Free messages left today: ${data.left}`; }
    }
    updateScrollBtn();
    attachCopyHandlers();
  }

  async function sendMessage(){
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    autoResizeTextarea(input);
    bubble('user', text);
    sendBtn.disabled = true;
    const prev = sendBtn.innerHTML;
    sendBtn.innerHTML = '<span class="opacity-80">Sending…</span>';
    const thinkingRow = createThinkingBubble();
    chat.scrollTop = chat.scrollHeight;
    try{
      const res = await fetch('/api/chat', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({message: text}) });
      const data = await res.json();
      thinkingRow.remove();
      if (data.error){ bubble('assistant', `Error: ${data.error}`); showToast(data.error, 'error'); }
      else {
        bubble('assistant', data.reply || '(No response)');
        attachCopyHandlers();
        if (data.left !== undefined){ if (data.left < 0){ limitP.textContent = 'Unlimited access active'; } else { limitP.textContent = `Free messages left today: ${data.left}`; } }
      }
    }catch(e){ thinkingRow.remove(); bubble('assistant', 'Network error.'); showToast('Network error','error'); }
    finally { sendBtn.disabled = false; sendBtn.innerHTML = prev || 'Send'; }
  }

  composer.addEventListener('submit', (e) => { e.preventDefault(); sendMessage(); });
  sendBtn.addEventListener('click', sendMessage);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); sendMessage(); }});
  input.addEventListener('input', () => autoResizeTextarea(input));
  input.addEventListener('focus', () => { setTimeout(() => { chat.scrollTop = chat.scrollHeight; }, 50); });
  window.addEventListener('resize', () => { chat.scrollTop = chat.scrollHeight; });
  chat.addEventListener('scroll', updateScrollBtn);
  scrollBottomBtn.addEventListener('click', () => { chat.scrollTop = chat.scrollHeight; updateScrollBtn(); });

  [fabNewChat, newChatTop, newChatSheet].filter(Boolean).forEach(btn => btn.addEventListener('click', async () => { await fetch('/api/newchat', {method:'POST'}); await Promise.all([loadConversations(), loadHistory()]); showToast('New chat created','success'); }));

  keyBtn.addEventListener('click', async () => {
    const key = await showPrompt({ title: 'Activate key', description: 'Enter your access key to unlock unlimited usage.', placeholder: 'Your key', confirmText: 'Activate' });
    if (!key) return;
    const res = await fetch('/api/key', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({key})});
    const data = await res.json();
    if (data.ok) { showToast('Key activated!','success'); await loadHistory(); } else { showToast(data.error || 'Invalid key','error'); }
  });

  loginBtn.addEventListener('click', async () => {
    const u = await showPrompt({ title: 'Admin login', placeholder: 'Username', confirmText: 'Next' });
    if (!u) return;
    const p = await showPrompt({ title: 'Admin login', placeholder: 'Password', confirmText: 'Login', type: 'password' });
    if (!p) return;
    const res = await fetch('/api/login', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({username:u, password:p}) });
    const data = await res.json();
    if (data.ok) { await loadConversations(); await loadHistory(); showToast('Logged in as admin','success'); } else { showToast(data.error || 'Login failed','error'); }
  });
  logoutBtn.addEventListener('click', async () => { await fetch('/api/logout', {method:'POST'}); await loadConversations(); await loadHistory(); showToast('Logged out','success'); });

  if (convSearch) convSearch.addEventListener('input', renderConversations);

  Promise.all([loadConversations(), loadHistory()]);
  </script>
</body>
</html>
"""


def _create_flask_app() -> Flask:
    app = Flask(__name__)
    # Secret key for cookies
    app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev_secret_change_me")

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

    @app.post("/api/chat")
    def api_chat():
        user_id, _ = _get_or_create_user_id()
        cid, _ = _ensure_current_conversation(user_id)
        data = request.get_json(silent=True) or {}
        text = str(data.get("message", "")).strip()
        if not text:
            return jsonify({"error": "Empty message"}), 400

        # Rate limit for free users
        if not _is_admin_request() and not _has_active_key(user_id):
            current = _get_message_count(user_id)
            if current >= FREE_DAILY_LIMIT:
                return jsonify({"error": "Daily free limit reached (3/day). Use a key to unlock unlimited.", "left": 0}), 429
            _increment_message_count(user_id)

        history = load_conversation_history(user_id, cid)
        now = datetime.now(timezone.utc)
        history.append({"role": "user", "content": text, "timestamp": now})

        contents = _build_gemini_contents(history)
        reply_text, err = _stream_gemini_response(contents)
        if err or not reply_text:
            err_text = err or "Unknown error"
            return jsonify({"error": err_text, "left": _free_left(user_id)})

        history.append({
            "role": "assistant",
            "content": reply_text,
            "timestamp": datetime.now(timezone.utc),
        })
        _save_conversation_history(user_id, history, cid)
        _update_conversation_timestamp(user_id, cid)

        # Auto-title if default
        try:
            _, _, _, col_convos = _get_db_collections()
            doc = col_convos.find_one({"user_id": user_id, "id": cid})
            if doc and (not doc.get("title") or doc.get("title") == "New chat"):
                preview = text.strip().split("\n")[0][:50]
                col_convos.update_one({"user_id": user_id, "id": cid}, {"$set": {"title": preview or "New chat"}})
        except Exception:
            pass

        return jsonify({"reply": reply_text, "left": _free_left(user_id)})

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
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()