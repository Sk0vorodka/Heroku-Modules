"""
Пересылка сообщений из MAX в Telegram.

Зависимость:
    pip install aiohttp
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import aiohttp


logger = logging.getLogger(__name__)


CONFIG_FILE = Path("max_to_telegram.json")
SEEN_FILE = Path("max_to_telegram_seen.json")


DEFAULT_CONFIG = {
    "max_chat_id": "",
    "telegram": {
        "bot_token": "",
        "chat_id": "",
        "message_thread_id": None
    },
    "ignore_own_messages": True,
    "include_sender": True,
    "max_message_length": 4096
}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default

    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        logger.exception("Не удалось прочитать %s", path)
        return default


def save_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")

    with temporary.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)

    temporary.replace(path)


def get_value(obj: Any, *names: str, default: Any = None) -> Any:
    """
    Универсальное получение атрибута из dict или объекта.

    Это позволяет работать с разными версиями моделей сообщений Maxli.
    """
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]

        value = getattr(obj, name, None)
        if value is not None:
            return value

    return default


class MaxToTelegram:
    def __init__(self, config: dict[str, Any]):
        self.config = config

        self.max_chat_id = str(config.get("max_chat_id", "")).strip()

        telegram_config = config.get("telegram", {})
        self.bot_token = (
            os.getenv("MAXLI_TELEGRAM_BOT_TOKEN")
            or str(telegram_config.get("bot_token", "")).strip()
        )
        self.telegram_chat_id = str(
            telegram_config.get("chat_id", "")
        ).strip()

        self.message_thread_id = telegram_config.get("message_thread_id")

        self.ignore_own_messages = bool(
            config.get("ignore_own_messages", True)
        )
        self.include_sender = bool(
            config.get("include_sender", True)
        )
        self.max_message_length = int(
            config.get("max_message_length", 4096)
        )

        self.seen_ids: set[str] = set(load_json(SEEN_FILE, []))
        self.session: aiohttp.ClientSession | None = None

        self._validate_config()

    def _validate_config(self) -> None:
        missing = []

        if not self.max_chat_id:
            missing.append("max_chat_id")

        if not self.bot_token:
            missing.append("telegram.bot_token или переменная MAXLI_TELEGRAM_BOT_TOKEN")

        if not self.telegram_chat_id:
            missing.append("telegram.chat_id")

        if missing:
            raise RuntimeError(
                "Не заполнены параметры: " + ", ".join(missing)
            )

    async def start(self) -> None:
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    def _message_id(self, message: Any) -> str:
        message_id = get_value(
            message,
            "id",
            "message_id",
            "msg_id",
            default=None
        )

        if message_id is not None:
            return str(message_id)

        raw = repr(message).encode("utf-8", errors="replace")
        return hashlib.sha256(raw).hexdigest()

    def _get_chat_id(self, message: Any) -> str:
        chat_id = get_value(
            message,
            "chat_id",
            "conversation_id",
            "dialog_id",
            default=None
        )

        if chat_id is not None:
            return str(chat_id)

        chat = get_value(message, "chat", "conversation", "dialog", default=None)

        return str(
            get_value(
                chat,
                "id",
                "chat_id",
                "conversation_id",
                default=""
            )
        )

    def _is_own_message(self, message: Any) -> bool:
        value = get_value(
            message,
            "is_outgoing",
            "outgoing",
            "is_from_me",
            default=False
        )
        return bool(value)

    def _get_sender_name(self, message: Any) -> str:
        sender = get_value(
            message,
            "sender",
            "author",
            "from_user",
            "user",
            default=None
        )

        if sender is None:
            return ""

        name = get_value(sender, "name", "display_name", default=None)

        if name:
            return str(name)

        first_name = get_value(sender, "first_name", default="")
        last_name = get_value(sender, "last_name", default="")

        return f"{first_name} {last_name}".strip()

    def _get_text(self, message: Any) -> str:
        text = get_value(
            message,
            "text",
            "body",
            "message",
            "caption",
            default=""
        )

        if text is None:
            return ""

        return str(text).strip()

    def _format_text(self, message: Any) -> str:
        text = self._get_text(message)

        if not text:
            text = "[сообщение без текста]"

        if self.include_sender:
            sender_name = self._get_sender_name(message)

            if sender_name:
                text = (
                    f"<b>{html.escape(sender_name)}</b>\n"
                    f"{html.escape(text)}"
                )
            else:
                text = html.escape(text)
        else:
            text = html.escape(text)

        # Telegram sendMessage допускает максимум 4096 символов.
        if len(text) > self.max_message_length:
            text = (
                text[: self.max_message_length - 40]
                + "\n\n<i>Сообщение обрезано</i>"
            )

        return text

    async def _telegram_request(
        self,
        method: str,
        payload: dict[str, Any]
    ) -> dict[str, Any]:
        await self.start()

        assert self.session is not None

        url = (
            f"https://api.telegram.org/bot"
            f"{self.bot_token}/{method}"
        )

        async with self.session.post(url, json=payload) as response:
            data = await response.json(content_type=None)

            if response.status != 200 or not data.get("ok"):
                raise RuntimeError(
                    f"Telegram API error {response.status}: {data}"
                )

            return data

    async def send_to_telegram(self, message: Any) -> None:
        text = self._format_text(message)

        payload: dict[str, Any] = {
            "chat_id": self.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }

        # Если задан ID темы форума, сообщение будет отправлено в неё.
        if self.message_thread_id not in (None, "", 0, "0"):
            payload["message_thread_id"] = int(self.message_thread_id)

        await self._telegram_request("sendMessage", payload)

    async def handle_message(self, message: Any) -> None:
        """
        Основной обработчик нового сообщения MAX.
        """
        message_chat_id = self._get_chat_id(message)

        if message_chat_id != self.max_chat_id:
            return

        if self.ignore_own_messages and self._is_own_message(message):
            return

        message_id = self._message_id(message)

        if message_id in self.seen_ids:
            return

        try:
            await self.send_to_telegram(message)

            self.seen_ids.add(message_id)

            # Оставляем только последние 5000 ID.
            self.seen_ids = set(list(self.seen_ids)[-5000:])
            save_json(SEEN_FILE, sorted(self.seen_ids))

            logger.info(
                "Сообщение %s из MAX переслано в Telegram",
                message_id
            )

        except Exception:
            logger.exception(
                "Не удалось переслать сообщение %s",
                message_id
            )


_instance: MaxToTelegram | None = None


async def on_message(message: Any) -> None:
    """
    Функция, которую нужно подключить к событию нового сообщения Maxli.
    """
    global _instance

    if _instance is None:
        config = load_json(CONFIG_FILE, DEFAULT_CONFIG)
        _instance = MaxToTelegram(config)
        await _instance.start()

    await _instance.handle_message(message)


async def shutdown() -> None:
    global _instance

    if _instance is not None:
        await _instance.close()
        _instance = None