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
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"> 
  <title>AIChatPal · Premium</title>
  <meta name="description" content="AIChatPal — fast, elegant AI chat powered by Gemini.">
  <meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
  <meta name="theme-color" content="#0b1020" media="(prefers-color-scheme: dark)">
  <meta property="og:title" content="AIChatPal · Premium">
  <meta property="og:description" content="A beautiful, responsive AI chat experience powered by Gemini.">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop stop-color='%236366f1' offset='0'/><stop stop-color='%237b61ff' offset='1'/></linearGradient></defs><rect width='64' height='64' rx='14' fill='url(%23g)'/><path d='M36 12L12 36h14l-2 16 24-24H34l2-16z' fill='white' opacity='.95'/></svg>">
  <script src="https://cdn.tailwindcss.com?plugins=typography,forms,line-clamp"></script>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/themes/prism-tomorrow.min.css"/>
  <script>
    tailwind.config = {
      theme: {
        fontFamily: {
          sans: ['Inter', 'ui-sans-serif', 'system-ui', 'Segoe UI', 'Roboto', 'Helvetica', 'Arial', 'Apple Color Emoji', 'Segoe UI Emoji']
        },
        extend: {
          colors: {
            brand: {
              50:'#eef2ff', 100:'#e0e7ff', 200:'#c7d2fe', 300:'#a5b4fc', 400:'#818cf8',
              500:'#6366f1', 600:'#4f46e5', 700:'#4338ca', 800:'#3730a3', 900:'#312e81'
            }
          },
          boxShadow: {
            elevated: '0 10px 30px -12px rgba(2,6,23,0.35)'
          },
          keyframes: {
            shimmer: { '0%': { backgroundPosition: '0% 50%' }, '100%': { backgroundPosition: '100% 50%'} },
            fadeInUp: { '0%': { opacity: 0, transform: 'translateY(6px)' }, '100%': { opacity: 1, transform: 'translateY(0)' } },
            float: { '0%': { transform: 'translateY(0px)' }, '50%': { transform: 'translateY(-14px)' }, '100%': { transform: 'translateY(0px)' } },
            spin: { '0%': { transform: 'rotate(0deg)' }, '100%': { transform: 'rotate(360deg)' } },
            typing: { '0%, 80%, 100%': { transform: 'translateY(0)', opacity: .6 }, '40%': { transform: 'translateY(-4px)', opacity: 1 } }
          },
          animation: {
            shimmer: 'shimmer 2s ease-in-out infinite',
            fadeInUp: 'fadeInUp .25s ease-out both',
            float: 'float 12s ease-in-out infinite',
            spin: 'spin 1s linear infinite'
          }
        }
      },
      darkMode: 'class'
    };
  </script>
  <style>
    :root { color-scheme: light dark; }
    .glass { backdrop-filter: saturate(140%) blur(14px); background: rgba(255,255,255,0.55); }
    .dark .glass { background: rgba(17,24,39,0.5); }
    .scroll-area { height: calc(100vh - 240px); }
    @supports (height: 100dvh) {
      .scroll-area { height: calc(100dvh - 220px); }
    }
    @media (max-width: 768px) {
      .scroll-area { height: calc(100svh - 180px - env(safe-area-inset-bottom)); }
      .glass { backdrop-filter: saturate(120%) blur(8px); }
      .decorative-bg { display: none; }
    }
    .mobile-safe-bottom { padding-bottom: max(env(safe-area-inset-bottom), 0px); }
    .safe-fab { bottom: calc(1.25rem + env(safe-area-inset-bottom)); }
    .scroll-area { overscroll-behavior: contain; -webkit-overflow-scrolling: touch; }
    body.drawer-open { overflow: hidden; }
    .msg { max-width: 72ch; }
    .typing-dot { width: 6px; height: 6px; border-radius: 999px; background: currentColor; opacity: .6; display: inline-block; animation: typing 1.2s infinite ease-in-out; }
    .typing-dot:nth-child(2) { animation-delay: .15s }
    .typing-dot:nth-child(3) { animation-delay: .30s }
    .spinner { width: 16px; height: 16px; border-radius: 9999px; border: 2px solid rgba(255,255,255,.45); border-top-color: transparent; }
    ::-webkit-scrollbar { width: 10px; height: 10px }
    ::-webkit-scrollbar-thumb { background: linear-gradient(180deg, #475569, #1f2937); border-radius: 999px }
    .dark ::-webkit-scrollbar-thumb { background: linear-gradient(180deg, #94a3b8, #475569) }

    /* Luxury palette and utilities */
    :root {
      --lux-1: #4f46e5; /* indigo-600 */
      --lux-2: #7c3aed; /* violet-600 */
      --lux-3: #c026d3; /* fuchsia-600 (muted) */
    }
    .dark:root {
      --lux-1: #6366f1;
      --lux-2: #7c3aed;
      --lux-3: #d946ef;
    }
    .lux-gradient { background-image: linear-gradient(90deg, var(--lux-1), var(--lux-2), var(--lux-3)); }
    .lux-bubble-user {
      background-image: linear-gradient(90deg, var(--lux-1), var(--lux-2), var(--lux-3));
      color: #fff;
      box-shadow: 0 10px 30px -12px rgba(2,6,23,0.35);
      border: 1px solid rgba(255,255,255,0.12);
    }

    /* Reduced motion/softer visuals */
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { animation-duration: 0.001ms !important; animation-iteration-count: 1 !important; transition-duration: 0.001ms !important; scroll-behavior: auto !important; }
      .typing-dot { animation: none !important; opacity: .5; }
      .glass { backdrop-filter: none; background: rgba(255,255,255,0.75); }
      .dark .glass { background: rgba(17,24,39,0.75); }
    }
  </style>
</head>
<body class="font-sans bg-[radial-gradient(1200px_500px_at_10%_-10%,rgba(99,102,241,.12),transparent),radial-gradient(1000px_500px_at_90%_10%,rgba(236,72,153,.10),transparent)] dark:bg-[radial-gradient(1200px_500px_at_10%_-10%,rgba(99,102,241,.25),transparent),radial-gradient(1000px_500px_at_90%_10%,rgba(236,72,153,.18),transparent)] text-slate-900 dark:text-slate-100">
  <!-- Decorative animated background -->
  <div aria-hidden="true" class="pointer-events-none fixed inset-0 -z-10 overflow-hidden decorative-bg">
    <div class="absolute -top-20 -left-20 h-80 w-80 rounded-full blur-3xl opacity-40 dark:opacity-30 bg-gradient-to-br from-brand-400 to-purple-500 animate-float"></div>
    <div class="absolute top-1/3 -right-16 h-72 w-72 rounded-full blur-3xl opacity-30 dark:opacity-25 bg-gradient-to-br from-pink-400 to-orange-400 [animation-delay:4s] animate-float"></div>
    <div class="absolute bottom-0 left-1/3 h-64 w-64 rounded-full blur-3xl opacity-25 dark:opacity-20 bg-gradient-to-br from-emerald-400 to-teal-500 [animation-delay:8s] animate-float"></div>
  </div>
  <div class="min-h-[100svh] flex text-[15px] md:text-base leading-relaxed">
    <!-- Sidebar -->
    <aside class="hidden md:flex fixed md:static inset-y-0 left-0 z-40 w-80 flex-col border-r border-slate-200 dark:border-slate-800 glass">
      <div class="p-5 flex items-center justify-between">
        <div class="flex items-center gap-3">
          <div class="h-8 w-8 rounded-xl bg-gradient-to-br from-brand-500 to-purple-500 shadow-elevated"></div>
          <div>
            <div class="font-extrabold tracking-tight">AIChatPal</div>
            <div class="text-xs text-slate-500 dark:text-slate-400">Your premium AI chat</div>
          </div>
        </div>
        <button id="themeToggle" class="px-2 py-1 text-xs rounded-lg bg-slate-200/70 dark:bg-slate-700/70 hover:bg-slate-200 dark:hover:bg-slate-700 transition">
          <span class="inline dark:hidden">☀️</span>
          <span class="hidden dark:inline">🌙</span>
        </button>
      </div>
      <div class="px-5">
        <button id="newChatBtn" class="w-full mb-4 px-4 py-2.5 rounded-xl lux-gradient text-white shadow-elevated transition hover:brightness-110">New chat</button>
        <div class="relative mb-4">
          <input id="convSearch" placeholder="Search chats" class="w-full px-10 py-2 rounded-xl bg-white/70 dark:bg-slate-900/60 border border-slate-200 dark:border-slate-700 focus:outline-none focus:ring-2 focus:ring-brand-500" />
          <div class="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400">🔎</div>
        </div>
        <div class="text-[11px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400 mb-2">Saved chats</div>
      </div>
      <nav id="convoList" class="flex-1 overflow-y-auto px-3 space-y-1"></nav>
      <div class="p-5 border-t border-slate-200 dark:border-slate-800 space-y-2">
        <button id="keyBtn" class="w-full px-4 py-2.5 rounded-xl bg-white/70 dark:bg-slate-900/60 border border-slate-200 dark:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-800 transition">Activate key</button>
        <div class="flex gap-2">
          <button id="loginBtn" class="flex-1 px-4 py-2.5 rounded-xl bg-white/70 dark:bg-slate-900/60 border border-slate-200 dark:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-800 transition">Admin login</button>
          <button id="logoutBtn" class="hidden flex-1 px-4 py-2.5 rounded-xl bg-white/70 dark:bg-slate-900/60 border border-slate-200 dark:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-800 transition">Logout</button>
        </div>
      </div>
    </aside>
    <div id="drawerBackdrop" class="md:hidden fixed inset-0 z-30 bg-black/40 hidden"></div>

    <!-- Main -->
    <div class="flex-1 flex flex-col">
      <header class="sticky top-0 z-20 glass border-b border-slate-200 dark:border-slate-800">
        <div class="max-w-6xl mx-auto px-4 py-3 flex items-center justify-between">
          <div class="flex items-center gap-3">
            <button id="mobileMenu" class="md:hidden px-3 py-2 rounded-lg bg-slate-900/5 dark:bg-white/10">☰</button>
            <h1 class="text-lg md:text-xl font-extrabold bg-clip-text text-transparent bg-gradient-to-r from-brand-600 via-purple-600 to-pink-600">AIChatPal</h1>
            <span class="text-[11px] px-2 py-1 rounded-lg bg-gradient-to-r from-brand-600/15 to-purple-600/15 text-brand-700 dark:text-brand-200 border border-brand-500/20">Gemini</span>
            <span id="adminBadge" class="hidden text-[11px] px-2 py-1 rounded-lg bg-gradient-to-r from-green-500/20 to-emerald-500/20 text-emerald-600 dark:text-emerald-300 border border-emerald-500/30">Admin</span>
          </div>
          <div class="flex items-center gap-2">
            <button id="newChatBtnTop" class="px-3 py-2 rounded-lg lux-gradient text-white shadow-elevated hover:brightness-110 transition">New chat</button>
            <button id="keyBtnTop" class="px-3 py-2 rounded-lg bg-gradient-to-r from-pink-600/20 to-orange-600/20 text-pink-700 dark:text-pink-200 hover:from-pink-600/30 hover:to-orange-600/30 transition">Activate key</button>
          </div>
        </div>
      </header>

      <main class="flex-1">
        <div class="max-w-6xl mx-auto p-4 grid grid-cols-1">
          <div id="chat" class="scroll-area overflow-y-auto rounded-2xl border border-slate-200/70 dark:border-slate-800/70 bg-white/70 dark:bg-slate-900/60 glass p-4 md:p-6 space-y-4 md:space-y-5 shadow-elevated"></div>
          <div class="mt-4 rounded-2xl border border-slate-200/70 dark:border-slate-800/70 bg-white/70 dark:bg-slate-900/60 glass p-3 md:p-4 shadow-elevated mobile-safe-bottom">
            <div class="flex items-end gap-2">
              <div class="flex-1">
                <textarea id="input" rows="2" placeholder="Type your message..." class="w-full resize-none rounded-xl border border-slate-200 dark:border-slate-700 bg-white/80 dark:bg-slate-900/60 p-3 md:p-3.5 focus:outline-none focus:ring-2 focus:ring-brand-500 placeholder:text-slate-400"></textarea>
                <div class="mt-2 flex items-center justify-between text-xs text-slate-500">
                  <div class="flex items-center gap-2">
                    <span class="hidden sm:inline">Shift+Enter for new line</span>
                  </div>
                  <p id="limit" class="px-2 py-0.5 rounded-full bg-slate-100 dark:bg-slate-800 border border-slate-200/70 dark:border-slate-700/70 text-slate-600 dark:text-slate-300"></p>
                </div>
              </div>
              <button id="send" class="h-12 md:h-12 px-5 md:px-6 rounded-xl lux-gradient text-white font-semibold hover:brightness-110 disabled:opacity-50 shadow-elevated transition">Send</button>
            </div>
          </div>
        </div>

        <button id="fabNewChat" class="md:hidden fixed safe-fab right-5 h-12 w-12 rounded-full shadow-elevated text-white lux-gradient">＋</button>
        <button id="scrollBottom" class="hidden fixed safe-fab right-5 md:right-10 z-40 h-10 w-10 rounded-full shadow-elevated text-white lux-gradient">↓</button>
      </main>

      <footer class="text-center text-xs text-slate-500 py-4">Powered by Gemini</footer>
    </div>
  </div>

  <!-- Toasts -->
  <div id="toasts" class="fixed bottom-5 left-1/2 -translate-x-1/2 space-y-2 z-50"></div>

  <!-- External libs -->
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.7/dist/purify.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/prism.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/components/prism-python.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/components/prism-javascript.min.js"></script>

  <script>
  // State and elements
  const chat = document.getElementById('chat');
  const input = document.getElementById('input');
  const sendBtn = document.getElementById('send');
  const newChatBtn = document.getElementById('newChatBtn');
  const newChatBtnTop = document.getElementById('newChatBtnTop');
  const keyBtn = document.getElementById('keyBtn');
  const keyBtnTop = document.getElementById('keyBtnTop');
  const limitP = document.getElementById('limit');
  const convoList = document.getElementById('convoList');
  const renameBtn = document.getElementById('renameBtn'); // legacy id not present; guarded below
  const deleteBtn = document.getElementById('deleteBtn'); // legacy id not present; guarded below
  const loginBtn = document.getElementById('loginBtn');
  const logoutBtn = document.getElementById('logoutBtn');
  const adminBadge = document.getElementById('adminBadge');
  const themeToggle = document.getElementById('themeToggle');
  const mobileMenuBtn = document.getElementById('mobileMenu');
  const convSearch = document.getElementById('convSearch');
  const fabNewChat = document.getElementById('fabNewChat');
  const scrollBottomBtn = document.getElementById('scrollBottom');

  let state = { conversations: [], current: null, is_admin: false };

  // Utilities
  function showToast(message, variant = 'default', timeout = 3000) {
    const host = document.getElementById('toasts');
    const node = document.createElement('div');
    const colors = variant === 'success' ? 'from-emerald-600 to-green-600' : variant === 'error' ? 'from-rose-600 to-pink-600' : 'from-slate-700 to-slate-900';
    node.className = `animate-fadeInUp text-sm text-white px-4 py-2 rounded-xl shadow-elevated bg-gradient-to-r ${colors}`;
    node.textContent = message;
    host.appendChild(node);
    setTimeout(() => { node.style.opacity = '0'; node.style.transform = 'translateY(6px)'; setTimeout(() => node.remove(), 200); }, timeout);
  }

  function autoResizeTextarea(el) {
    el.style.height = 'auto';
    el.style.height = (el.scrollHeight) + 'px';
  }

  function fmtTime(ts) { try { return new Date(ts).toLocaleString(); } catch { return ''; } }

  // Markdown rendering with sanitization and code highlighting
  marked.setOptions({ breaks: true, gfm: true });
  function renderMarkdownToHtml(md) {
    const dirty = marked.parse(md || '');
    const clean = DOMPurify.sanitize(dirty, { USE_PROFILES: { html: true } });
    const wrapper = document.createElement('div');
    wrapper.innerHTML = clean;
    Prism.highlightAllUnder(wrapper);
    // Add copy buttons to code blocks
    wrapper.querySelectorAll('pre').forEach(pre => {
      const btn = document.createElement('button');
      btn.textContent = 'Copy';
      btn.className = 'absolute top-2 right-2 text-[11px] px-2 py-1 rounded bg-black/50 hover:bg-black/70 text-white';
      btn.addEventListener('click', () => {
        const code = pre.querySelector('code')?.innerText || '';
        navigator.clipboard.writeText(code).then(() => showToast('Code copied', 'success'));
      });
      pre.classList.add('relative');
      pre.appendChild(btn);
    });
    return wrapper.innerHTML;
  }

  function updateScrollBtn() {
    const nearBottom = (chat.scrollHeight - chat.scrollTop - chat.clientHeight) < 120;
    scrollBottomBtn.classList.toggle('hidden', nearBottom);
  }

  // Message rendering
  function avatarFor(role) {
    if (role === 'user') {
      return '<div class="h-8 w-8 rounded-full bg-gradient-to-br from-slate-400 to-slate-600"></div>';
    }
    return '<div class="h-8 w-8 rounded-xl bg-gradient-to-br from-brand-500 to-purple-600 shadow-elevated"></div>';
  }

  function renderMessage(role, content) {
    const row = document.createElement('div');
    row.className = 'w-full flex items-start gap-3 ' + (role === 'user' ? 'justify-end' : 'justify-start');

    const bubble = document.createElement('div');
    const isUser = role === 'user';
    bubble.className = 'msg rounded-2xl px-4 py-3 md:px-5 md:py-3.5 shadow ' + (isUser ? 'lux-bubble-user' : 'bg-white/80 dark:bg-slate-900/60 border border-slate-200/60 dark:border-slate-700/60 shadow-sm backdrop-blur-sm');
    bubble.innerHTML = isUser ? `<div class="tracking-tight">${content.replace(/</g,'&lt;')}</div>` : `<div class="prose prose-slate dark:prose-invert max-w-none md:prose-lg prose-p:leading-relaxed prose-headings:tracking-tight prose-headings:font-semibold">${renderMarkdownToHtml(content)}</div>`;

    if (isUser) {
      row.appendChild(bubble);
      row.appendChild(createElementFromHTML(avatarFor(role)));
    } else {
      row.appendChild(createElementFromHTML(avatarFor(role)));
      row.appendChild(bubble);
    }

    row.classList.add('animate-fadeInUp');
    chat.appendChild(row);
    chat.scrollTop = chat.scrollHeight;
    updateScrollBtn();
  }

  function createElementFromHTML(htmlString) {
    const div = document.createElement('div');
    div.innerHTML = htmlString.trim();
    return div.firstChild;
  }

  function renderThinking() {
    const row = document.createElement('div');
    row.className = 'w-full flex items-start gap-3 justify-start';
    const bubble = document.createElement('div');
    bubble.className = 'msg rounded-2xl px-4 py-3 shadow italic text-slate-500 bg-slate-50/90 dark:bg-slate-800/80 border border-slate-200/70 dark:border-slate-700/70';
    bubble.innerHTML = '<div class="flex items-center gap-1"><span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span></div>';
    row.appendChild(createElementFromHTML(avatarFor('assistant')));
    row.appendChild(bubble);
    chat.appendChild(row);
    chat.scrollTop = chat.scrollHeight;
    updateScrollBtn();
    return row;
  }

  function renderConversations() {
    convoList.innerHTML = '';
    const q = (convSearch?.value || '').toLowerCase();
    state.conversations
      .filter(c => !q || (c.title||'').toLowerCase().includes(q))
      .forEach(c => {
        const a = document.createElement('button');
        a.className = 'w-full text-left px-3 py-2 rounded-xl hover:bg-slate-100/80 dark:hover:bg-slate-800/80 flex items-center justify-between border border-transparent ' + (state.current === c.id ? 'bg-slate-100/80 dark:bg-slate-800/80 border-slate-200/70 dark:border-slate-700/70' : '');
        const left = document.createElement('div');
        left.innerHTML = `<div class="text-sm font-medium truncate">${(c.title || 'Untitled')}</div><div class="text-[11px] text-slate-500">${fmtTime(c.updated_at)}</div>`;
        a.appendChild(left);
        a.addEventListener('click', async () => { await selectConversation(c.id); });
        convoList.appendChild(a);
    });
  }

  async function loadConversations() {
    const res = await fetch('/api/conversations');
    const data = await res.json();
    state.conversations = data.conversations || [];
    state.current = data.current || (state.conversations[0]?.id || null);
    state.is_admin = !!data.is_admin;
    renderConversations();
    adminBadge.classList.toggle('hidden', !state.is_admin);
    loginBtn.classList.toggle('hidden', state.is_admin);
    logoutBtn.classList.toggle('hidden', !state.is_admin);
  }

  async function selectConversation(id) {
    await fetch('/api/select_conversation', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({id}) });
    state.current = id;
    await Promise.all([loadConversations(), loadHistory()]);
  }

  async function createConversation(title) {
    const res = await fetch('/api/conversations', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({title}) });
    const data = await res.json();
    state.current = data.id;
    await Promise.all([loadConversations(), loadHistory()]);
  }

  // Modal prompts
  function showPrompt({title = 'Input', description = '', placeholder = '', confirmText = 'Confirm', type = 'text', confirmVariant = 'default'} = {}) {
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
              <button id="_ok" class="px-3 py-2 rounded-xl text-white ${confirmVariant==='danger' ? 'bg-rose-600 hover:bg-rose-500' : 'bg-gradient-to-r from-brand-600 to-purple-600 hover:from-brand-500 hover:to-purple-500'}">${confirmText}</button>
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

  function showConfirm({title='Confirm', description='Are you sure?', confirmText='Confirm'} = {}) {
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

  function renderEmptyState() {
    chat.innerHTML = `
      <div class="w-full grid place-items-center">
        <div class="max-w-2xl text-center space-y-4 p-6 rounded-2xl glass border border-slate-200/70 dark:border-slate-800/70 shadow-elevated">
          <div class="inline-flex items-center justify-center h-12 w-12 rounded-xl bg-gradient-to-br from-brand-500 to-purple-600 text-white shadow-elevated">✨</div>
          <h2 class="text-2xl font-extrabold bg-clip-text text-transparent bg-gradient-to-r from-brand-600 via-purple-600 to-pink-600">Welcome to AIChatPal</h2>
          <p class="text-slate-600 dark:text-slate-300">Start a conversation and experience fast, beautiful AI answers with code highlighting, dark mode, and more.</p>
          <div><button class="px-4 py-2 rounded-xl bg-gradient-to-r from-brand-600 to-purple-600 text-white hover:from-brand-500 hover:to-purple-500" onclick="document.getElementById('input').focus()">Start typing</button></div>
        </div>
      </div>`;
  }

  // History / chat
  async function loadHistory() {
    const res = await fetch('/api/history');
    const data = await res.json();
    chat.innerHTML = '';
    (data.history || []).forEach(m => renderMessage(m.role, m.content));
    if ((data.history || []).length === 0) { renderEmptyState(); }
    if (data.left !== undefined) {
      if (data.left < 0) {
        limitP.textContent = 'Unlimited access active';
      } else {
        limitP.textContent = `Free messages left today: ${data.left}`;
      }
    }
    updateScrollBtn();
  }

  async function sendMessage() {
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    autoResizeTextarea(input);
    renderMessage('user', text);
    sendBtn.disabled = true;
    const previous = sendBtn.innerHTML;
    sendBtn.innerHTML = '<span class="inline-flex items-center gap-2"><span class="spinner animate-spin"></span> Sending…</span>';
    const thinkingNode = renderThinking();
    try {
      const res = await fetch('/api/chat', {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({message: text})});
      const data = await res.json();
      thinkingNode.remove();
      if (data.error) {
        renderMessage('assistant', `Error: ${data.error}`);
        showToast(data.error, 'error');
      } else {
        renderMessage('assistant', data.reply || '(No response)');
        if (data.left !== undefined) {
          if (data.left < 0) { limitP.textContent = 'Unlimited access active'; } else { limitP.textContent = `Free messages left today: ${data.left}`; }
        }
      }
    } catch(e) {
      thinkingNode.remove();
      renderMessage('assistant', 'Network error.');
      showToast('Network error', 'error');
    } finally {
      sendBtn.disabled = false;
      sendBtn.innerHTML = previous || 'Send';
    }
  }

  // Actions
  sendBtn.addEventListener('click', sendMessage);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }});
  input.addEventListener('input', () => autoResizeTextarea(input));

  const newChatHandlers = [newChatBtn, newChatBtnTop, fabNewChat].filter(Boolean);
  newChatHandlers.forEach(btn => btn.addEventListener('click', async () => { await fetch('/api/newchat', {method: 'POST'}); await Promise.all([loadConversations(), loadHistory()]); showToast('New chat created','success'); }));

  const keyHandlers = [keyBtn, keyBtnTop].filter(Boolean);
  keyHandlers.forEach(btn => btn.addEventListener('click', async () => {
    const key = await showPrompt({ title: 'Activate key', description: 'Enter your access key to unlock unlimited usage.', placeholder: 'Your key', confirmText: 'Activate' });
    if (!key) return;
    const res = await fetch('/api/key', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({key})});
    const data = await res.json();
    if (data.ok) { showToast('Key activated!','success'); await loadHistory(); } else { showToast(data.error || 'Invalid key','error'); }
  }));

  if (renameBtn) renameBtn.addEventListener('click', async () => {
    if (!state.current) return;
    const title = await showPrompt({ title: 'Rename conversation', placeholder: 'New title', confirmText: 'Rename' });
    if (!title) return;
    await fetch(`/api/conversations/${state.current}`, { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify({title}) });
    await loadConversations();
  });

  if (deleteBtn) deleteBtn.addEventListener('click', async () => {
    if (!state.current) return;
    const ok = await showConfirm({ title: 'Delete conversation', description: 'This action cannot be undone.', confirmText: 'Delete' });
    if (!ok) return;
    await fetch(`/api/conversations/${state.current}`, { method: 'DELETE' });
    await loadConversations();
    await loadHistory();
    showToast('Conversation deleted','success');
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

  mobileMenuBtn.addEventListener('click', () => {
    const aside = document.querySelector('aside');
    const backdrop = document.getElementById('drawerBackdrop');
    const willShow = aside.classList.contains('hidden');
    aside.classList.toggle('hidden');
    backdrop?.classList.toggle('hidden');
    document.body.classList.toggle('drawer-open', willShow);
  });
  document.getElementById('drawerBackdrop')?.addEventListener('click', () => {
    const aside = document.querySelector('aside');
    const backdrop = document.getElementById('drawerBackdrop');
    aside.classList.add('hidden');
    backdrop?.classList.add('hidden');
    document.body.classList.remove('drawer-open');
  });
  themeToggle.addEventListener('click', () => { document.documentElement.classList.toggle('dark'); localStorage.setItem('theme', document.documentElement.classList.contains('dark') ? 'dark' : 'light'); });
  if (localStorage.getItem('theme') === 'dark') { document.documentElement.classList.add('dark'); }
  if (convSearch) convSearch.addEventListener('input', renderConversations);
  if (scrollBottomBtn) scrollBottomBtn.addEventListener('click', () => { chat.scrollTop = chat.scrollHeight; updateScrollBtn(); });
  if (chat) chat.addEventListener('scroll', updateScrollBtn);

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