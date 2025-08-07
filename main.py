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
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\"> 
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"> 
  <title>AIChatPal</title>
  <script src=\"https://cdn.tailwindcss.com\"></script>
  <style>
    .msg { max-width: 80%; }
    .scroll-area { height: calc(100vh - 200px); }
  </style>
</head>
<body class=\"bg-gray-50 text-gray-900\">
  <div class=\"min-h-screen flex flex-col\">
    <header class=\"bg-gradient-to-r from-indigo-500 via-purple-500 to-pink-500 text-white p-4 shadow\">
      <div class=\"max-w-3xl mx-auto flex items-center justify-between\">
        <h1 class=\"text-xl font-bold\">AIChatPal</h1>
        <div class=\"flex gap-2\">
          <button id=\"newChatBtn\" class=\"px-3 py-1 rounded bg-white/20 hover:bg-white/30\">New chat</button>
          <button id=\"keyBtn\" class=\"px-3 py-1 rounded bg-white/20 hover:bg-white/30\">Activate key</button>
        </div>
      </div>
    </header>

    <main class=\"flex-1\">
      <div class=\"max-w-3xl mx-auto p-4\">
        <div id=\"chat\" class=\"scroll-area overflow-y-auto rounded-lg border bg-white p-3 space-y-3 shadow\"></div>
        <div class=\"mt-4 flex items-end gap-2\">
          <textarea id=\"input\" rows=\"2\" placeholder=\"Type your message...\" class=\"flex-1 rounded border p-3 focus:outline-none focus:ring-2 focus:ring-indigo-500\"></textarea>
          <button id=\"send\" class=\"h-12 px-5 rounded bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50\">Send</button>
        </div>
        <p id=\"limit\" class=\"text-sm text-gray-500 mt-2\"></p>
      </div>
    </main>

    <footer class=\"text-center text-xs text-gray-500 py-4\">Powered by Gemini</footer>
  </div>

<script>
const chat = document.getElementById('chat');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const newChatBtn = document.getElementById('newChatBtn');
const keyBtn = document.getElementById('keyBtn');
const limitP = document.getElementById('limit');

function renderMessage(role, content) {
  const row = document.createElement('div');
  row.className = 'w-full flex ' + (role === 'user' ? 'justify-end' : 'justify-start');
  const bubble = document.createElement('div');
  bubble.className = 'msg rounded-2xl px-4 py-2 shadow ' + (role === 'user' ? 'bg-indigo-600 text-white' : 'bg-gray-100');
  bubble.textContent = content;
  row.appendChild(bubble);
  chat.appendChild(row);
  chat.scrollTop = chat.scrollHeight;
}

function renderThinking() {
  const row = document.createElement('div');
  row.className = 'w-full flex justify-start';
  const bubble = document.createElement('div');
  bubble.className = 'msg rounded-2xl px-4 py-2 shadow bg-gray-100 italic text-gray-500';
  bubble.textContent = 'Thinking…';
  row.appendChild(bubble);
  chat.appendChild(row);
  chat.scrollTop = chat.scrollHeight;
  return row;
}

async function loadHistory() {
  const res = await fetch('/api/history');
  const data = await res.json();
  chat.innerHTML = '';
  (data.history || []).forEach(m => renderMessage(m.role, m.content));
  if (data.left !== undefined) {
    if (data.left < 0) {
      limitP.textContent = '';
    } else {
      limitP.textContent = `Free messages left today: ${data.left}`;
    }
  }
}

async function sendMessage() {
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  renderMessage('user', text);
  sendBtn.disabled = true;
  const thinkingNode = renderThinking();
  try {
    const res = await fetch('/api/chat', {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({message: text})});
    const data = await res.json();
    thinkingNode.remove();
    if (data.error) {
      renderMessage('assistant', `Error: ${data.error}`);
    } else {
      renderMessage('assistant', data.reply || '(No response)');
      if (data.left !== undefined) {
        if (data.left < 0) { limitP.textContent = ''; } else { limitP.textContent = `Free messages left today: ${data.left}`; }
      }
    }
  } catch(e) {
    thinkingNode.remove();
    renderMessage('assistant', 'Network error.');
  } finally {
    sendBtn.disabled = false;
  }
}

sendBtn.addEventListener('click', sendMessage);
input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }});
newChatBtn.addEventListener('click', async () => {
  await fetch('/api/newchat', {method: 'POST'});
  await loadHistory();
});
keyBtn.addEventListener('click', async () => {
  const key = prompt('Enter your key:');
  if (!key) return;
  const res = await fetch('/api/key', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({key})});
  const data = await res.json();
  if (data.ok) {
    alert('Key activated!');
  } else {
    alert(data.error || 'Invalid key');
  }
});

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

    def _free_left(user_id: int) -> int:
        if _has_active_key(user_id):
            return -1
        used = _get_message_count(user_id)
        left = max(0, FREE_DAILY_LIMIT - used)
        return left

    @app.get("/")
    def index() -> Response:
        user_id, resp = _get_or_create_user_id()
        if resp is None:
            return Response(HTML_INDEX, mimetype="text/html")
        resp.set_data(HTML_INDEX)
        resp.mimetype = "text/html"
        return resp

    @app.get("/api/history")
    def api_history():
        user_id, resp = _get_or_create_user_id()
        history = load_conversation_history(user_id)
        payload = {
            "history": [{"role": m.get("role"), "content": m.get("content")} for m in history],
            "left": _free_left(user_id),
        }
        if resp is None:
            return jsonify(payload)
        resp.set_data(json.dumps(payload))
        resp.mimetype = "application/json"
        return resp

    @app.post("/api/newchat")
    def api_newchat():
        user_id, _ = _get_or_create_user_id()
        _save_conversation_history(user_id, [])
        return jsonify({"ok": True})

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

    @app.post("/api/chat")
    def api_chat():
        user_id, _ = _get_or_create_user_id()
        data = request.get_json(silent=True) or {}
        text = str(data.get("message", "")).strip()
        if not text:
            return jsonify({"error": "Empty message"}), 400

        # Rate limit for free users
        if not _has_active_key(user_id):
            current = _get_message_count(user_id)
            if current >= FREE_DAILY_LIMIT:
                return jsonify({"error": "Daily free limit reached (3/day). Use a key to unlock unlimited.", "left": 0}), 429
            _increment_message_count(user_id)

        history = load_conversation_history(user_id)
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
        _save_conversation_history(user_id, history)

        return jsonify({"reply": reply_text, "left": _free_left(user_id)})

    # Optional admin logs endpoint (no auth in web demo)
    @app.get("/adminJackLogs")
    def admin_logs():
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