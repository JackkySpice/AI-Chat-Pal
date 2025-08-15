AIChatPal (Web Chat UI + Google Gemini)

A simple, production-friendly web chat app powered by Google Gemini using the `google-genai` SDK with MongoDB persistence and in-memory fallback. Built with Flask and a responsive chat UI.

[Deploy to Render](https://render.com/deploy?repo=https://github.com/JackkySpice/AI-Chat-Pal)

Requirements
- Python 3.13
- Linux container friendly

Tech Stack
- Flask 2.x (Werkzeug 2.0.3)
- google-genai (latest)
- pymongo[srv]==3.12.0 with fallback to mongomock==4.1.2
- urllib3==1.26.18, six==1.16.0
- python-dotenv==0.19.1

Files
- `requirements.txt`: pinned versions
- `main.py`: Flask app, Gemini integration, persistence
- `keys.py`: demo keys mapping to expiry datetimes
- `imghdr.py`: Python 3.13 compatibility shim
- `README.md`: this file

Environment Variables
- `GEMINI_API_KEY`: Google Gemini API key (required for model responses)
- `MONGODB_URI`: optional MongoDB URI (fallback to in-memory `mongomock` if absent/unavailable)
- `GEMINI_MODEL`: optional model name (default `gemini-2.5-pro`)
- `GEMINI_SYSTEM_PROMPT`: optional system instruction string
- `FLASK_SECRET_KEY`: optional Flask secret key for cookies (a random string)
- `ENABLE_DAILY_RESET_THREAD`: optional; set to `0` to disable the built-in daily reset thread (defaults to enabled)

Behavior
- Free tier: 3 messages/day per user (reset daily at 00:00 server time)
- Paid: Activate with a demo key to unlock unlimited until the stored expiry (`keys_in_use` collection)
- Persistence: `chat_history_db` with collections `users`, `history`, `keys_in_use`.
  - `users`: `{user_id, message_count}`
  - `history`: `{user_id, conversation_history: [{role, content, timestamp}]}`
  - `keys_in_use`: `{user_id, key, valid_until}`
- In-memory fallback: `mongomock` used automatically if MongoDB is not configured or unreachable
- Conversation memory: last 20 messages; timestamps are Python datetimes

Install
```bash
python3 -m pip install --break-system-packages -r requirements.txt
```

Run
```bash
# Either set env vars directly
export GEMINI_API_KEY=YOUR_GEMINI_API_KEY
# Optional:
# export MONGODB_URI=mongodb+srv://...
# export GEMINI_MODEL=gemini-2.5-pro
# export GEMINI_SYSTEM_PROMPT="You are AIChatPal, a helpful assistant."
# export FLASK_SECRET_KEY="change_me"
# export ENABLE_DAILY_RESET_THREAD=1

python3 main.py
```

Using a .env file
- `.env` is supported for local/dev. Create a file named `.env` next to `main.py`:
```
GEMINI_API_KEY=your_key_here
MONGODB_URI=
GEMINI_MODEL=gemini-2.5-pro
GEMINI_SYSTEM_PROMPT=You are AIChatPal, a helpful assistant.
FLASK_SECRET_KEY=change_me
ENABLE_DAILY_RESET_THREAD=1
```

Notes
- The app serves a responsive chat page at `http://localhost:8080/`.
- Message history is stored per-browser using a secure cookie `uid` to identify a user.
- If `GEMINI_API_KEY` is not set, `/api/chat_stream` will return a clear error message: `Gemini is not configured. Please set GEMINI_API_KEY.`
- Now includes HTTP compression, basic security headers, streaming responses at `/api/chat_stream`, PWA endpoints (`/manifest.json`, `/sw.js`, `/icon.svg`), improved accessibility (ARIA labels, keyboard support), model toggle, theme toggle, export/clear endpoints, and an unlock dialog for unlimited mode.
