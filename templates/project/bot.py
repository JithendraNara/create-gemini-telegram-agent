#!/usr/bin/env python3
"""
Telegram Bot -> Gemini CLI bridge.
Receives messages from Telegram, forwards to Gemini CLI, and sends the response back.
"""

import asyncio
import json
import logging
import os
import random
import re
import shlex
import stat
import time
from collections import defaultdict, deque
from pathlib import Path

from telegram import BotCommand, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
# Avoid logging Telegram API URLs that embed the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _get_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError("must be positive")
        return value
    except Exception:
        logger.warning("Invalid %s=%r, using default=%s", name, raw, default)
        return default


def _parse_chat_allowlist(raw: str) -> set[int]:
    allowlist: set[int] = set()
    for part in raw.split(","):
        candidate = part.strip()
        if not candidate:
            continue
        try:
            allowlist.add(int(candidate))
        except ValueError:
            logger.warning("Ignoring invalid chat id in ALLOWED_CHAT_IDS: %r", candidate)
    return allowlist


class BotConfig:
    BOT_TOKEN_FILE = os.path.expanduser(
        os.environ.get("BOT_TOKEN_FILE", "~/.config/gemini-telegram-agent/telegram-bot-token.txt")
    )
    GEMINI_PATH = os.path.expanduser(
        os.environ.get("GEMINI_PATH", "gemini")
    )
    GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip()
    MAX_CONCURRENT_PROCESSES = _get_int_env("MAX_CONCURRENT_PROCESSES", 3)
    REQUEST_TIMEOUT_SEC = _get_int_env("REQUEST_TIMEOUT_SEC", 120)
    MAX_INPUT_CHARS = _get_int_env("MAX_INPUT_CHARS", 2000)
    RATE_LIMIT_PER_MIN = _get_int_env("RATE_LIMIT_PER_MIN", 12)
    RATE_LIMIT_BURST = _get_int_env("RATE_LIMIT_BURST", 3)
    RATE_LIMIT_WINDOW_SEC = _get_int_env("RATE_LIMIT_WINDOW_SEC", 60)
    TELEGRAM_MESSAGE_CHUNK_SIZE = 4000
    TYPING_REFRESH_SEC = _get_int_env("TYPING_REFRESH_SEC", 4)
    PROGRESS_REFRESH_SEC = _get_int_env("PROGRESS_REFRESH_SEC", 5)
    LONG_WAIT_NOTIFY_SEC = _get_int_env("LONG_WAIT_NOTIFY_SEC", 15)
    UPDATE_DEDUPE_TTL_SEC = _get_int_env("UPDATE_DEDUPE_TTL_SEC", 300)
    UPDATE_DEDUPE_MAX = _get_int_env("UPDATE_DEDUPE_MAX", 2000)
    GEMINI_RETRY_ATTEMPTS = _get_int_env("GEMINI_RETRY_ATTEMPTS", 2)
    GEMINI_RETRY_BASE_SEC = _get_int_env("GEMINI_RETRY_BASE_SEC", 2)
    GEMINI_RETRY_MAX_SEC = _get_int_env("GEMINI_RETRY_MAX_SEC", 20)
    ALLOWED_CHAT_IDS = _parse_chat_allowlist(os.environ.get("ALLOWED_CHAT_IDS", ""))
    GEMINI_COMMANDS_DIR = Path(
        os.path.expanduser(os.environ.get("GEMINI_COMMANDS_DIR", "~/.gemini/commands"))
    )
    GEMINI_COMMANDS_PREVIEW_LIMIT = _get_int_env("GEMINI_COMMANDS_PREVIEW_LIMIT", 30)
    TELEGRAM_MENU_MAX_COMMANDS = _get_int_env("TELEGRAM_MENU_MAX_COMMANDS", 100)
    TELEGRAM_DYNAMIC_MENU_MAX = _get_int_env("TELEGRAM_DYNAMIC_MENU_MAX", 24)
    CLI_TIMEOUT_SEC = _get_int_env("CLI_TIMEOUT_SEC", 60)
    CLI_CHAIN_MAX = _get_int_env("CLI_CHAIN_MAX", 8)
    SESSIONS_LIST_DEFAULT_LIMIT = _get_int_env("SESSIONS_LIST_DEFAULT_LIMIT", 25)
    SESSIONS_LIST_MAX_LIMIT = _get_int_env("SESSIONS_LIST_MAX_LIMIT", 200)
    SESSION_IDLE_RESET_SEC = _get_int_env("SESSION_IDLE_RESET_SEC", 1800)


class RuntimeStats:
    start_time = time.time()
    inflight_requests = 0
    waiting_requests = 0
    processed_count = 0
    timeout_count = 0
    error_count = 0
    duplicate_updates = 0


process_semaphore = asyncio.Semaphore(BotConfig.MAX_CONCURRENT_PROCESSES)
request_history: dict[int, deque[float]] = defaultdict(deque)
recent_update_times: dict[str, float] = {}
recent_update_queue: deque[tuple[str, float]] = deque()
recent_update_lock = asyncio.Lock()

RECOVERABLE_GEMINI_ERR_RE = re.compile(
    r"(timeout|timed out|temporar|try again|rate limit|429|500|502|503|504|"
    r"network|socket|econn|eai_again|enotfound|etimedout)",
    flags=re.IGNORECASE,
)
TELEGRAM_COMMAND_NAME_PATTERN = re.compile(r"^[a-z0-9_]{1,32}$")
SESSION_LIST_ITEM_RE = re.compile(
    r"^\s*(\d+)\.\s+(.+?)\s+\(([^()]*)\)\s+\[([^\]]+)\]\s*$"
)
CLI_DENY_FLAGS = {"-p", "--prompt", "-i", "--prompt-interactive"}
CLI_FAST_TOPLEVEL = {
    "mcp",
    "skills",
    "skill",
    "extensions",
    "extension",
    "hooks",
    "hook",
}
CLI_FAST_GLOBAL_FLAGS = {
    "-h",
    "--help",
    "-v",
    "--version",
    "-l",
    "--list-extensions",
    "--list-sessions",
}
BUILTIN_GEMINI_SLASH_MENU = [
    BotCommand("about", "Gemini CLI version/environment info"),
    BotCommand("auth", "Authentication commands"),
    BotCommand("bug", "Report a bug"),
    BotCommand("chat", "Manage conversations"),
    BotCommand("clear", "Clear terminal output"),
    BotCommand("compress", "Compress context/history"),
    BotCommand("docs", "Open Gemini CLI docs"),
    BotCommand("editor", "Configure external editor"),
    BotCommand("extensions", "Manage extensions"),
    BotCommand("ide", "Manage IDE integration"),
    BotCommand("mcp", "Manage MCP servers"),
    BotCommand("memory", "Manage context memory"),
    BotCommand("model", "Select or inspect model"),
    BotCommand("privacy", "Privacy and diagnostics controls"),
    BotCommand("quit", "Exit command"),
    BotCommand("restore", "Restore tool checkpoint"),
    BotCommand("resume", "Resume previous chat session"),
    BotCommand("rewind", "Rewind conversation state"),
    BotCommand("settings", "Open settings"),
    BotCommand("stats", "Conversation stats"),
    BotCommand("theme", "Change theme"),
    BotCommand("tools", "List available tools"),
]

