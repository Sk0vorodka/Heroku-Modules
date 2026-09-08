# modules/max2tg.py
"""
Max2Tg — мост сообщений Max → Telegram Bot API.

Конфиг:
  max_chat_ids        — список ID чатов Max (int/str), пустой = все
  tg_bot_token        — токен бота @BotFather
  tg_chat_id          — ID чата/канала/группы Telegram (можно @username)
  tg_thread_id        — ID ветки форума (message_thread_id), 0 = без ветки
  prefix_enabled      — добавлять шапку с автором
  ignore_self         — не форвардить свои сообщения
  ignore_bots         — не форвардить ботов Max
  include_service     — форвардить сервисные (join/leave/pin)
"""

from __future__ import annotations

import asyncio
import html
import io
import logging
import mimetypes
from typing import Any, Iterable, Optional

import aiohttp

from .. import loader, utils  # Maxli: from maxli import loader, utils

logger = logging.getLogger(__name__)

TG_API = "https://api.telegram.org/bot{token}/{method}"
ALBUM_FLUSH_DELAY = 0.85
MAX_CAPTION = 1024
MAX_TEXT = 4096


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _norm_id(value: Any) -> str:
    return str(value).strip() if value is not None else ""


class Max2Tg(loader.Module):
    """Пересылка сообщений из Max в Telegram через Bot API."""

    strings = {
        "name": "Max2Tg",
        "cfg_ok": "✅ Конфиг сохранён.",
        "need_token": "❌ Укажи токен бота: <code>.m2tg token 123:ABC</code>",
        "need_chat": "❌ Укажи Telegram-чат: <code>.m2tg chat -100...</code>",
        "status": (
            "<b>Max2Tg</b>\n"
            "• enabled: <code>{enabled}</code>\n"
            "• max chats: <code>{max_chats}</code>\n"
            "• tg chat: <code>{tg_chat}</code>\n"
            "• thread: <code>{thread}</code>\n"
            "• token: <code>{token}</code>\n"
            "• prefix: <code>{prefix}</code>\n"
            "• ignore self/bots: <code>{ign_self}</code>/<code>{ign_bots}</code>"
        ),
        "test_ok": "✅