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


# -------------------------- Web App --------------------------
from flask import Flask, request, jsonify, make_response, Response
import secrets


HTML_INDEX = """
<!doctype html>
<html lang="en" class="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content, maximum-scale=1">
  <title>AIChatPal</title>
  <meta name="description" content="AIChatPal — Clean, fast AI chat powered by Gemini.">
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
          }
        }
      }
    };
  </script>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/themes/prism-tomorrow.min.css"/>
  <style>
    :root { color-scheme: dark; -webkit-text-size-adjust: 100%; }
    html, body { height: 100%; }
    body { background:
      radial-gradient(80rem 40rem at 20% -10%, rgba(80,80,120,.16), transparent 45%),
      radial-gradient(60rem 30rem at 110% 10%, rgba(60,90,60,.14), transparent 40%),
      linear-gradient(#0b0b10, #09090c);
    }
    .safe-bottom { padding-bottom: max(env(safe-area-inset-bottom), 0px); }
    .msg { max-width: 72ch; }
    .skeleton { position: relative; overflow: hidden; }
    .skeleton::after { content: ""; position: absolute; inset: 0; background: linear-gradient(110deg, transparent 0%, rgba(255,255,255,.06) 40%, rgba(255,255,255,.12) 60%, transparent 100%); animation: shimmer 1.1s linear infinite; }
    @keyframes shimmer { 0% { transform: translateX(-100%);} 100% { transform: translateX(100%);} }
    pre { position: relative; }
    pre code { white-space: pre-wrap; word-break: break-word; }
    .copy-btn { position: absolute; top: .5rem; right: .5rem; }
  </style>
</head>
<body class="font-sans bg-ink text-zinc-100">
  <div class="min-h-[100svh] flex flex-col">
    <header class="p-4 flex justify-center">
      <div class="px-4 py-2 rounded-full text-sm bg-zinc-900/60 border border-zinc-800 text-zinc-200 shadow backdrop-blur">
        ✨ AIChatPal Premier
      </div>
    </header>

    <main class="flex-1">
      <div class="max-w-3xl mx-auto w-full px-3">
        <div id="chat" class="pt-2 pb-28" role="log" aria-live="polite" aria-relevant="additions"></div>
      </div>
    </main>

    <div class="fixed inset-x-0 bottom-0 z-40 safe-bottom bg-gradient-to-t from-ink to-ink/95 border-t border-zinc-900/70">
      <form id="composer" class="max-w-3xl mx-auto px-3 py-3">
        <div class="flex items-end gap-2">
          <div class="flex-1 rounded-2xl bg-zinc-950/60 border border-zinc-900 focus-within:border-zinc-700 transition-colors relative backdrop-blur">
            <div id="attachmentPreview" class="px-3 pt-3 pb-0 hidden flex-wrap gap-2"></div>
            <div class="flex items-center">
              <button type="button" id="attachBtn" class="shrink-0 p-3 text-zinc-400 hover:text-zinc-200" title="Attach">＋</button>
              <textarea id="input" rows="1" placeholder="Ask anything" enterkeyhint="send" autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false" inputmode="text" class="flex-1 bg-transparent text-zinc-100 placeholder:text-zinc-500 p-3 focus:outline-none resize-none"></textarea>
            </div>
            <div id="attachMenu" class="hidden absolute bottom-[54px] left-2 w-56 rounded-xl border border-zinc-800 bg-zinc-950/90 shadow-luxe backdrop-blur p-1">
              <button type="button" id="actionAddPhotos" class="w-full flex items-center gap-2 px-3 py-2 rounded-lg hover:bg-zinc-900 text-zinc-200"><span>🖼️</span><span>Add photos</span></button>
              <button type="button" id="actionTakePhoto" class="w-full flex items-center gap-2 px-3 py-2 rounded-lg hover:bg-zinc-900 text-zinc-200"><span>📷</span><span>Take photo</span></button>
              <button type="button" id="actionAddFiles" class="w-full flex items-center gap-2 px-3 py-2 rounded-lg hover:bg-zinc-900 text-zinc-200"><span>📎</span><span>Add files</span></button>
              <input id="photosInput" type="file" accept="image/*" multiple class="hidden" />
              <input id="cameraInput" type="file" accept="image/*" capture="environment" class="hidden" />
              <input id="filesInput" type="file" multiple class="hidden" />
            </div>
          </div>
          <button id="send" type="submit" class="h-12 w-12 rounded-full bg-emerald-600 hover:bg-emerald-500 text-white shadow-raised grid place-items-center">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="w-5 h-5"><path d="M6 15l6-6 6 6"/></svg>
          </button>
        </div>
        <div class="mt-2 text-[12px] text-zinc-500" id="limit"></div>
      </form>
    </div>
  </div>

  <div id="toasts" class="fixed bottom-[96px] left-1/2 -translate-x-1/2 space-y-2 z-50"></div>

  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.7/dist/purify.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/prism.min.js"></script>
  <script>
  const chat = document.getElementById('chat');
  const input = document.getElementById('input');
  const sendBtn = document.getElementById('send');
  const limitP = document.getElementById('limit');
  const attachBtn = document.getElementById('attachBtn');
  const attachMenu = document.getElementById('attachMenu');
  const photosInput = document.getElementById('photosInput');
  const cameraInput = document.getElementById('cameraInput');
  const filesInput = document.getElementById('filesInput');
  const attachmentPreview = document.getElementById('attachmentPreview');

  const MAX_ATTACHMENTS = 5;
  const MAX_FILE_SIZE = 8 * 1024 * 1024; // 8MB per file
  const MAX_TOTAL_SIZE = 12 * 1024 * 1024; // 12MB per message
  let pendingAttachments = [];

  function showToast(message, variant = 'default', timeout = 2200) {
    const host = document.getElementById('toasts');
    const node = document.createElement('div');
    const colors = variant === 'success' ? 'from-emerald-600 to-green-600' : variant === 'error' ? 'from-rose-600 to-pink-600' : 'from-zinc-700 to-zinc-900';
    node.className = `text-sm text-white px-4 py-2 rounded-xl shadow-raised bg-gradient-to-r ${colors}`;
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
  function renderMarkdownToHtml(md) {
    const dirty = marked.parse(md || '');
    const clean = DOMPurify.sanitize(dirty, { USE_PROFILES: { html: true } });
    const wrapper = document.createElement('div');
    wrapper.innerHTML = clean;
    wrapper.querySelectorAll('a').forEach(a => { a.target = '_blank'; a.rel = 'noopener noreferrer'; });
    wrapper.querySelectorAll('pre').forEach(p => p.classList.add('not-prose','rounded-lg','border','border-zinc-800'));
    Prism.highlightAllUnder(wrapper);
    return wrapper.innerHTML;
  }

  function bubble(role, content){
    const row = document.createElement('div');
    row.className = 'w-full flex items-start gap-3 ' + (role === 'user' ? 'justify-end' : 'justify-start');
    const isUser = role === 'user';
    const bubble = document.createElement('div');
    bubble.className = 'msg rounded-2xl px-4 py-3 ' + (isUser ? 'bg-emerald-600 text-white shadow-raised' : 'bg-zinc-900/70 border border-zinc-800 backdrop-blur');
    bubble.innerHTML = isUser ? `<div class=\"tracking-tight\">${content.replace(/</g,'&lt;')}</div>` : `<div class=\"prose prose-invert max-w-none\">${renderMarkdownToHtml(content)}</div>`;
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
    b.className = 'msg rounded-2xl px-4 py-3 bg-zinc-900/70 border border-zinc-800 skeleton backdrop-blur';
    b.innerHTML = '<div class="h-4 w-3/4 mb-2 bg-zinc-800 rounded"></div><div class="h-4 w-5/6 bg-zinc-800 rounded"></div>';
    row.appendChild(b);
    chat.appendChild(row);
    chat.scrollTop = chat.scrollHeight;
    return row;
  }

  // Always dark by default; preserved for potential future toggle
  function setTheme(on){ document.documentElement.classList.toggle('dark', on); try { localStorage.setItem('theme', on ? 'dark' : 'light'); } catch(e){} }
  if (localStorage.getItem('theme') !== 'light'){ setTheme(true); }

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
          </div>
        </div>`;
    } else {
      items.forEach(m => bubble(m.role, m.content));
    }
    if (data.left !== undefined){
      if (data.left < 0) { limitP.textContent = 'Unlimited access active'; }
      else { limitP.textContent = `Free messages left today: ${data.left}`; }
    }
    attachCopyHandlers();
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
      chip.className = 'inline-flex items-center gap-2 px-2 py-1 rounded-lg border border-zinc-800 bg-zinc-900/70 text-xs text-zinc-200';
      if (a.kind === 'image'){
        const img = document.createElement('img'); img.src = a.base64; img.alt = a.name; img.className = 'h-7 w-7 rounded object-cover'; chip.appendChild(img);
      } else {
        const span = document.createElement('span'); span.textContent = '📎'; chip.appendChild(span);
      }
      const label = document.createElement('span'); label.textContent = `${a.name} • ${bytesToSize(a.size)}`; chip.appendChild(label);
      const x = document.createElement('button'); x.type = 'button'; x.className = 'ml-1 text-zinc-400 hover:text-zinc-200'; x.textContent = '✕';
      x.addEventListener('click', () => { pendingAttachments.splice(idx,1); renderAttachmentPreview(); });
      chip.appendChild(x);
      frag.appendChild(chip);
    });
    attachmentPreview.innerHTML = '';
    attachmentPreview.appendChild(frag);
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
      const reader = new FileReader();
      reader.onload = () => {
        const base64 = String(reader.result || '');
        const kind = (file.type || '').startsWith('image/') ? 'image' : 'file';
        pendingAttachments.push({ name: file.name, mime: file.type || 'application/octet-stream', size: file.size, base64, kind });
        renderAttachmentPreview();
      };
      reader.readAsDataURL(file);
    });
  }

  function toggleAttachMenu(show){
    const willShow = show === undefined ? attachMenu.classList.contains('hidden') : !!show;
    attachMenu.classList.toggle('hidden', !willShow);
  }

  async function sendMessage(){
    const text = input.value.trim();
    if (!text && !pendingAttachments.length) return;
    input.value = '';
    autoResizeTextarea(input);
    bubble('user', text || (pendingAttachments.length ? '(Sent attachments)' : ''));
    const payloadAttachments = pendingAttachments.map(a => ({ name: a.name, mime: a.mime, size: a.size, data: (a.base64.split(',')[1] || '') }));
    pendingAttachments = []; renderAttachmentPreview(); toggleAttachMenu(false);
    sendBtn.disabled = true;
    const prev = sendBtn.innerHTML;
    sendBtn.innerHTML = '<span class="opacity-80">…</span>';
    const thinkingRow = createThinkingBubble();
    chat.scrollTop = chat.scrollHeight;
    try{
      const res = await fetch('/api/chat', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({message: text, attachments: payloadAttachments}) });
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

  document.getElementById('composer').addEventListener('submit', (e) => { e.preventDefault(); sendMessage(); });
  sendBtn.addEventListener('click', sendMessage);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); sendMessage(); }});
  input.addEventListener('input', () => autoResizeTextarea(input));
  input.addEventListener('focus', () => { setTimeout(() => { chat.scrollTop = chat.scrollHeight; }, 50); });

  // Attachments menu and actions
  attachBtn?.addEventListener('click', (e) => { e.stopPropagation(); toggleAttachMenu(); });
  document.addEventListener('click', (e) => { if (!attachMenu.contains(e.target) && e.target !== attachBtn) toggleAttachMenu(false); });
  document.getElementById('actionAddPhotos')?.addEventListener('click', () => photosInput.click());
  document.getElementById('actionTakePhoto')?.addEventListener('click', () => cameraInput.click());
  document.getElementById('actionAddFiles')?.addEventListener('click', () => filesInput.click());
  photosInput.addEventListener('change', (e) => { addFiles(e.target.files); photosInput.value = ''; toggleAttachMenu(false); });
  cameraInput.addEventListener('change', (e) => { addFiles(e.target.files); cameraInput.value = ''; toggleAttachMenu(false); });
  filesInput.addEventListener('change', (e) => { addFiles(e.target.files); filesInput.value = ''; toggleAttachMenu(false); });

  loadHistory();
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
                    size = int(a.get("size") or 0)
                    if not data_b64:
                        continue
                    if size > 8 * 1024 * 1024:
                        return jsonify({"error": f"{name} is too large (max 8MB)", "left": _free_left(user_id)}), 400
                    total_size += size
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

        contents = _build_gemini_contents(history, latest_attachments=attachment_parts)
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
                preview = (text or user_content).strip().split("\n")[0][:50]
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