BASE_MENU_COMMANDS = [
    BotCommand("start", "Show bot intro"),
    BotCommand("help", "Show usage and protections"),
    BotCommand("status", "Quick online check"),
    BotCommand("health", "Runtime health details"),
    BotCommand("stats", "Alias for /health details"),
    BotCommand("sessions", "List Gemini sessions"),
    BotCommand("sessionuse", "Set resume target for this chat"),
    BotCommand("sessionclear", "Clear resume target"),
    BotCommand("sessiondelete", "Delete a Gemini session"),
    BotCommand("continue", "Continue from latest session"),
    BotCommand("g", "Run a Gemini prompt (/g <prompt>)"),
    BotCommand("cli", "Fast Gemini CLI chain (/cli <cmd1> ;; <cmd2>)"),
    BotCommand("gcmds", "List Gemini custom slash commands"),
    BotCommand("synccommands", "Re-sync this Telegram command menu"),
]

LOCAL_COMMAND_NAMES = {
    "/start",
    "/help",
    "/status",
    "/health",
    "/stats",
    "/sessions",
    "/sessionuse",
    "/sessionclear",
    "/sessiondelete",
    "/continue",
    "/g",
    "/cli",
    "/gcmds",
    "/synccommands",
}
GEMINI_TELEGRAM_ALIAS_MAP: dict[str, str] = {}
CURRENT_MENU_COMMANDS: list[BotCommand] = list(BASE_MENU_COMMANDS)
chat_resume_target: dict[int, str] = {}
chat_last_activity: dict[int, float] = {}


