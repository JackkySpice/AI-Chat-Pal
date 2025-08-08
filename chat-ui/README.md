# Chat UI – Thoughts + Output

A lightweight, standalone chat UI that separates a visible Thoughts panel (concise reasoning summary and activity log) from the actual Answer output. No private chain-of-thought is displayed.

## Run

- Open `index.html` in a browser. No build step needed.
- Optional: serve locally (recommended for CORS/file access stability):
  - Python: `python3 -m http.server 8000` then visit `http://localhost:8000/chat-ui/`

## Files

- `index.html`: Structure and markup
- `styles.css`: Visual design
- `app.js`: Interaction logic (rendering, streaming effect, tabs, theme)

## Design

- Thoughts panel shows:
  - Plan: a brief, high-level approach
  - Activity: a minimal execution log (safe to display)
- Answer panel contains the final response, streamed for a lively experience.
- Includes a theme toggle and a sidebar for conversations.

## Notes

This UI is inspired by modern chat assistants. It is not an exact replica of any specific product. It avoids exposing private chain-of-thought and instead presents a concise, user-facing summary and activity log.