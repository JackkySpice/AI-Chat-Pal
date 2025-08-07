AIChatPalBot (Telegram + Google Gemini)

A production-ready Telegram bot powered by Google Gemini using the `google-genai` SDK with MongoDB persistence and in-memory fallback. Includes a tiny Flask web server (port 8080) for liveness.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/JackkySpice/AI-Chat-Pal)

Requirements
- Python 3.13
- Linux container friendly

Tech Stack
- python-telegram-bot==13.7 (v13 API with Updater/Dispatcher)
- google-genai (latest)
- pymongo[srv]==3.12.0 with fallback to mongomock==4.1.2
- flask==2.0.2 (Werkzeug==2.0.3)
- urllib3==1.26.18, six==1.16.0
- python-dotenv==0.19.1

Files
- `requirements.txt`: pinned versions
- `main.py`: bot, Flask server, Gemini integration
- `keys.py`: demo keys mapping to expiry datetimes
- `imghdr.py`: Python 3.13 compatibility shim
- `README.md`: this file

Environment Variables
- `TELEGRAM_TOKEN`: Telegram bot token (required to start the bot; if absent, only Flask starts)
- `GEMINI_API_KEY`: Google Gemini API key (required for model responses)
- `MONGODB_URI`: optional MongoDB URI (fallback to in-memory `mongomock` if absent/unavailable)
- `GEMINI_MODEL`: optional model name (default `gemini-2.5-pro`)
- `GEMINI_SYSTEM_PROMPT`: optional system instruction string

Behavior
- Commands: `/start`, `/help`, `/key <your_key>`, `/newchat`, `/refresh`, `/logout`, `/adminJackLogs`
- Admin username: `Torionllm` for `/adminJackLogs`
- Free tier: 3 messages/day per user (reset daily at 00:00 server time)
- Paid: `/key` unlocks unlimited until the stored expiry (`keys_in_use` collection)
- Persistence: `chat_history_db` with collections `users`, `history`, `keys_in_use`.
  - `users`: `{user_id, message_count}`
  - `history`: `{user_id, conversation_history: [{role, content, timestamp}]}`
  - `keys_in_use`: `{user_id, key, valid_until}`
- In-memory fallback: `mongomock` used automatically if MongoDB is not configured or unreachable
- Conversation memory: last 20 messages; timestamps are Python datetimes
- New chat prompt: if last message is older than 5 minutes, an inline keyboard asks whether to start fresh
- Telegram sending: sends a "Thinking…" placeholder and edits it with the model’s final text; messages are chunked to <= 4000 chars and sent as plain text
- Flask server: serves `Hello, world!` on `0.0.0.0:8080` in a background thread
- Single-instance guard: `bot.lock` prevents multiple bot instances

Install
```bash
python3 -m pip install --break-system-packages -r requirements.txt
```

Run
```bash
# Or copy .env.example to .env and fill it
export TELEGRAM_TOKEN=YOUR_TELEGRAM_TOKEN
export GEMINI_API_KEY=YOUR_GEMINI_API_KEY
# Optional:
# export MONGODB_URI=mongodb+srv://...
# export GEMINI_MODEL=gemini-2.5-pro
# export GEMINI_SYSTEM_PROMPT="You are AIChatPal, a helpful assistant."

python3 main.py
```

Notes
- Importing `main` should succeed on Python 3.13. External dependencies are imported lazily.
- Without `TELEGRAM_TOKEN`, only Flask runs on port 8080.
- Keys for `/key` are demo keys defined in `keys.py` (e.g., `DEMO-KEY-7D`).