def _normalize_telegram_command_name(value: str) -> str:
    name = value.strip().lower()
    if name.startswith("/"):
        name = name[1:]
    name = name.replace(":", "_").replace("-", "_").replace("/", "_")
    name = re.sub(r"[^a-z0-9_]", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    if not name:
        return ""
    if not name[0].isalpha():
        name = f"c_{name}"
    return name[:32]


def _resolve_unique_telegram_command_name(base_name: str, used: set[str]) -> str:
    candidate = base_name
    if TELEGRAM_COMMAND_NAME_PATTERN.fullmatch(candidate) and candidate not in used:
        return candidate
    for suffix_idx in range(2, 100):
        suffix = f"_{suffix_idx}"
        trimmed = base_name[: max(1, 32 - len(suffix))]
        candidate = f"{trimmed}{suffix}"
        if TELEGRAM_COMMAND_NAME_PATTERN.fullmatch(candidate) and candidate not in used:
            return candidate
    return ""


def _build_dynamic_gemini_menu() -> tuple[list[BotCommand], dict[str, str]]:
    commands = _discover_gemini_commands()
    if not commands:
        return [], {}

    used_names = {entry.command for entry in BASE_MENU_COMMANDS}
    dynamic_menu: list[BotCommand] = []
    alias_map: dict[str, str] = {}

    for command_name, description in commands:
        normalized = _normalize_telegram_command_name(command_name)
        if not normalized:
            continue
        unique = _resolve_unique_telegram_command_name(normalized, used_names)
        if not unique:
            continue
        used_names.add(unique)

        alias_map[f"/{unique}"] = f"/{command_name}"
        dynamic_menu.append(BotCommand(unique, f"Gemini: {description}"[:256]))

        if len(dynamic_menu) >= BotConfig.TELEGRAM_DYNAMIC_MENU_MAX:
            break

    return dynamic_menu, alias_map


def _build_builtin_gemini_menu() -> list[BotCommand]:
    used = {entry.command for entry in BASE_MENU_COMMANDS}
    menu: list[BotCommand] = []
    for command in BUILTIN_GEMINI_SLASH_MENU:
        if command.command in used:
            continue
        used.add(command.command)
        menu.append(command)
    return menu


def _refresh_telegram_command_registry() -> list[BotCommand]:
    global GEMINI_TELEGRAM_ALIAS_MAP
    global CURRENT_MENU_COMMANDS

    builtin_menu = _build_builtin_gemini_menu()
    dynamic_menu, alias_map = _build_dynamic_gemini_menu()
    GEMINI_TELEGRAM_ALIAS_MAP = alias_map
    merged = list(BASE_MENU_COMMANDS) + builtin_menu + dynamic_menu
    CURRENT_MENU_COMMANDS = merged[: BotConfig.TELEGRAM_MENU_MAX_COMMANDS]
    return CURRENT_MENU_COMMANDS


def _extract_toml_description(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return "Custom Gemini command"

    match = re.search(r'^\s*description\s*=\s*"([^"]+)"\s*$', text, flags=re.MULTILINE)
    if match:
        return match.group(1).strip()
    return "Custom Gemini command"


def _discover_gemini_commands(limit: int | None = None) -> list[tuple[str, str]]:
    root = BotConfig.GEMINI_COMMANDS_DIR
    if not root.exists() or not root.is_dir():
        return []

    items: list[tuple[str, str]] = []
    for file_path in sorted(root.rglob("*.toml")):
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(root).with_suffix("")
        command_name = str(relative).replace(os.sep, ":")
        description = _extract_toml_description(file_path)
        items.append((command_name, description))

    if limit is not None:
        return items[:limit]
    return items


async def _sync_telegram_command_menu(app: Application) -> None:
    commands = _refresh_telegram_command_registry()
    builtin_count = len(_build_builtin_gemini_menu())
    dynamic_count = len(GEMINI_TELEGRAM_ALIAS_MAP)
    try:
        await app.bot.delete_my_commands()
    except Exception:
        pass
    await app.bot.set_my_commands(commands)
    logger.info(
        "Synced Telegram command menu with %s commands (%s built-in Gemini + %s dynamic aliases)",
        len(commands),
        builtin_count,
        dynamic_count,
    )


async def _post_init(app: Application) -> None:
    try:
        await app.bot.delete_webhook(drop_pending_updates=False)
        logger.info("Cleared Telegram webhook before polling")
    except Exception as exc:
        logger.warning("Webhook cleanup failed before polling: %s", exc)
    try:
        await _sync_telegram_command_menu(app)
    except Exception as exc:
        logger.exception("Failed to sync Telegram command menu on startup: %s", exc)


def _validate_startup() -> str:
    # First, try to get the token from an environment variable
    token = os.environ.get("BOT_TOKEN")
    if token:
        logger.info("Using BOT_TOKEN from environment variable.")
        pass # Token found, proceed with validation

    # If not found in env, try to read from file
    else:
        token_file = BotConfig.BOT_TOKEN_FILE
        if not os.path.isfile(token_file):
            raise FileNotFoundError(f"Token file not found: {token_file}. Set BOT_TOKEN environment variable or create this file.")

        file_stat = os.stat(token_file)
        if file_stat.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PermissionError(
                f"Token file permissions are too open: {token_file} (expected 600-style)"
            )

        with open(token_file, "r", encoding="utf-8") as f:
            token = f.read().strip()
        if not token:
            raise ValueError(f"Token file is empty: {token_file}")
        logger.info("Using BOT_TOKEN from file: %s", token_file)


    gemini_path = BotConfig.GEMINI_PATH
    if not os.path.isfile(gemini_path):
        raise FileNotFoundError(f"Gemini CLI path not found: {gemini_path}")
    if not os.access(gemini_path, os.X_OK):
        raise PermissionError(f"Gemini CLI path is not executable: {gemini_path}")

    return token


def _is_chat_allowed(chat_id: int) -> bool:
    if not BotConfig.ALLOWED_CHAT_IDS:
        return True
    return chat_id in BotConfig.ALLOWED_CHAT_IDS


def _is_rate_limited(chat_id: int) -> bool:
    now = time.time()
    entries = request_history[chat_id]
    while entries and (now - entries[0]) > BotConfig.RATE_LIMIT_WINDOW_SEC:
        entries.popleft()

    effective_limit = BotConfig.RATE_LIMIT_PER_MIN + BotConfig.RATE_LIMIT_BURST
    if len(entries) >= effective_limit:
        return True

    entries.append(now)
    return False


def _uptime_seconds() -> int:
    return int(time.time() - RuntimeStats.start_time)


def _build_update_key(update: Update) -> str | None:
    if isinstance(update.update_id, int):
        return f"update:{update.update_id}"
    if update.callback_query and update.callback_query.id:
        return f"callback:{update.callback_query.id}"

    message = update.effective_message
    if message and message.chat and isinstance(message.message_id, int):
        return f"message:{message.chat.id}:{message.message_id}"
    return None


async def _is_duplicate_update(update: Update) -> tuple[bool, str | None]:
    key = _build_update_key(update)
    if not key:
        return False, None

    now = time.time()
    ttl = float(BotConfig.UPDATE_DEDUPE_TTL_SEC)
    async with recent_update_lock:
        while recent_update_queue and (now - recent_update_queue[0][1]) > ttl:
            old_key, old_seen_at = recent_update_queue.popleft()
            if recent_update_times.get(old_key) == old_seen_at:
                recent_update_times.pop(old_key, None)

        existing_seen_at = recent_update_times.get(key)
        if existing_seen_at is not None and (now - existing_seen_at) <= ttl:
            return True, key

        recent_update_times[key] = now
        recent_update_queue.append((key, now))
        while len(recent_update_queue) > BotConfig.UPDATE_DEDUPE_MAX:
            old_key, old_seen_at = recent_update_queue.popleft()
            if recent_update_times.get(old_key) == old_seen_at:
                recent_update_times.pop(old_key, None)

    return False, key


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forward a user text message to Gemini CLI and return the response."""
    if not update.message or update.message.text is None:
        return
    user_text = update.message.text.strip()
    force_fresh = _is_greeting_message(user_text)
    await _forward_prompt_to_gemini(
        update,
        user_text,
        chat_mode=True,
        allow_yolo=False,
        force_fresh=force_fresh,
    )


async def _reply_in_chunks(update: Update, text: str) -> None:
    if not update.message:
        return
    max_len = BotConfig.TELEGRAM_MESSAGE_CHUNK_SIZE
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            await update.message.reply_text(remaining)
            break

        split_at = remaining.rfind("\n", 0, max_len + 1)
        if split_at < max_len // 2:
            split_at = remaining.rfind(" ", 0, max_len + 1)
        if split_at <= 0:
            split_at = max_len

        chunk = remaining[:split_at].rstrip()
        if not chunk:
            chunk = remaining[:max_len]
            split_at = len(chunk)

        await update.message.reply_text(chunk)
        remaining = remaining[split_at:].lstrip()


def _is_greeting_message(text: str) -> bool:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "", text.strip().lower())
    return normalized in {
        "hi",
        "hii",
        "hiii",
        "hey",
        "heyy",
        "heyyy",
        "hello",
        "yo",
        "sup",
    }


def _build_telegram_chat_prompt(user_text: str) -> str:
    return (
        "You are in Telegram chat mode.\n"
        "Reply directly to the user's latest message.\n"
        "Do not summarize internal maintenance/tasks unless the user asks.\n"
        "Do not run tools or file edits unless the user explicitly asks for an action.\n"
        "If the message is a greeting or small talk, reply naturally and briefly.\n\n"
        f"User message:\n{user_text}"
    )


def _extract_json_payload(stdout_text: str) -> dict | None:
    """Best-effort parse for headless JSON output."""
    if not stdout_text:
        return None
    raw = stdout_text.strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return None
    except Exception:
        pass

    # Fallback for accidental prefix/suffix noise around JSON.
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        snippet = raw[start : end + 1]
        try:
            parsed = json.loads(snippet)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return None
    return None


def _parse_headless_response(stdout_text: str) -> tuple[str | None, str | None, str | None]:
    """
    Returns (response_text, session_id, error_text).
    Falls back to plain text when JSON payload is not present.
    """
    payload = _extract_json_payload(stdout_text)
    if payload is None:
        text = stdout_text.strip()
        if not text:
            text = "Received empty response from Gemini."
        return text, None, None

    session_id = payload.get("session_id")
    if not isinstance(session_id, str):
        session_id = None

    response = payload.get("response")
    error = payload.get("error")
    if error is not None and response in (None, ""):
        if isinstance(error, dict):
            message = str(error.get("message", "")).strip() or "Gemini returned an error."
        else:
            message = str(error).strip() or "Gemini returned an error."
        return None, session_id, message

    if response is None:
        response_text = ""
    elif isinstance(response, str):
        response_text = response
    else:
        response_text = json.dumps(response, ensure_ascii=False)

    response_text = response_text.strip()
    if not response_text:
        response_text = "Received empty response from Gemini."
    return response_text, session_id, None


def _clean_session_title(value: str) -> str:
    title = re.sub(r"<hook_context>.*?</hook_context>", "", value, flags=re.IGNORECASE)
    title = re.sub(r"<system_note>.*?</system_note>", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+", " ", title).strip()
    if not title:
        title = "(no title)"
    if len(title) > 120:
        title = f"{title[:117]}..."
    return title


def _format_sessions_output(raw_text: str, limit: int | None) -> str:
    lines = raw_text.splitlines()
    total = 0
    header = "Available sessions"
    for line in lines:
        header_match = re.search(r"Available sessions for this project \((\d+)\):", line)
        if header_match:
            total = int(header_match.group(1))
            header = f"Available sessions for this project ({total})"
            break

    items: list[tuple[int, str, str, str]] = []
    for line in lines:
        m = SESSION_LIST_ITEM_RE.match(line)
        if not m:
            continue
        index = int(m.group(1))
        title = _clean_session_title(m.group(2))
        age = m.group(3).strip()
        session_id = m.group(4).strip()
        items.append((index, title, age, session_id))

    if not items:
        return raw_text.strip() or "No sessions found."

    display_limit = len(items) if limit is None else max(1, min(limit, len(items)))
    selected = items[:display_limit]

    out_lines = [header + ":", ""]
    for index, title, age, session_id in selected:
        out_lines.append(f"{index}. {title} ({age}) [{session_id}]")

    if len(items) > display_limit:
        out_lines.extend(
            [
                "",
                f"Showing {display_limit} of {len(items)}.",
                "Use `/sessions all` for full list, or `/sessions <n>`.",
            ]
        )

    return "\n".join(out_lines)


async def _typing_keepalive(chat, stop_event: asyncio.Event) -> None:
    """Keep Telegram typing indicator alive until stop_event is set."""
    while not stop_event.is_set():
        try:
            await chat.send_action(action=ChatAction.TYPING)
        except Exception as exc:
            logger.debug("Typing keepalive failed: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=float(BotConfig.TYPING_REFRESH_SEC))
        except asyncio.TimeoutError:
            continue


async def _progress_status_loop(
    update: Update, stop_event: asyncio.Event, started_at: float
) -> None:
    """Show and refresh a status message while a request is running."""
    if not update.message:
        return

    status_message = None
    last_text = ""

    async def _send_or_edit(text: str) -> None:
        nonlocal status_message, last_text
        if text == last_text:
            return
        try:
            if status_message is None:
                status_message = await update.message.reply_text(text)
            else:
                await status_message.edit_text(text)
            last_text = text
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return
            logger.debug("Progress status update failed: %s", exc)

    try:
        await asyncio.wait_for(stop_event.wait(), timeout=float(BotConfig.LONG_WAIT_NOTIFY_SEC))
        return
    except asyncio.TimeoutError:
        pass

    while not stop_event.is_set():
        elapsed = max(1, int(time.time() - started_at))
        status_text = (
            f"Still working... {elapsed}s elapsed | queued {RuntimeStats.waiting_requests} "
            f"| active {RuntimeStats.inflight_requests}"
        )
        await _send_or_edit(status_text)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=float(BotConfig.PROGRESS_REFRESH_SEC))
        except asyncio.TimeoutError:
            continue

    if status_message is not None:
        try:
            await status_message.delete()
        except Exception as exc:
            logger.debug("Progress status cleanup failed: %s", exc)


def _is_recoverable_gemini_failure(stderr_text: str, returncode: int | None = None) -> bool:
    if returncode is not None and returncode < 0:
        return True
    if not stderr_text:
        return False
    return bool(RECOVERABLE_GEMINI_ERR_RE.search(stderr_text))


def _retry_delay_sec(attempt: int) -> float:
    base_delay = float(BotConfig.GEMINI_RETRY_BASE_SEC)
    max_delay = float(BotConfig.GEMINI_RETRY_MAX_SEC)
    raw_delay = min(max_delay, base_delay * (2 ** max(0, attempt - 1)))
    jitter = raw_delay * 0.2
    return max(0.25, raw_delay + random.uniform(-jitter, jitter))


async def _run_gemini_with_retries(
    user_message: str,
    chat_id: int,
    username: str,
    allow_yolo: bool,
    force_fresh: bool,
    telegram_mode: bool,
):
    attempts = max(1, BotConfig.GEMINI_RETRY_ATTEMPTS)
    last_error_detail = "unknown"

    resume_target = chat_resume_target.get(chat_id)
    now_ts = time.time()
    last_seen = chat_last_activity.get(chat_id, 0.0)
    if resume_target and last_seen > 0 and (now_ts - last_seen) > float(BotConfig.SESSION_IDLE_RESET_SEC):
        logger.info(
            "Clearing stale resume target due idle timeout chat_id=%s user=@%s target=%s idle_sec=%s",
            chat_id,
            username,
            resume_target,
            int(now_ts - last_seen),
        )
        chat_resume_target.pop(chat_id, None)
        resume_target = None
    if force_fresh and resume_target:
        logger.info(
            "Clearing resume target for fresh chat turn chat_id=%s user=@%s target=%s",
            chat_id,
            username,
            resume_target,
        )
        chat_resume_target.pop(chat_id, None)
        resume_target = None

    for attempt in range(1, attempts + 1):
        try:
            command = [BotConfig.GEMINI_PATH, "--output-format", "json"]
            if allow_yolo:
                command.append("-y")
            if resume_target:
                command.extend(["-r", resume_target])
            if BotConfig.GEMINI_MODEL:
                command.extend(["-m", BotConfig.GEMINI_MODEL])
            command.extend(["-p", user_message])
            env = os.environ.copy()
            if telegram_mode:
                env["GEMINI_TELEGRAM_MODE"] = "1"
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except Exception as exc:
            last_error_detail = f"spawn error: {exc}"
            if attempt < attempts:
                delay = _retry_delay_sec(attempt)
                logger.warning(
                    "Gemini spawn failed chat_id=%s user=@%s attempt=%s/%s; retrying in %.1fs (%s)",
                    chat_id,
                    username,
                    attempt,
                    attempts,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
                continue
            return None, "error", last_error_detail

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=float(BotConfig.REQUEST_TIMEOUT_SEC),
            )
        except asyncio.TimeoutError:
            RuntimeStats.timeout_count += 1
            process.kill()
            await process.communicate()
            last_error_detail = "timeout"
            if attempt < attempts:
                delay = _retry_delay_sec(attempt)
                logger.warning(
                    "Gemini timeout chat_id=%s user=@%s attempt=%s/%s; retrying in %.1fs",
                    chat_id,
                    username,
                    attempt,
                    attempts,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            return None, "timeout", last_error_detail

        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            stderr_snippet = stderr_text[:500]
            last_error_detail = f"rc={process.returncode} stderr={stderr_snippet!r}"
            can_retry = (
                attempt < attempts
                and _is_recoverable_gemini_failure(stderr_text, returncode=process.returncode)
            )
            if can_retry:
                delay = _retry_delay_sec(attempt)
                logger.warning(
                    "Gemini returned recoverable failure chat_id=%s user=@%s attempt=%s/%s; "
                    "retrying in %.1fs rc=%s stderr=%r",
                    chat_id,
                    username,
                    attempt,
                    attempts,
                    delay,
                    process.returncode,
                    stderr_snippet,
                )
                await asyncio.sleep(delay)
                continue
            return None, "error", last_error_detail

        stdout_text = stdout.decode("utf-8", errors="replace")
        response, session_id, parse_error = _parse_headless_response(stdout_text)
        if parse_error:
            last_error_detail = parse_error
            can_retry = attempt < attempts and _is_recoverable_gemini_failure(parse_error)
            if can_retry:
                delay = _retry_delay_sec(attempt)
                logger.warning(
                    "Gemini headless parse/error chat_id=%s user=@%s attempt=%s/%s; "
                    "retrying in %.1fs error=%r",
                    chat_id,
                    username,
                    attempt,
                    attempts,
                    delay,
                    parse_error[:500],
                )
                await asyncio.sleep(delay)
                continue
            return None, "error", parse_error

        if session_id:
            previous = chat_resume_target.get(chat_id)
            chat_resume_target[chat_id] = session_id
            if previous != session_id:
                logger.info(
                    "Pinned chat resume target chat_id=%s user=@%s session_id=%s",
                    chat_id,
                    username,
                    session_id,
                )
        chat_last_activity[chat_id] = time.time()
        return response, "ok", ""

    return None, "error", last_error_detail


def _parse_cli_chain(raw: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"\s*(?:;;|&&|\n+)\s*", raw) if part.strip()]
    if not parts:
        return []
    return parts


def _validate_fast_cli_tokens(tokens: list[str]) -> tuple[bool, str]:
    if not tokens:
        return False, "empty command"
    if any(token in CLI_DENY_FLAGS for token in tokens):
        return False, "prompt/model flags are blocked in /cli fast mode; use /g for model prompts"

    first = tokens[0]
    if first.startswith("-"):
        if first in CLI_FAST_GLOBAL_FLAGS:
            return True, ""
        return False, f"unsupported global flag in /cli fast mode: {first}"

    if first in CLI_FAST_TOPLEVEL:
        return True, ""
    return (
        False,
        "unsupported fast command; allowed: gemini mcp/skills/extensions/hooks, --list-sessions, --help, --version",
    )


async def _run_fast_cli_tokens(tokens: list[str]) -> tuple[int | None, str, str, bool]:
    process = await asyncio.create_subprocess_exec(
        BotConfig.GEMINI_PATH,
        *tokens,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=float(BotConfig.CLI_TIMEOUT_SEC),
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return None, "", "", True

    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    stderr_text = stderr.decode("utf-8", errors="replace").strip()
    return process.returncode, stdout_text, stderr_text, False


def _render_fast_cli_result(index: int, total: int, command_text: str, rc: int | None, stdout: str, stderr: str, timed_out: bool) -> str:
    max_chars = 3200
    prefix = f"[{index}/{total}] {command_text}"
    if timed_out:
        return f"{prefix}\nStatus: timeout after {BotConfig.CLI_TIMEOUT_SEC}s"
    if rc is None:
        return f"{prefix}\nStatus: failed (no exit code)"
    sections = [prefix, f"Exit: {rc}"]
    if stdout:
        sections.append(f"stdout:\n{stdout[:max_chars]}")
    if stderr:
        sections.append(f"stderr:\n{stderr[:max_chars]}")
    if not stdout and not stderr:
        sections.append("No output.")
    return "\n".join(sections)


async def _run_gemini_management_command(args: list[str], timeout_sec: int) -> tuple[int | None, str, str, bool]:
    process = await asyncio.create_subprocess_exec(
        BotConfig.GEMINI_PATH,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=float(timeout_sec),
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return None, "", "", True
    return (
        process.returncode,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
        False,
    )


async def _run_oneoff_prompt_with_resume(
    update: Update, prompt: str, resume_target: str
) -> None:
    if not update.message:
        return

    chat_id = update.effective_chat.id if update.effective_chat else 0
    previous = chat_resume_target.get(chat_id)
    chat_resume_target[chat_id] = resume_target
    try:
        await _forward_prompt_to_gemini(update, prompt)
    finally:
        if previous is not None:
            chat_resume_target[chat_id] = previous
        else:
            chat_resume_target.pop(chat_id, None)


async def _forward_prompt_to_gemini(
    update: Update,
    user_message: str,
    *,
    chat_mode: bool = False,
    allow_yolo: bool = True,
    force_fresh: bool = False,
) -> None:
    if not update.message:
        return

    chat_id = update.effective_chat.id if update.effective_chat else 0
    user = update.effective_user
    username = user.username if user and user.username else f"user_{user.id if user else 0}"
    message_length = len(user_message)

    if not _is_chat_allowed(chat_id):
        logger.warning("Rejected message from non-allowlisted chat_id=%s", chat_id)
        await update.message.reply_text("This bot is not enabled for this chat.")
        return

    if not user_message:
        await update.message.reply_text("Please send a non-empty message.")
        return

    if message_length > BotConfig.MAX_INPUT_CHARS:
        await update.message.reply_text(
            f"Message too long ({message_length} chars). Limit is {BotConfig.MAX_INPUT_CHARS}."
        )
        return

    is_duplicate, update_key = await _is_duplicate_update(update)
    if is_duplicate:
        RuntimeStats.duplicate_updates += 1
        logger.info(
            "Skipped duplicate Telegram update key=%s chat_id=%s user=@%s",
            update_key,
            chat_id,
            username,
        )
        return

    if _is_rate_limited(chat_id):
        await update.message.reply_text("Rate limit reached. Please try again in about a minute.")
        return

    prompt_for_model = _build_telegram_chat_prompt(user_message) if chat_mode else user_message

    logger.info(
        "Accepted prompt chat_id=%s user=@%s len=%s chat_mode=%s yolo=%s fresh=%s",
        chat_id,
        username,
        message_length,
        chat_mode,
        allow_yolo,
        force_fresh,
    )
    stop_event = asyncio.Event()
    started_at = time.time()
    typing_task = asyncio.create_task(_typing_keepalive(update.message.chat, stop_event))
    progress_task = asyncio.create_task(_progress_status_loop(update, stop_event, started_at))

    acquired = False
    RuntimeStats.waiting_requests += 1
    try:
        async with process_semaphore:
            acquired = True
            RuntimeStats.waiting_requests = max(0, RuntimeStats.waiting_requests - 1)
            RuntimeStats.inflight_requests += 1

            response, status, detail = await _run_gemini_with_retries(
                user_message=prompt_for_model,
                chat_id=chat_id,
                username=username,
                allow_yolo=allow_yolo,
                force_fresh=force_fresh,
                telegram_mode=chat_mode,
            )

            if status == "timeout":
                await update.message.reply_text("Timeout: Gemini took too long to respond.")
                logger.warning("Gemini timeout chat_id=%s user=@%s", chat_id, username)
                return

            if status != "ok" or response is None:
                RuntimeStats.error_count += 1
                logger.error(
                    "Gemini subprocess failed chat_id=%s user=@%s detail=%s",
                    chat_id,
                    username,
                    detail,
                )
                await update.message.reply_text("Gemini request failed. Please try again shortly.")
                return

            await _reply_in_chunks(update, response)
            RuntimeStats.processed_count += 1
            logger.info("Response sent chat_id=%s user=@%s", chat_id, username)
    except Exception as exc:
        RuntimeStats.error_count += 1
        logger.exception("Unhandled error chat_id=%s user=@%s: %s", chat_id, username, exc)
        await update.message.reply_text("Internal bot error. Please try again.")
    finally:
        stop_event.set()
        for task in (typing_task, progress_task):
            try:
                await task
            except Exception:
                pass
        if not acquired:
            RuntimeStats.waiting_requests = max(0, RuntimeStats.waiting_requests - 1)
        if acquired:
            RuntimeStats.inflight_requests = max(0, RuntimeStats.inflight_requests - 1)


async def handle_cli(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run fast Gemini CLI management commands without model prompting."""
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id if update.effective_chat else 0
    if not _is_chat_allowed(chat_id):
        await update.message.reply_text("This bot is not enabled for this chat.")
        return

    is_duplicate, update_key = await _is_duplicate_update(update)
    if is_duplicate:
        RuntimeStats.duplicate_updates += 1
        logger.info("Skipped duplicate /cli update key=%s chat_id=%s", update_key, chat_id)
        return

    if _is_rate_limited(chat_id):
        await update.message.reply_text("Rate limit reached. Please try again in about a minute.")
        return

    parts = update.message.text.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            "Usage: /cli <command>\n"
            "Chain multiple commands: /cli mcp list && skills list\n"
            "Example: /cli --list-sessions"
        )
        return

    chain = _parse_cli_chain(parts[1].strip())
    if not chain:
        await update.message.reply_text("No commands found. Provide at least one /cli command.")
        return
    if len(chain) > BotConfig.CLI_CHAIN_MAX:
        await update.message.reply_text(
            f"Too many chained commands ({len(chain)}). Max is {BotConfig.CLI_CHAIN_MAX}."
        )
        return

    user = update.effective_user
    username = user.username if user and user.username else f"user_{user.id if user else 0}"
    stop_event = asyncio.Event()
    started_at = time.time()
    typing_task = asyncio.create_task(_typing_keepalive(update.message.chat, stop_event))
    progress_task = asyncio.create_task(_progress_status_loop(update, stop_event, started_at))
    acquired = False
    RuntimeStats.waiting_requests += 1
    try:
        async with process_semaphore:
            acquired = True
            RuntimeStats.waiting_requests = max(0, RuntimeStats.waiting_requests - 1)
            RuntimeStats.inflight_requests += 1

            results: list[str] = []
            for index, raw_command in enumerate(chain, start=1):
                try:
                    tokens = shlex.split(raw_command)
                except ValueError as exc:
                    results.append(f"[{index}/{len(chain)}] {raw_command}\nParse error: {exc}")
                    continue
                allowed, reason = _validate_fast_cli_tokens(tokens)
                if not allowed:
                    results.append(f"[{index}/{len(chain)}] {raw_command}\nBlocked: {reason}")
                    continue

                rc, stdout_text, stderr_text, timed_out = await _run_fast_cli_tokens(tokens)
                results.append(
                    _render_fast_cli_result(
                        index=index,
                        total=len(chain),
                        command_text=raw_command,
                        rc=rc,
                        stdout=stdout_text,
                        stderr=stderr_text,
                        timed_out=timed_out,
                    )
                )

            RuntimeStats.processed_count += 1
            await _reply_in_chunks(update, "\n\n".join(results))
            logger.info("CLI chain complete chat_id=%s user=@%s commands=%s", chat_id, username, len(chain))
    except Exception as exc:
        RuntimeStats.error_count += 1
        logger.exception("Unhandled /cli error chat_id=%s user=@%s: %s", chat_id, username, exc)
        await update.message.reply_text("Internal bot error while running /cli. Please try again.")
    finally:
        stop_event.set()
        for task in (typing_task, progress_task):
            try:
                await task
            except Exception:
                pass
        if not acquired:
            RuntimeStats.waiting_requests = max(0, RuntimeStats.waiting_requests - 1)
        if acquired:
            RuntimeStats.inflight_requests = max(0, RuntimeStats.inflight_requests - 1)


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    if not update.message:
        return
    chat_id = update.effective_chat.id if update.effective_chat else 0
    if not _is_chat_allowed(chat_id):
        await update.message.reply_text("This bot is not enabled for this chat.")
        return
    await update.message.reply_text(
        "Hi. I am your Gemini CLI Telegram assistant.\n\n"
        "Commands:\n"
        "/start - Show this message\n"
        "/help - Show help\n"
        "/status - Quick online check\n"
        "/health - Runtime health details\n"
        "/stats - Alias for /health\n"
        "/sessions [all|n] - List Gemini sessions\n"
        "/sessionuse <target> - Set chat resume target\n"
        "/sessionclear - Clear chat resume target\n"
        "/sessiondelete <target> - Delete Gemini session\n"
        "/continue <prompt> - One-off resume from latest\n"
        "/g <prompt> - Run Gemini with exact prompt text\n"
        "/cli <cmd> - Fast no-model Gemini CLI command chain\n"
        "/gcmds - List available Gemini custom slash commands\n"
        "/synccommands - Refresh Telegram command menu\n\n"
        "Gemini slash commands are exported in Telegram menu (for example /memory, /model, /mcp)."
    )


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    if not update.message:
        return
    chat_id = update.effective_chat.id if update.effective_chat else 0
    if not _is_chat_allowed(chat_id):
        await update.message.reply_text("This bot is not enabled for this chat.")
        return
    await update.message.reply_text(
        "Gemini Bot Help\n\n"
        "Send any normal text message and I will run Gemini CLI with it.\n"
        "Use /g <prompt> when you want to force slash-heavy Gemini prompts, for example:\n"
        "/g /memory show\n"
        "/g /oc:status\n\n"
        "Session/history workflow:\n"
        "/sessions\n"
        "/sessions 15\n"
        "/sessions all\n"
        "/sessionuse latest\n"
        "/sessionuse 3\n"
        "/sessionclear\n"
        "/continue Continue with next step\n"
        "/sessiondelete 3\n\n"
        "Fast CLI mode (no model prompting):\n"
        "Direct commands: /mcp list, /skills list, /--list-sessions\n"
        "Chain multiple commands: /cli mcp list && skills list\n"
        "Example: /cli --list-sessions\n\n"
        "Built-in protections:\n"
        f"- Timeout: {BotConfig.REQUEST_TIMEOUT_SEC}s\n"
        f"- Input limit: {BotConfig.MAX_INPUT_CHARS} chars\n"
        f"- Rate limit: {BotConfig.RATE_LIMIT_PER_MIN}+{BotConfig.RATE_LIMIT_BURST} per {BotConfig.RATE_LIMIT_WINDOW_SEC}s"
    )


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command."""
    if not update.message:
        return
    chat_id = update.effective_chat.id if update.effective_chat else 0
    if not _is_chat_allowed(chat_id):
        await update.message.reply_text("This bot is not enabled for this chat.")
        return
    await update.message.reply_text("Bot is online and ready.")


async def handle_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List available Gemini sessions."""
    if not update.message:
        return
    chat_id = update.effective_chat.id if update.effective_chat else 0
    if not _is_chat_allowed(chat_id):
        await update.message.reply_text("This bot is not enabled for this chat.")
        return
    if _is_rate_limited(chat_id):
        await update.message.reply_text("Rate limit reached. Please try again in about a minute.")
        return

    raw = update.message.text.strip()
    parts = raw.split(maxsplit=1)
    requested_limit: int | None = BotConfig.SESSIONS_LIST_DEFAULT_LIMIT
    if len(parts) > 1 and parts[1].strip():
        arg = parts[1].strip().lower()
        if arg == "all":
            requested_limit = None
        else:
            try:
                value = int(arg)
                requested_limit = max(1, min(value, BotConfig.SESSIONS_LIST_MAX_LIMIT))
            except ValueError:
                await update.message.reply_text(
                    "Usage: /sessions [all|<count>]\n"
                    f"Default count: {BotConfig.SESSIONS_LIST_DEFAULT_LIMIT}"
                )
                return

    try:
        rc, stdout_text, stderr_text, timed_out = await _run_gemini_management_command(
            ["--list-sessions"],
            timeout_sec=BotConfig.CLI_TIMEOUT_SEC,
        )
    except Exception as exc:
        logger.exception("Failed to list sessions: %s", exc)
        await update.message.reply_text("Failed to list sessions.")
        return

    if timed_out:
        await update.message.reply_text(
            f"Session listing timed out after {BotConfig.CLI_TIMEOUT_SEC}s."
        )
        return
    if rc != 0:
        error_text = stderr_text[:1000] if stderr_text else "unknown error"
        await _reply_in_chunks(update, f"Failed to list sessions (rc={rc}).\n{error_text}")
        return

    output = _format_sessions_output(stdout_text, limit=requested_limit)
    await _reply_in_chunks(update, output)


async def handle_sessionuse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set sticky resume target for current Telegram chat."""
    if not update.message:
        return
    chat_id = update.effective_chat.id if update.effective_chat else 0
    if not _is_chat_allowed(chat_id):
        await update.message.reply_text("This bot is not enabled for this chat.")
        return

    raw = update.message.text.strip()
    parts = raw.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        current = chat_resume_target.get(chat_id, "not set")
        await update.message.reply_text(
            "Usage: /sessionuse <latest|index|session_id>\n"
            f"Current resume target: {current}"
        )
        return

    target = parts[1].strip()
    chat_resume_target[chat_id] = target
    await update.message.reply_text(
        f"Resume target set for this chat: {target}\n"
        "All /g and normal messages will continue this session."
    )


async def handle_sessionclear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear sticky resume target for current Telegram chat."""
    if not update.message:
        return
    chat_id = update.effective_chat.id if update.effective_chat else 0
    if not _is_chat_allowed(chat_id):
        await update.message.reply_text("This bot is not enabled for this chat.")
        return

    previous = chat_resume_target.pop(chat_id, None)
    if previous:
        await update.message.reply_text(f"Cleared resume target: {previous}")
    else:
        await update.message.reply_text("No resume target was set for this chat.")


async def handle_sessiondelete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete Gemini session by index or id."""
    if not update.message:
        return
    chat_id = update.effective_chat.id if update.effective_chat else 0
    if not _is_chat_allowed(chat_id):
        await update.message.reply_text("This bot is not enabled for this chat.")
        return
    if _is_rate_limited(chat_id):
        await update.message.reply_text("Rate limit reached. Please try again in about a minute.")
        return

    raw = update.message.text.strip()
    parts = raw.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text("Usage: /sessiondelete <index|session_id>")
        return

    target = parts[1].strip()
    try:
        rc, stdout_text, stderr_text, timed_out = await _run_gemini_management_command(
            ["--delete-session", target],
            timeout_sec=BotConfig.CLI_TIMEOUT_SEC,
        )
    except Exception as exc:
        logger.exception("Failed to delete session: %s", exc)
        await update.message.reply_text("Failed to delete session.")
        return

    if timed_out:
        await update.message.reply_text(
            f"Session delete timed out after {BotConfig.CLI_TIMEOUT_SEC}s."
        )
        return
    if rc != 0:
        error_text = stderr_text[:1000] if stderr_text else "unknown error"
        await _reply_in_chunks(update, f"Failed to delete session (rc={rc}).\n{error_text}")
        return

    current = chat_resume_target.get(chat_id)
    if current == target:
        chat_resume_target.pop(chat_id, None)
    output = stdout_text or f"Deleted session: {target}"
    await _reply_in_chunks(update, output)


async def handle_continue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Continue from latest session for one message."""
    if not update.message:
        return
    chat_id = update.effective_chat.id if update.effective_chat else 0
    if not _is_chat_allowed(chat_id):
        await update.message.reply_text("This bot is not enabled for this chat.")
        return

    raw = update.message.text.strip()
    parts = raw.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            "Usage: /continue <prompt>\n"
            "Example: /continue Continue with the next fix step."
        )
        return
    await _run_oneoff_prompt_with_resume(update, parts[1].strip(), "latest")


