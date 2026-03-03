# Gemini Telegram Agent (Scaffolded)

This project bridges Telegram messages to Gemini CLI.

## Quick Start

1. Create `.venv` and install dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

2. Configure `.env` and token file.

3. Run locally:

```bash
.venv/bin/python bot.py
```

## systemd (user)

Use `telegram-gemini-bot.service.tmpl` as your user service template.
