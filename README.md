# create-gemini-telegram-agent

Open-source scaffold CLI for running a Telegram bot that forwards messages to Gemini CLI.

## What It Creates

- Python Telegram runtime (`bot.py`) with:
  - retries + timeout handling
  - per-chat session pinning
  - `/sessions`, `/sessionuse`, `/sessionclear`, `/sessiondelete`, `/continue`
  - `/cli` command-chain mode for fast Gemini CLI commands
  - Telegram chat-mode guardrails for normal messages
- `.env` runtime configuration
- user `systemd` service template

## Requirements

- Linux with `systemd --user` (recommended)
- Node.js >= 20
- Python >= 3.10
- Gemini CLI installed and authenticated (`gemini --list-sessions` works)
- Telegram bot token from BotFather

## Quick Start

```bash
npx create-gemini-telegram-agent init \
  --dir ./my-gemini-bot \
  --bot-token <telegram_bot_token> \
  --chat-id <your_chat_id>
```

Then verify:

```bash
cd ./my-gemini-bot
npx create-gemini-telegram-agent doctor --dir .
```

## Commands

```bash
create-gemini-telegram-agent init [options]
create-gemini-telegram-agent doctor [--dir <path>]
create-gemini-telegram-agent start [--dir <path>]
create-gemini-telegram-agent sync-commands [--service-name <name>]
create-gemini-telegram-agent uninstall [--service-name <name>] [--purge] [--yes]
```

## Security Defaults

- Bot token written to `~/.config/gemini-telegram-agent/telegram-bot-token.txt` with `0600`
- No token values printed in CLI logs
- Normal Telegram messages run non-YOLO mode by default

## Publishing

```bash
npm run check
npm run pack:dry-run
npm publish --access public
```

## Notes

- A Telegram bot username/ID alone is not enough for setup. Users need the bot **token**.
- For OAuth-backed Gemini usage, authentication happens on each host machine.