async def handle_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /health command."""
    if not update.message:
        return
    chat_id = update.effective_chat.id if update.effective_chat else 0
    if not _is_chat_allowed(chat_id):
        await update.message.reply_text("This bot is not enabled for this chat.")
        return

    available_slots = getattr(process_semaphore, "_value", "n/a")
    lines = [
        "Health check",
        f"uptime_sec: {_uptime_seconds()}",
        f"inflight_requests: {RuntimeStats.inflight_requests}",
        f"queued_requests: {RuntimeStats.waiting_requests}",
        f"processed_count: {RuntimeStats.processed_count}",
        f"timeout_count: {RuntimeStats.timeout_count}",
        f"error_count: {RuntimeStats.error_count}",
        f"duplicate_updates: {RuntimeStats.duplicate_updates}",
        f"concurrency_limit: {BotConfig.MAX_CONCURRENT_PROCESSES}",
        f"available_slots: {available_slots}",
        f"rate_limit: {BotConfig.RATE_LIMIT_PER_MIN}+{BotConfig.RATE_LIMIT_BURST}/{BotConfig.RATE_LIMIT_WINDOW_SEC}s",
        f"allowlist_enabled: {bool(BotConfig.ALLOWED_CHAT_IDS)}",
        f"gemini_model: {BotConfig.GEMINI_MODEL or 'default'}",
        f"chat_resume_target: {chat_resume_target.get(chat_id, 'not set')}",
    ]
    await update.message.reply_text("\n".join(lines))


async def handle_g(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /g <prompt> for explicit Gemini forwarding."""
    if not update.message:
        return

    raw = update.message.text.strip()
    parts = raw.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            "Usage: /g <prompt>\n"
            "Example: /g /memory show\n"
            "Example: /g /oc:run fix nginx config drift"
        )
        return
    await _forward_prompt_to_gemini(update, parts[1].strip())


async def handle_gcmds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show Gemini custom slash commands discovered in ~/.gemini/commands."""
    if not update.message:
        return
    chat_id = update.effective_chat.id if update.effective_chat else 0
    if not _is_chat_allowed(chat_id):
        await update.message.reply_text("This bot is not enabled for this chat.")
        return

    commands = _discover_gemini_commands(limit=BotConfig.GEMINI_COMMANDS_PREVIEW_LIMIT)
    if not commands:
        await update.message.reply_text(
            "No Gemini custom commands found in ~/.gemini/commands yet."
        )
        return

    lines = [
        "Gemini custom slash commands",
        "",
        "Use these through /g or exported Telegram aliases.",
        "",
    ]
    for name, desc in commands:
        original = f"/{name}"
        alias = next(
            (
                alias_name
                for alias_name, target in GEMINI_TELEGRAM_ALIAS_MAP.items()
                if target == original
            ),
            "",
        )
        if alias:
            lines.append(f"{original} (alias {alias}) - {desc}")
        else:
            lines.append(f"{original} - {desc}")
    await _reply_in_chunks(update, "\n".join(lines))


async def handle_synccommands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-sync Telegram command menu."""
    if not update.message:
        return
    chat_id = update.effective_chat.id if update.effective_chat else 0
    if not _is_chat_allowed(chat_id):
        await update.message.reply_text("This bot is not enabled for this chat.")
        return

    try:
        if context.application is None:
            raise RuntimeError("Application context unavailable")
        await _sync_telegram_command_menu(context.application)
        await update.message.reply_text("Telegram command menu synced.")
    except Exception as exc:
        logger.exception("Failed to sync command menu via /synccommands: %s", exc)
        await update.message.reply_text("Failed to sync command menu. Check logs.")


async def handle_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Forwards unknown slash commands.
    If it matches a fast CLI pattern, routes it to handle_cli.
    Otherwise, forwards to Gemini as a raw prompt.
    """
    if not update.message or not update.message.text:
        return

    raw = update.message.text.strip()
    if not raw.startswith("/"):
        return

    # Extract command part (e.g., "/mcp" from "/mcp list")
    command_parts = raw.split(maxsplit=1)
    command_token = command_parts[0]
    base_command_name = command_token.split("@", 1)[0].lower() # e.g. "/mcp"

    # If it's one of the bot's own built-in commands, let the other handlers deal with it
    if base_command_name in LOCAL_COMMAND_NAMES:
        return

    # Try to resolve dynamic alias first
    mapped = GEMINI_TELEGRAM_ALIAS_MAP.get(base_command_name)
    if mapped:
        remainder = raw[len(command_token):].strip()
        mapped_prompt = f"{mapped} {remainder}".strip()
        await _forward_prompt_to_gemini(update, mapped_prompt)
        return

    # Check if it's a direct fast CLI command (e.g., /mcp list, /--list-sessions)
    try:
        # Use shlex to correctly split the command and its arguments
        tokens = shlex.split(raw[1:])  # Remove leading '/'
        # Only consider it a fast CLI command if it's a single command execution,
        # not a chain, as /cli is for chains, and it contains valid fast tokens.
        if tokens:
            is_fast_cli_command, _ = _validate_fast_cli_tokens(tokens)
            if is_fast_cli_command and not any(part in tokens for part in [";;", "&&"]):
                # Construct a message suitable for handle_cli
                # Temporarily modify the update.message.text to be in the /cli format
                original_text = update.message.text
                try:
                    update.message.text = f"/cli {raw[1:]}"  # Remove leading '/'
                    logger.info("Routing unknown command %r to /cli handler", original_text)
                    await handle_cli(update, context)
                finally:
                    update.message.text = original_text
                return
    except ValueError as exc:
        logger.debug("Failed to parse unknown command for fast CLI check: %s", exc)
        # Fall through to Gemini prompt if parsing fails or not a fast CLI command

    # Default: forward to Gemini as a prompt
    await _forward_prompt_to_gemini(update, raw)


def main():
    """Start the bot."""
    logger.info("Starting Telegram Gemini bot...")
    bot_token = _validate_startup()
    logger.info(
        "Config timeout=%ss concurrency=%s max_input=%s rate=%s+%s/%ss allowlist=%s model=%s",
        BotConfig.REQUEST_TIMEOUT_SEC,
        BotConfig.MAX_CONCURRENT_PROCESSES,
        BotConfig.MAX_INPUT_CHARS,
        BotConfig.RATE_LIMIT_PER_MIN,
        BotConfig.RATE_LIMIT_BURST,
        BotConfig.RATE_LIMIT_WINDOW_SEC,
        bool(BotConfig.ALLOWED_CHAT_IDS),
        BotConfig.GEMINI_MODEL or "default",
    )

    app = Application.builder().token(bot_token).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("help", handle_help))
    app.add_handler(CommandHandler("status", handle_status))
    app.add_handler(CommandHandler("health", handle_health))
    app.add_handler(CommandHandler("stats", handle_health))
    app.add_handler(CommandHandler("sessions", handle_sessions))
    app.add_handler(CommandHandler("sessionuse", handle_sessionuse))
    app.add_handler(CommandHandler("sessionclear", handle_sessionclear))
    app.add_handler(CommandHandler("sessiondelete", handle_sessiondelete))
    app.add_handler(CommandHandler("continue", handle_continue))
    app.add_handler(CommandHandler("g", handle_g))
    app.add_handler(CommandHandler("cli", handle_cli))
    app.add_handler(CommandHandler("gcmds", handle_gcmds))
    app.add_handler(CommandHandler("synccommands", handle_synccommands))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.COMMAND, handle_unknown_command))

    logger.info("Bot started. Polling for messages. Unknown slash commands may be routed to CLI or Gemini.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
        bootstrap_retries=-1,
    )


if __name__ == "__main__":
    main()
