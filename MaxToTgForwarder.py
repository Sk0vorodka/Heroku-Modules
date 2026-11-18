# meta developer: @claude
# meta pic: https://em-content.zobj.net/source/microsoft-teams/337/incoming-envelope_1f4e8.png
# requires: aiohttp
# min-maxli: 100

"""
MaxToTgForwarder — модуль для юзербота Maxli (https://github.com/YouRooni/Maxli)

Слушает выбранный чат в MAX и пересылает новые сообщения в Telegram через
Bot API: текст — как обычное сообщение (имя отправителя жирным, текст
цитатой), медиа — соответствующим методом. Несколько фото/видео в одном
сообщении уходят альбомом, длинный текст режется на части, а не обрезается.

Настройка (после .loadmod):
  .config MaxToTgForwarder source_chat_id  <ID чата MAX>
  .config MaxToTgForwarder bot_token       <токен от @BotFather>
  .config MaxToTgForwarder target_chat_id  <ID чата Telegram>
  .config MaxToTgForwarder thread_id       <ID ветки, необязательно>
  .config MaxToTgForwarder proxy_url       <http://user:pass@ip:port или
                                             socks5://user:pass@ip:port>

Как узнать ID:
  - ID чата MAX: команда .id в нужном чате.
  - chat_id Telegram: перешлите сообщение из нужного чата боту @RawDataBot.
  - thread_id: ID топика форум-группы (из ссылки на сообщение в теме).

Команды:
  .tgfwd        — включить/выключить пересылку
  .tgfwdtest    — тестовое сообщение в Telegram
  .tgfwdinfo    — настройки и статистика
  .tgfwddebug   — дамп полей сообщения (в ответ на сообщение)

Как это работает:
  - Watcher только фильтрует сообщение и кладёт его в очередь. Отправляет
    один фоновый воркер: порядок в Telegram совпадает с MAX, а юзербот не
    тормозит на скачивании файлов.
  - Дубли (одинаковые chat_id + id) отсекаются.
  - Telegram 429 — ждём retry_after; 5xx и сеть — повтор с паузой; битая
    HTML-разметка — повтор без неё; несуществующая ветка — повтор в общий чат.
  - Вложения качаются параллельно (до 4), файлы больше 50 МБ (лимит Bot API)
    не качаются вовсе.

Оговорки:
  - Структура вложений PyMax задокументирована не для всех типов, поэтому
    тип определяется эвристически (имя класса / поле type). Если что-то не
    пересылается — в логах будет repr() вложения, плюс есть .tgfwddebug.
  - У стикеров и кружков в Bot API нет подписи, поэтому перед ними уходит
    отдельное сообщение с именем отправителя.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import html
import inspect
import json
import logging
import re
import socket
import time
from collections import OrderedDict
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from maxli import loader, utils

logger = logging.getLogger(__name__)

MODULE_VERSION = "2026-11-18.6 (photo download fallbacks, preview, clear errors)"

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
UPLOAD_LIMIT = 50 * 1024 * 1024
ALBUM_MAX = 10
QUEUE_MAX = 500
DEDUP_MAX = 1000
NAME_CACHE_MAX = 2000
NAME_NEGATIVE_TTL = 600
DOWNLOAD_CONCURRENCY = 4
MAX_FLOOD_WAITS = 5
MAX_FLOOD_WAIT_SEC = 120

try:
    from aiohttp_socks import ProxyConnector

    _HAS_SOCKS = True
except ImportError:
    _HAS_SOCKS = False


MEDIA_METHOD_MAP = {
    "photo": ("sendPhoto", "photo"),
    "video": ("sendVideo", "video"),
    "video_note": ("sendVideoNote", "video_note"),
    "voice": ("sendVoice", "voice"),
    "audio": ("sendAudio", "audio"),
    "sticker": ("sendSticker", "sticker"),
    "document": ("sendDocument", "document"),
}
URL_SEND_KINDS = {"photo", "video", "document", "voice", "audio"}
NO_CAPTION_KINDS = {"sticker", "video_note"}
ALBUM_KINDS = {"photo", "video"}
DEFAULT_EXT = {
    "photo": ".jpg",
    "video": ".mp4",
    "video_note": ".mp4",
    "voice": ".ogg",
    "audio": ".mp3",
    "sticker": ".webp",
}
SKIP_MARKERS = (
    "SHARE", "LINK", "PREVIEW", "CONTROL", "KEYBOARD",
    "CONTACT", "LOCATION", "UNKNOWN", "POLL", "REPLY",
)
LINK_ATTRS = (
    "linked_message", "link", "forward", "forwarded_message",
    "forward_message", "reply", "reply_to",
)
FORWARD_ATTRS = {"forward", "forwarded_message", "forward_message"}

_TAG_RE = re.compile(r"<[^>]+>")

# CDN MAX (i.oneme.ru, *.okcdn.ru) может отвечать 403 / рвать соединение
# клиентам без браузерного User-Agent и Referer, поэтому притворяемся веб-версией
DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,video/*,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": "https://web.max.ru/",
}


class _TooLarge(Exception):
    """Файл больше, чем бот может загрузить в Telegram."""


@loader.tds
class MaxToTgForwarderMod(loader.Module):
    """Пересылает сообщения (текст и медиа) из чата MAX в Telegram через Bot API"""

    strings = {"name": "MaxToTgForwarder"}

    def __init__(self):
        self.config = loader.ModuleConfig(
            loader.ConfigValue(
                "source_chat_id",
                0,
                lambda: "ID чата MAX, из которого пересылать (узнать: .id в нужном чате)",
                validator=loader.validators.Integer(),
            ),
            loader.ConfigValue(
                "bot_token",
                "",
                lambda: "Токен Telegram-бота, полученный у @BotFather",
                validator=loader.validators.String(),
            ),
            loader.ConfigValue(
                "target_chat_id",
                "",
                lambda: (
                    "ID чата/канала Telegram, куда пересылать "
                    "(для супергрупп/каналов обычно начинается с -100)"
                ),
                validator=loader.validators.String(),
            ),
            loader.ConfigValue(
                "thread_id",
                0,
                lambda: (
                    "ID ветки (топика) для ВСЕХ сообщений, кроме одиночных фото "
                    "(см. single_photo_thread_id). 0 — общий чат"
                ),
                validator=loader.validators.Integer(),
            ),
            loader.ConfigValue(
                "single_photo_thread_id",
                0,
                lambda: (
                    "ID ветки для сообщений, где РОВНО одно фото и больше ничего "
                    "(текст не длиннее single_photo_max_text_length). "
                    "0 — не выделять такие сообщения"
                ),
                validator=loader.validators.Integer(),
            ),
            loader.ConfigValue(
                "single_photo_max_text_length",
                0,
                lambda: (
                    "Сколько символов текста ещё считается 'просто фото'. "
                    "0 — фото должно быть совсем без подписи"
                ),
                validator=loader.validators.Integer(),
            ),
            loader.ConfigValue(
                "template",
                "<b>{sender}</b>\n{text}",
                lambda: (
                    "Шаблон сообщения/подписи. Доступны {sender} и {text} "
                    "(текст уже в <blockquote>). HTML-разметка Telegram"
                ),
                validator=loader.validators.String(),
            ),
            loader.ConfigValue(
                "enabled",
                True,
                lambda: "Включена ли пересылка",
                validator=loader.validators.Boolean(),
            ),
            loader.ConfigValue(
                "forward_media",
                True,
                lambda: "Пересылать медиа (фото, видео, кружки, голосовые, стикеры, файлы)",
                validator=loader.validators.Boolean(),
            ),
            loader.ConfigValue(
                "resolve_names",
                True,
                lambda: (
                    "Получать имя отправителя через клиент MAX вместо числового ID. "
                    "Если не работает — смотрите .tgfwddebug"
                ),
                validator=loader.validators.Boolean(),
            ),
            loader.ConfigValue(
                "proxy_url",
                "",
                lambda: (
                    "Прокси до Telegram API: http://user:pass@ip:port или "
                    "socks5://user:pass@ip:port (нужен aiohttp_socks). Пусто — напрямую"
                ),
                validator=loader.validators.String(),
            ),
            loader.ConfigValue(
                "timeout",
                30,
                lambda: "Таймаут запроса к Telegram API, сек (для загрузки файлов x4)",
                validator=loader.validators.Integer(),
            ),
            loader.ConfigValue(
                "retries",
                2,
                lambda: "Сколько раз повторить отправку при ошибке сети (0 — без повторов)",
                validator=loader.validators.Integer(),
            ),
            loader.ConfigValue(
                "force_ipv4",
                True,
                lambda: (
                    "Ходить к MAX и Telegram только по IPv4 (если IPv6 сломан, "
                    "соединение висит до таймаута). Применяется после перезагрузки"
                ),
                validator=loader.validators.Boolean(),
            ),
        )

        self._download_session: aiohttp.ClientSession | None = None
        self._tg_session: aiohttp.ClientSession | None = None
        self._fallback_session: aiohttp.ClientSession | None = None
        self._tg_session_proxy: str | None = None
        self._socks_warned = False

        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None

        self._name_cache: OrderedDict = OrderedDict()
        self._name_method: tuple | None = None
        self._seen: OrderedDict = OrderedDict()

        self._stats = {"forwarded": 0, "failed": 0, "dropped": 0, "duplicates": 0}

    # ------------------------------------------------------------------ #
    # Жизненный цикл
    # ------------------------------------------------------------------ #

    def _new_plain_session(self) -> aiohttp.ClientSession:
        kwargs = {"ttl_dns_cache": 300, "limit": 20}
        if self.config["force_ipv4"]:
            kwargs["family"] = socket.AF_INET
        return aiohttp.ClientSession(connector=aiohttp.TCPConnector(**kwargs))

    async def client_ready(self):
        self._download_session = self._new_plain_session()
        self._ensure_worker()
        logger.info("MaxToTgForwarder загружен, версия %s", MODULE_VERSION)

    async def on_unload(self):
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._worker = None

        for session in (self._download_session, self._tg_session, self._fallback_session):
            if session is not None and not session.closed:
                await session.close()

    def _ensure_worker(self):
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=QUEUE_MAX)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._worker_loop())

    async def _worker_loop(self):
        while True:
            message = await self._queue.get()
            try:
                await self._process_message(message)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                self._stats["failed"] += 1
                logger.exception("MaxToTgForwarder: необработанная ошибка при пересылке")
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------ #
    # Сессии/прокси
    # ------------------------------------------------------------------ #

    def _proxy_url(self) -> str:
        return (self.config["proxy_url"] or "").strip()

    async def _get_download_session(self) -> aiohttp.ClientSession:
        # Вложения из MAX качаются всегда напрямую, без прокси Telegram
        if self._download_session is None or self._download_session.closed:
            self._download_session = self._new_plain_session()
        return self._download_session

    async def _get_telegram_session(self) -> aiohttp.ClientSession:
        proxy_url = self._proxy_url()

        if proxy_url.startswith(("socks4://", "socks5://")):
            if not _HAS_SOCKS:
                if not self._socks_warned:
                    logger.error(
                        "MaxToTgForwarder: указан socks-прокси, но aiohttp_socks "
                        "не установлен (pip install aiohttp_socks). Иду напрямую."
                    )
                    self._socks_warned = True
                return await self._get_download_session()

            if (
                self._tg_session is None
                or self._tg_session.closed
                or proxy_url != self._tg_session_proxy
            ):
                if self._tg_session is not None and not self._tg_session.closed:
                    await self._tg_session.close()
                self._tg_session = aiohttp.ClientSession(
                    connector=ProxyConnector.from_url(proxy_url)
                )
                self._tg_session_proxy = proxy_url
            return self._tg_session

        # http(s)-прокси или без прокси: обычная сессия, прокси идёт через `proxy=`
        return await self._get_download_session()

    # ------------------------------------------------------------------ #
    # Текст/HTML
    # ------------------------------------------------------------------ #

    @staticmethod
    def _escape_html(text) -> str:
        return html.escape(str(text), quote=False)

    @staticmethod
    def _strip_html(text: str) -> str:
        return html.unescape(_TAG_RE.sub("", text))

    def _mask(self, text) -> str:
        """Вырезает токен бота из текста ошибок."""
        token = (self.config["bot_token"] or "").strip()
        text = str(text)
        return text.replace(token, "***") if token else text

    @staticmethod
    def _mask_proxy(url: str) -> str:
        if not url:
            return url
        try:
            parts = urlsplit(url)
            if parts.password:
                host = parts.hostname or ""
                if parts.port:
                    host = f"{host}:{parts.port}"
                return urlunsplit(parts._replace(netloc=f"{parts.username}:***@{host}"))
        except ValueError:
            pass
        return url

    def _render(self, sender_html: str, text: str) -> str:
        """Подстановка в шаблон через replace, а не str.format: фигурные
        скобки в пользовательском шаблоне больше не роняют модуль."""
        body = f"<blockquote>{self._escape_html(text)}</blockquote>" if text else ""
        template = self.config["template"] or "<b>{sender}</b>\n{text}"
        return template.replace("{sender}", sender_html).replace("{text}", body).strip()

    def _text_budget(self, sender_html: str, limit: int) -> int:
        """Сколько символов текста влезет в лимит вместе с шаблоном
        (Telegram считает видимые символы, без тегов)."""
        overhead = len(self._strip_html(self._render(sender_html, "")))
        return max(100, limit - overhead - 4)

    @staticmethod
    def _split_text(text: str, size: int) -> list:
        chunks = []
        while len(text) > size:
            cut = text.rfind("\n", 0, size)
            if cut < size // 2:
                cut = text.rfind(" ", 0, size)
            if cut < size // 2:
                cut = size
            chunks.append(text[:cut].rstrip())
            text = text[cut:].lstrip()
        if text:
            chunks.append(text)
        return chunks

    @staticmethod
    def _err_text(exc: BaseException) -> str:
        msg = str(exc).strip()
        return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__

    # ------------------------------------------------------------------ #
    # Разбор сообщения MAX
    # ------------------------------------------------------------------ #

    @staticmethod
    def _classify_attachment(att) -> str:
        type_val = getattr(att, "type", None)
        type_name = str(getattr(type_val, "value", type_val) or "").upper()
        combined = f"{type_name} {att.__class__.__name__.upper()}"

        if any(k in combined for k in SKIP_MARKERS):
            return "skip"
        if "STICKER" in combined:
            return "sticker"
        if "VIDEO" in combined and any(k in combined for k in ("NOTE", "ROUND", "CIRCLE")):
            return "video_note"
        if "VOICE" in combined:
            return "voice"
        if "VIDEO" in combined:
            # Кружки в MAX квадратные. Если квадратное видео не примется как
            # кружок, сработает фолбэк на sendVideo.
            width = getattr(att, "width", None)
            height = getattr(att, "height", None)
            if width and height and width == height:
                return "video_note"
            if getattr(att, "video_type", None) in (1, "1", "NOTE", "ROUND"):
                return "video_note"
            return "video"
        if "PHOTO" in combined or "IMAGE" in combined:
            return "photo"
        if "AUDIO" in combined:
            is_voice = getattr(att, "is_voice", None) or getattr(att, "voice", None)
            return "voice" if is_voice else "audio"
        if "FILE" in combined or "DOCUMENT" in combined:
            return "document"
        return "skip"

    def _pick_thread_id(self, kinds: list, text: str = "") -> int:
        """Ровно одно фото без (значимого) текста -> single_photo_thread_id."""
        single_photo_thread = self.config["single_photo_thread_id"]
        if (
            single_photo_thread
            and kinds == ["photo"]
            and len((text or "").strip()) <= self.config["single_photo_max_text_length"]
        ):
            return single_photo_thread
        return self.config["thread_id"]

    @staticmethod
    def _extract_attachment_urls(att) -> list:
        """Все ссылки-кандидаты из вложения, без дублей. Если первая не
        скачается, пробуем следующие. У фото PyMax главная — base_url
        (https://i.oneme.ru/i?r=...), плюс base_raw_url, если есть."""
        found = []

        def add(val):
            if val and isinstance(val, str) and val.startswith("http") and val not in found:
                found.append(val)

        for attr in (
            "url", "base_url", "base_raw_url", "src", "link", "photo_url", "video_url"
        ):
            add(getattr(att, attr, None))

        for nested_attr in ("payload", "video", "audio", "file", "media"):
            nested = getattr(att, nested_attr, None)
            if nested is None:
                continue
            for attr in ("url", "base_url", "base_raw_url", "src"):
                add(getattr(nested, attr, None))

        for el in getattr(att, "elements", None) or []:
            add(getattr(el, "url", None))

        sizes = getattr(att, "sizes", None) or getattr(att, "photo_sizes", None)
        if sizes:
            try:
                add(getattr(sizes[-1], "url", None))
            except (IndexError, TypeError, KeyError):
                pass
        return found

    @staticmethod
    def _preview_bytes(att) -> bytes | None:
        """preview_data у PhotoAttach: маленькое превью (bytes, base64 или
        data:-URI). Последний шанс показать фото, если оригинал не скачался."""
        raw = getattr(att, "preview_data", None)
        if not raw:
            return None
        if isinstance(raw, (bytes, bytearray)):
            data = bytes(raw)
        elif isinstance(raw, str):
            payload = raw.split(",", 1)[1] if raw.startswith("data:") else raw
            try:
                data = base64.b64decode(payload + "=" * (-len(payload) % 4))
            except (ValueError, binascii.Error):
                return None
        else:
            return None
        # Только настоящие картинки: JPEG / PNG / WEBP
        if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" or data[8:12] == b"WEBP":
            return data
        return None

    @staticmethod
    def _ids_of(obj, default_chat_id=None):
        chat_id = getattr(obj, "chat_id", None)
        if chat_id is None:
            chat_id = default_chat_id
        message_id = (
            getattr(obj, "id", None)
            or getattr(obj, "message_id", None)
            or getattr(obj, "mid", None)
        )
        return chat_id, message_id

    @staticmethod
    def _payload_of(obj):
        text = getattr(obj, "text", None)
        text = str(text) if text else ""
        attaches = getattr(obj, "attaches", None) or getattr(obj, "attachments", None) or []
        try:
            attaches = list(attaches)
        except TypeError:
            attaches = []
        return text, attaches

    def _find_linked(self, message):
        """Ищет пересланное/цитируемое сообщение (в схеме MAX это
        linked_message(.message); имя атрибута в PyMax не задокументировано)."""
        for link_attr in LINK_ATTRS:
            linked = getattr(message, link_attr, None)
            if linked is None or isinstance(linked, (str, bytes, int)):
                continue

            for candidate in (getattr(linked, "message", None), linked):
                if candidate is None or candidate is message:
                    continue
                c_text, c_attaches = self._payload_of(candidate)
                if not (c_text or c_attaches):
                    continue

                linked_chat = getattr(linked, "chat_id", None)
                cand_ids = self._ids_of(candidate, default_chat_id=linked_chat)
                pairs = [cand_ids]
                if linked_chat is not None and cand_ids[1] is not None and cand_ids[0] != linked_chat:
                    pairs.append((linked_chat, cand_ids[1]))
                return {
                    "attr": link_attr,
                    "linked": linked,
                    "text": c_text,
                    "attaches": c_attaches,
                    "sender": getattr(linked, "sender", None),
                    "pairs": pairs,
                }
        return None

    @staticmethod
    def _is_forward(found) -> bool:
        if found["attr"] in FORWARD_ATTRS:
            return True
        type_val = getattr(found["linked"], "type", None)
        return "FORWARD" in str(getattr(type_val, "value", type_val) or "").upper()

    def _get_effective_content(self, message):
        """(text, attaches, original_sender, id_pairs).

        Пересылка без комментария -> содержимое пересланного.
        Пересылка С комментарием -> комментарий + пересланное (раньше терялось).
        Ответ (reply) -> только сам ответ, цитата не дублируется.
        """
        outer_ids = self._ids_of(message)
        text, attaches = self._payload_of(message)
        found = self._find_linked(message)

        if text or attaches:
            if found is not None and self._is_forward(found) and found["text"] != text:
                combined = "\n\n".join(t for t in (text, found["text"]) if t)
                return (
                    combined,
                    attaches + found["attaches"],
                    found["sender"],
                    [outer_ids] + found["pairs"],
                )
            return text, attaches, None, [outer_ids]

        if found is not None:
            return found["text"], found["attaches"], found["sender"], [outer_ids] + found["pairs"]
        return "", [], None, [outer_ids]

    def _is_duplicate(self, message) -> bool:
        chat_id, message_id = self._ids_of(message)
        if message_id is None:
            return False
        key = (str(chat_id), str(message_id))
        if key in self._seen:
            return True
        self._seen[key] = None
        while len(self._seen) > DEDUP_MAX:
            self._seen.popitem(last=False)
        return False

    # ------------------------------------------------------------------ #
    # Клиент MAX
    # ------------------------------------------------------------------ #

    async def _call_flexible(self, method, **kwargs):
        """Вызывает метод клиента, пробуя message_id как есть и как str/int
        (в примерах PyMax он то int, то str)."""
        variants = [kwargs]
        mid = kwargs.get("message_id")
        if mid is not None:
            alt = dict(kwargs)
            if isinstance(mid, str):
                try:
                    alt["message_id"] = int(mid)
                except ValueError:
                    alt = None
            else:
                alt["message_id"] = str(mid)
            if alt is not None:
                variants.append(alt)

        last_exc = None
        for variant in variants:
            try:
                result = method(**variant)
                if inspect.isawaitable(result):
                    result = await result
                if result is not None:
                    return result
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        if last_exc is not None:
            logger.warning(
                "MaxToTgForwarder: вызов %s не удался: %r",
                getattr(method, "__name__", method),
                last_exc,
            )
        return None

    async def _resolve_media_urls(self, att, kind: str, id_pairs) -> list:
        """Ссылки-кандидаты для скачивания, лучшая первой."""
        urls = self._extract_attachment_urls(att)
        api_url = await self._resolve_api_url(att, kind, id_pairs)
        if api_url:
            urls = [api_url] + [u for u in urls if u != api_url]
        return urls

    async def _resolve_api_url(self, att, kind: str, id_pairs):
        """У файлов и видео (включая кружки) прямой ссылки нет: берём
        file_id/video_id и запрашиваем временную ссылку через
        client.get_file_by_id / client.get_video_by_id."""
        client = getattr(self, "client", None)

        if client is not None and kind in ("document", "video", "video_note"):
            if kind == "document":
                method = getattr(client, "get_file_by_id", None)
                id_key = "file_id"
                media_id = getattr(att, "file_id", None) or getattr(att, "id", None)
            else:
                method = getattr(client, "get_video_by_id", None)
                id_key = "video_id"
                media_id = getattr(att, "video_id", None) or getattr(att, "id", None)

            if method is not None and media_id is not None:
                tried = set()
                for chat_id, message_id in id_pairs:
                    if chat_id is None or message_id is None:
                        continue
                    key = (str(chat_id), str(message_id))
                    if key in tried:
                        continue
                    tried.add(key)

                    info = await self._call_flexible(
                        method, chat_id=chat_id, message_id=message_id, **{id_key: media_id}
                    )
                    if info is None:
                        continue
                    url = info.get("url") if isinstance(info, dict) else getattr(info, "url", None)
                    if url:
                        return url
        return None

    # ------------------------------------------------------------------ #
    # Имя отправителя
    # ------------------------------------------------------------------ #

    @staticmethod
    def _name_from_obj(obj) -> str | None:
        if obj is None:
            return None
        if isinstance(obj, dict):
            for key in ("name", "display_name", "full_name", "title", "username"):
                if obj.get(key):
                    return str(obj[key])
            return None

        for attr in ("name", "display_name", "full_name", "title", "username"):
            val = getattr(obj, attr, None)
            if val:
                return str(val)

        first = getattr(obj, "first_name", None)
        last = getattr(obj, "last_name", None)
        if first or last:
            return " ".join(str(p) for p in (first, last) if p)

        names = getattr(obj, "names", None)
        if names:
            try:
                entry = names[0]
                n = getattr(entry, "name", None) or getattr(entry, "first_name", None)
                if n:
                    return str(n)
            except (IndexError, TypeError, KeyError):
                pass
        return None

    async def _fetch_user_name(self, user_id) -> str | None:
        """Best-effort резолв имени через клиент PyMax. Сработавший метод
        запоминается и дальше пробуется первым."""
        client = getattr(self, "client", None)
        if client is None:
            return None

        candidates = [
            ("get_user", False),
            ("fetch_user", False),
            ("get_contact", False),
            ("fetch_contact", False),
            ("get_users", True),
            ("get_contacts", True),
            ("fetch_users", True),
        ]
        if self._name_method in candidates:
            candidates.remove(self._name_method)
            candidates.insert(0, self._name_method)

        for method_name, is_batch in candidates:
            method = getattr(client, method_name, None)
            if method is None:
                continue
            try:
                result = method([user_id] if is_batch else user_id)
                if inspect.isawaitable(result):
                    result = await result
            except Exception:  # noqa: BLE001
                continue

            if is_batch and result:
                try:
                    result = result[0]
                except (TypeError, IndexError, KeyError):
                    pass

            name = self._name_from_obj(result)
            if name:
                self._name_method = (method_name, is_batch)
                return name
        return None

    async def _resolve_name(self, sender) -> str:
        """Резолвит имя по значению sender (int ID, строка или объект)."""
        if isinstance(sender, str) and not sender.strip().isdigit():
            return sender

        user_id = sender
        if sender is not None and not isinstance(sender, (int, str)):
            name = self._name_from_obj(sender)
            if name:
                return name
            user_id = getattr(sender, "id", None) or getattr(sender, "user_id", None)

        if user_id is None:
            return "MAX"
        if isinstance(user_id, str) and user_id.strip().isdigit():
            user_id = int(user_id.strip())
        key = str(user_id)

        cached = self._name_cache.get(key)
        if cached is not None:
            name, expires = cached
            if name:
                self._name_cache.move_to_end(key)
                return name
            if expires > time.monotonic():
                return key

        if not self.config["resolve_names"]:
            return key

        # Неудача тоже кэшируется на 10 минут, иначе каждое сообщение
        # неизвестного автора дёргало бы MAX до 7 раз
        name = await self._fetch_user_name(user_id)
        self._name_cache[key] = (name, 0 if name else time.monotonic() + NAME_NEGATIVE_TTL)
        self._name_cache.move_to_end(key)
        while len(self._name_cache) > NAME_CACHE_MAX:
            self._name_cache.popitem(last=False)
        return name or key

    async def _resolve_sender_name(self, message) -> str:
        for attr in ("sender_name", "from_name", "author_name", "user_name"):
            val = getattr(message, attr, None)
            if val:
                return str(val)
        return await self._resolve_name(getattr(message, "sender", None))

    # ------------------------------------------------------------------ #
    # Telegram API
    # ------------------------------------------------------------------ #

    def _plain_fallback(self, data: dict) -> dict:
        """Копия запроса без HTML-разметки (на случай битого шаблона)."""
        data = dict(data)
        data.pop("parse_mode", None)
        for key in ("text", "caption"):
            if isinstance(data.get(key), str):
                data[key] = self._strip_html(data[key])
        if isinstance(data.get("media"), list):
            media = []
            for entry in data["media"]:
                entry = dict(entry)
                entry.pop("parse_mode", None)
                if isinstance(entry.get("caption"), str):
                    entry["caption"] = self._strip_html(entry["caption"])
                media.append(entry)
            data["media"] = media
        return data

    @staticmethod
    def _form_value(value) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list, bool)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    async def _tg_call(self, method: str, data: dict, files: dict | None = None):
        """Возвращает (ok, error, result)."""
        token = (self.config["bot_token"] or "").strip()
        if not token:
            return False, "не настроен bot_token (см. .tgfwdinfo)", None
        if not str(self.config["target_chat_id"] or "").strip():
            return False, "не настроен target_chat_id (см. .tgfwdinfo)", None

        url = TELEGRAM_API_BASE.format(token=token, method=method)
        base_timeout = max(5, self.config["timeout"] or 30)
        timeout = aiohttp.ClientTimeout(total=base_timeout * (4 if files else 1), connect=15)

        proxy_url = self._proxy_url()
        http_proxy = proxy_url if proxy_url.startswith(("http://", "https://")) else None

        max_attempts = max(0, self.config["retries"] or 0) + 1
        data = {k: v for k, v in data.items() if v is not None}
        last_error = "неизвестная ошибка"

        attempt = 0
        flood_waits = 0
        thread_fixed = False
        markup_fixed = False

        while attempt < max_attempts:
            attempt += 1
            result = None
            status = None
            try:
                session = await self._get_telegram_session()
                if files:
                    form = aiohttp.FormData()
                    for key, value in data.items():
                        form.add_field(key, self._form_value(value))
                    for field_name, (filename, content, content_type) in files.items():
                        form.add_field(
                            field_name, content, filename=filename, content_type=content_type
                        )
                    request_ctx = session.post(url, data=form, timeout=timeout, proxy=http_proxy)
                else:
                    request_ctx = session.post(url, json=data, timeout=timeout, proxy=http_proxy)

                async with request_ctx as resp:
                    status = resp.status
                    try:
                        result = await resp.json(content_type=None)
                    except (ValueError, aiohttp.ContentTypeError):
                        result = None

            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, TimeoutError):
                last_error = (
                    "не удалось подключиться к api.telegram.org (таймаут). "
                    "Возможно, Telegram заблокирован в этой сети — задайте proxy_url "
                    "(.config MaxToTgForwarder proxy_url http://... или socks5://...)"
                )
            except aiohttp.ClientConnectorError as exc:
                last_error = (
                    f"не удалось подключиться к api.telegram.org: {exc}. "
                    "Проверьте интернет/DNS или задайте proxy_url"
                )
            except (aiohttp.ClientError, OSError) as exc:
                last_error = f"сетевая ошибка: {self._err_text(exc)}"
            except Exception as exc:  # noqa: BLE001
                logger.exception("MaxToTgForwarder: исключение при обращении к Telegram")
                return False, self._mask(self._err_text(exc)), None
            else:
                if not isinstance(result, dict):
                    # HTML-ошибка прокси/балансировщика: стоит повторить
                    last_error = f"Telegram вернул не-JSON ответ (HTTP {status})"
                elif result.get("ok"):
                    logger.debug("MaxToTgForwarder: Telegram %s — ок", method)
                    return True, None, result.get("result")
                else:
                    desc = str(result.get("description") or result)
                    desc_low = desc.lower()
                    code = result.get("error_code") or status or 0
                    params = result.get("parameters") or {}

                    if code == 429:
                        wait = int(params.get("retry_after") or 5)
                        if flood_waits < MAX_FLOOD_WAITS and wait <= MAX_FLOOD_WAIT_SEC:
                            flood_waits += 1
                            logger.warning("MaxToTgForwarder: flood-лимит Telegram, жду %sс", wait)
                            await asyncio.sleep(wait + 0.5)
                            attempt -= 1
                            continue
                        return False, desc, None

                    if "thread not found" in desc_low and "message_thread_id" in data and not thread_fixed:
                        logger.warning(
                            "MaxToTgForwarder: ветка %s не найдена, шлю в общий чат",
                            data["message_thread_id"],
                        )
                        data.pop("message_thread_id")
                        thread_fixed = True
                        attempt -= 1
                        continue

                    if "can't parse entities" in desc_low and not markup_fixed:
                        logger.warning("MaxToTgForwarder: битая разметка (%s), шлю без неё", desc)
                        data = self._plain_fallback(data)
                        markup_fixed = True
                        attempt -= 1
                        continue

                    if code >= 500:
                        last_error = desc
                    else:
                        return False, desc, None

            if attempt < max_attempts:
                await asyncio.sleep(1.5 * attempt)

        last_error = self._mask(last_error)
        logger.error("MaxToTgForwarder: %s: %s", method, last_error)
        return False, last_error, None

    def _base_payload(self, thread_id) -> dict:
        payload = {"chat_id": self.config["target_chat_id"]}
        if thread_id:
            payload["message_thread_id"] = thread_id
        return payload

    async def _send_text_message(self, html_text: str, thread_id: int | None = None):
        if thread_id is None:
            thread_id = self.config["thread_id"]
        payload = self._base_payload(thread_id)
        payload.update(
            text=html_text,
            parse_mode="HTML",
            link_preview_options={"is_disabled": True},
        )
        ok, error, _ = await self._tg_call("sendMessage", payload)
        return ok, error

    async def _send_long_text(self, sender: str, text: str, thread_id):
        """Текст длиннее 4096 режется на части по абзацам, а не обрезается."""
        chunks = self._split_text(text or "", self._text_budget(sender, TEXT_LIMIT)) or [""]
        ok, error = await self._send_text_message(self._render(sender, chunks[0]), thread_id)
        for chunk in chunks[1:]:
            part_ok, part_err = await self._send_text_message(
                f"<blockquote>{self._escape_html(chunk)}</blockquote>", thread_id
            )
            ok = ok and part_ok
            error = error or part_err
        return ok, error

    # ------------------------------------------------------------------ #
    # Вложения
    # ------------------------------------------------------------------ #

    async def _get_fallback_session(self) -> aiohttp.ClientSession:
        """Сессия без ограничения на IPv4: если CDN MAX отвечает только по
        IPv6 (или IPv4-маршрут сломан), второй заход идёт через неё."""
        if self._fallback_session is None or self._fallback_session.closed:
            self._fallback_session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ttl_dns_cache=300, limit=10)
            )
        return self._fallback_session

    def _use_preview(self, item: dict) -> dict:
        """Оригинал фото не скачался, но в сообщении есть превью: шлём его,
        чтобы в Telegram было хоть какое-то фото, а не только текст ошибки."""
        if item["kind"] != "photo":
            return item
        preview = self._preview_bytes(item["att"])
        if preview is None:
            return item
        logger.warning("MaxToTgForwarder: оригинал фото не скачался, шлю превью из сообщения")
        item.update(
            data=preview,
            ct="image/png" if preview[:4] == b"\x89PNG" else "image/jpeg",
            filename="photo_preview.jpg",
            is_preview=True,
        )
        return item

    async def _download(self, url: str, attempts: int = 2, session=None):
        session = session or await self._get_download_session()
        timeout = aiohttp.ClientTimeout(total=600, connect=15, sock_read=60)
        last_exc: BaseException | None = None

        for attempt in range(attempts):
            try:
                async with session.get(url, timeout=timeout, headers=DOWNLOAD_HEADERS) as resp:
                    resp.raise_for_status()
                    if resp.content_length and resp.content_length > UPLOAD_LIMIT:
                        raise _TooLarge(resp.content_length)
                    buf = bytearray()
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        buf.extend(chunk)
                        if len(buf) > UPLOAD_LIMIT:
                            raise _TooLarge(len(buf))
                    return bytes(buf), resp.headers.get("Content-Type", "")
            except _TooLarge:
                raise
            except aiohttp.ClientResponseError as exc:
                # 4xx: ссылка протухла или недоступна, повторять бессмысленно
                if 400 <= exc.status < 500:
                    raise
                last_exc = exc
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, TimeoutError, aiohttp.ClientError, OSError) as exc:
                last_exc = exc

            if attempt < attempts - 1:
                await asyncio.sleep(2 * (attempt + 1))

        raise last_exc or RuntimeError("download failed")

    @staticmethod
    def _ensure_extension(filename: str, kind: str) -> str:
        """У ссылок MAX имя файла часто без расширения — добавляем по типу."""
        if "." in filename.rsplit("/", 1)[-1]:
            return filename
        return filename + DEFAULT_EXT.get(kind, "")

    @staticmethod
    def _new_item(att, kind: str, name=None, error=None) -> dict:
        return {
            "att": att,
            "kind": kind,
            "name": str(name) if name else None,
            "url": None,
            "data": None,
            "ct": "",
            "filename": None,
            "error": error,
            "url_ok": True,
            "skip": False,
        }

    async def _prepare_item(self, att, kind: str, id_pairs) -> dict:
        """Получает ссылку и скачивает вложение. Ничего не отправляет."""
        att_name = getattr(att, "name", None) or getattr(att, "file_name", None)
        item = self._new_item(att, kind, att_name)

        urls = await self._resolve_media_urls(att, kind, id_pairs)
        if not urls:
            logger.warning("MaxToTgForwarder: не удалось получить URL вложения (%s): %r", kind, att)
            item["error"] = "в вложении нет ссылки на файл (пришлите вывод .tgfwddebug)"
            return self._use_preview(item)
        item["url"] = urls[0]

        # Пробуем каждую ссылку: сначала основной сессией (IPv4, если включено),
        # затем, если сеть не пустила, обычной (IPv4+IPv6)
        sessions = [await self._get_download_session()]
        if self.config["force_ipv4"]:
            sessions.append(await self._get_fallback_session())

        data = content_type = None
        errors = []
        for url in urls:
            for session in sessions:
                try:
                    data, content_type = await self._download(url, session=session)
                    item["url"] = url
                    break
                except _TooLarge as exc:
                    size_mb = (exc.args[0] if exc.args else 0) / 1024 / 1024
                    item["error"] = f"файл {size_mb:.0f} МБ, больше лимита Bot API (50 МБ)"
                    item["url_ok"] = False
                    return item
                except aiohttp.ClientResponseError as exc:
                    # HTTP-ответ пришёл, значит сеть ни при чём: другая сессия
                    # не поможет, переходим к следующей ссылке
                    errors.append(f"HTTP {exc.status} от {urlsplit(url).hostname}")
                    break
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{self._err_text(exc)} ({urlsplit(url).hostname})")
            if data is not None:
                break

        if data is None:
            item["error"] = "; ".join(dict.fromkeys(errors)) or "неизвестная ошибка"
            logger.warning(
                "MaxToTgForwarder: не удалось скачать вложение (%s): %s | ссылки: %s",
                kind,
                item["error"],
                ", ".join(u[:120] for u in urls),
            )
            return self._use_preview(item)

        # Настоящее медиа не бывает HTML-страницей (превью сайта и т.п.)
        ct_lower = (content_type or "").lower()
        head = data[:200].lstrip().lower()
        if "text/html" in ct_lower or head.startswith((b"<!doctype html", b"<html")):
            logger.warning(
                "MaxToTgForwarder: по ссылке вложения (%s) скачалась HTML-страница, пропускаю", kind
            )
            item["skip"] = True
            return item

        filename = item["name"] or (url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1] or kind)
        filename = self._ensure_extension(filename, kind)

        # Голосовые в MAX почти всегда OGG/Opus, обычные аудио — mp3/m4a
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if kind == "audio" and (
            ext in ("ogg", "oga", "opus") or "ogg" in ct_lower or "opus" in ct_lower
        ):
            kind = "voice"

        item.update(
            kind=kind,
            data=data,
            ct=content_type or "application/octet-stream",
            filename=filename,
        )
        return item

    async def _send_failure_note(self, item: dict, caption: str | None, thread_id):
        label = f" «{self._escape_html(item['name'])}»" if item["name"] else ""
        note = (
            f"📎 <i>вложение{label} ({self._escape_html(item['kind'])}): "
            f"{self._escape_html(item['error'])}</i>"
        )
        if caption:
            note = f"{caption}\n\n{note}"
        await self._send_text_message(note, thread_id=thread_id)

    async def _upload(self, method, field, item, caption, thread_id, content_type=None):
        payload = self._base_payload(thread_id)
        if caption and method not in ("sendSticker", "sendVideoNote"):
            payload["caption"] = caption
            payload["parse_mode"] = "HTML"
        files = {field: (item["filename"], item["data"], content_type or item["ct"])}
        ok, error, _ = await self._tg_call(method, payload, files=files)
        return ok, error

    async def _send_by_url(self, item: dict, caption: str | None, thread_id) -> bool:
        """Сервер не скачал файл: пусть Telegram скачает его сам по ссылке."""
        kind = item["kind"]
        if not (item["url"] and item["url_ok"] and kind in URL_SEND_KINDS):
            return False
        method, field = MEDIA_METHOD_MAP[kind]
        payload = self._base_payload(thread_id)
        payload[field] = item["url"]
        if caption:
            payload["caption"] = caption
            payload["parse_mode"] = "HTML"
        ok, tg_err, _ = await self._tg_call(method, payload)
        if not ok:
            logger.warning("MaxToTgForwarder: Telegram тоже не скачал по ссылке: %s", tg_err)
        return ok

    async def _send_item(self, item: dict, caption: str | None, thread_id) -> bool:
        kind = item["kind"]

        if item["data"] is None:
            if await self._send_by_url(item, caption, thread_id):
                return True
            await self._send_failure_note(item, caption, thread_id)
            return False

        if item.get("is_preview"):
            # Оригинал не скачался: сперва Telegram по ссылке, потом превью
            if await self._send_by_url(item, caption, thread_id):
                return True
            mark = "<i>(превью: оригинал фото не скачался)</i>"
            caption = f"{caption}\n{mark}" if caption else mark

        method, field = MEDIA_METHOD_MAP.get(kind, ("sendDocument", "document"))
        ok, error = await self._upload(method, field, item, caption, thread_id)
        if ok:
            return True

        # Фолбэки: кружок -> видео, стикер -> фото/файл, остальное -> файл
        if kind == "video_note":
            fallback = ("sendVideo", "video", "video/mp4")
        elif kind == "sticker" and item["ct"].startswith(("image/png", "image/jpeg")):
            fallback = ("sendPhoto", "photo", None)
        elif kind != "document":
            fallback = ("sendDocument", "document", None)
        else:
            fallback = None

        if fallback is not None:
            logger.warning(
                "MaxToTgForwarder: %s не удался (%s), пробую %s", method, error, fallback[0]
            )
            ok, error = await self._upload(
                fallback[0], fallback[1], item, caption, thread_id, content_type=fallback[2]
            )
            if ok:
                return True

        logger.error("MaxToTgForwarder: не удалось переслать вложение (%s): %s", kind, error)
        item["error"] = error
        await self._send_failure_note(item, caption, thread_id)
        return False

    async def _send_album(self, items: list, caption: str | None, thread_id) -> bool:
        media = []
        files = {}
        for i, item in enumerate(items):
            key = f"file{i}"
            entry = {"type": item["kind"], "media": f"attach://{key}"}
            if i == 0 and caption:
                entry["caption"] = caption
                entry["parse_mode"] = "HTML"
            media.append(entry)
            files[key] = (item["filename"], item["data"], item["ct"])

        payload = self._base_payload(thread_id)
        payload["media"] = media
        ok, error, _ = await self._tg_call("sendMediaGroup", payload, files=files)
        if ok:
            return True

        logger.warning("MaxToTgForwarder: альбом не ушёл (%s), шлю по одному", error)
        all_ok = True
        for i, item in enumerate(items):
            sent = await self._send_item(item, caption if i == 0 else None, thread_id)
            all_ok = all_ok and sent
        return all_ok

    # ------------------------------------------------------------------ #
    # Обработка сообщения (вызывается воркером)
    # ------------------------------------------------------------------ #

    async def _process_message(self, message):
        text, attaches, original_sender, id_pairs = self._get_effective_content(message)

        # Классифицируем каждое вложение один раз (раньше — до 4 раз);
        # служебные (превью ссылок и т.п.) отбрасываем
        items = []
        for att in attaches:
            kind = self._classify_attachment(att)
            if kind != "skip":
                items.append((att, kind))

        if not text and not items:
            return

        sender_name = await self._resolve_sender_name(message)
        if original_sender is not None:
            sender_name = f"{sender_name} ↪️ {await self._resolve_name(original_sender)}"
        sender = self._escape_html(sender_name)

        kinds = [k for _, k in items]
        thread_id = self._pick_thread_id(kinds, text)

        logger.info(
            "MaxToTgForwarder: сообщение из чата MAX %s: текст %d симв., вложения: %s -> ветка %s",
            getattr(message, "chat_id", "?"),
            len(text),
            ", ".join(kinds) or "нет",
            thread_id or "общая",
        )

        if not items or not self.config["forward_media"]:
            if items:
                text = text or "[вложение, пересылка медиа выключена]"
            ok, error = await self._send_long_text(sender, text, thread_id)
            self._stats["forwarded" if ok else "failed"] += 1
            if not ok:
                logger.error("MaxToTgForwarder: не удалось переслать сообщение: %s", error)
            return

        # Качаем вложения параллельно, отправляем строго по порядку
        sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

        async def prepare(att, kind):
            async with sem:
                try:
                    return await self._prepare_item(att, kind, id_pairs)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("MaxToTgForwarder: ошибка подготовки вложения: %r", att)
                    item = self._new_item(att, kind, error=self._err_text(exc))
                    item["url_ok"] = False
                    return item

        prepared = await asyncio.gather(*(prepare(a, k) for a, k in items))
        prepared = [p for p in prepared if not p["skip"]]

        if not prepared:
            if text:
                ok, _ = await self._send_long_text(sender, text, thread_id)
                self._stats["forwarded" if ok else "failed"] += 1
            return

        # Текст идёт подписью к первому медиа, если влезает в 1024 символа и
        # у первого медиа вообще бывает подпись. Иначе — отдельным сообщением
        # перед медиа (так и стикер/кружок не остаются без автора, а длинный
        # текст не обрезается). Подпись ставится один раз, а не на каждое
        # вложение, как раньше.
        caption: str | None
        if prepared[0]["kind"] in NO_CAPTION_KINDS or len(text) > self._text_budget(
            sender, CAPTION_LIMIT
        ):
            await self._send_long_text(sender, text, thread_id)
            caption = None
        else:
            caption = self._render(sender, text)

        all_ok = True
        i = 0
        while i < len(prepared):
            j = i
            while (
                j < len(prepared)
                and j - i < ALBUM_MAX
                and prepared[j]["data"] is not None
                and not prepared[j].get("is_preview")
                and prepared[j]["kind"] in ALBUM_KINDS
            ):
                j += 1
            group = prepared[i:j] if j - i >= 2 else [prepared[i]]
            i += len(group)

            try:
                if len(group) >= 2:
                    ok = await self._send_album(group, caption, thread_id)
                else:
                    ok = await self._send_item(group[0], caption, thread_id)
            except Exception:  # noqa: BLE001
                logger.exception("MaxToTgForwarder: ошибка при отправке вложения")
                ok = False
            all_ok = all_ok and ok
            caption = None

        self._stats["forwarded" if all_ok else "failed"] += 1

    # ------------------------------------------------------------------ #
    # Watcher
    # ------------------------------------------------------------------ #

    @loader.watcher("no_commands", "in")
    async def watcher(self, message):
        if not self.config["enabled"]:
            return

        source_chat_id = self.config["source_chat_id"]
        if not source_chat_id:
            return

        chat_id = getattr(message, "chat_id", None)
        if chat_id is None:
            return
        try:
            same_chat = int(chat_id) == int(source_chat_id)
        except (TypeError, ValueError):
            same_chat = str(chat_id) == str(source_chat_id)
        if not same_chat:
            return

        if self._is_duplicate(message):
            self._stats["duplicates"] += 1
            return

        self._ensure_worker()
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            self._stats["dropped"] += 1
            logger.error("MaxToTgForwarder: очередь переполнена (%s), сообщение пропущено", QUEUE_MAX)

    # ------------------------------------------------------------------ #
    # Команды
    # ------------------------------------------------------------------ #

    @loader.command()
    async def tgfwdcmd(self, message):
        """— включить/выключить пересылку MAX → Telegram"""
        self.config["enabled"] = not self.config["enabled"]
        state = "включена ✅" if self.config["enabled"] else "выключена ❌"
        await utils.answer(message, f"🔀 Пересылка в Telegram {state}")

    @loader.command()
    async def tgfwdtestcmd(self, message):
        """— отправить тестовое сообщение в Telegram"""
        ok, error = await self._send_text_message(
            "<b>MaxToTgForwarder</b>\n<blockquote>✅ Тестовое сообщение</blockquote>"
        )
        if ok:
            await utils.answer(message, "✅ Тестовое сообщение успешно отправлено в Telegram")
        else:
            await utils.answer(message, f"❌ Не удалось отправить: {error}")

    @loader.command()
    async def tgfwddebugcmd(self, message):
        """— (отладка) дамп sender и вложений (в ответ на сообщение с кружком/гс/стикером)"""
        target = message
        get_reply = getattr(message, "get_reply_message", None)
        if get_reply is not None:
            try:
                replied = await get_reply()
                if replied is not None:
                    target = replied
            except Exception:  # noqa: BLE001
                pass

        sender = getattr(target, "sender", None)
        lines = [f"sender: type={type(sender).__name__}, value={sender!r}"]

        for link_attr in LINK_ATTRS:
            linked = getattr(target, link_attr, None)
            if linked is not None:
                lines.append(f"\nнайден атрибут '{link_attr}': {linked!r}"[:1000])

        _, attaches = self._payload_of(target)
        lines.append(f"attaches: {len(attaches)} шт.")

        for i, att in enumerate(attaches):
            lines.append(
                f"\n--- вложение {i} ({att.__class__.__name__}) -> "
                f"{self._classify_attachment(att)} ---"
            )
            dumped = None
            for method_name in ("model_dump", "dict"):
                dump_method = getattr(att, method_name, None)
                if dump_method is None:
                    continue
                try:
                    dumped = dump_method()
                    break
                except Exception:  # noqa: BLE001
                    continue

            if dumped is not None:
                try:
                    dump_text = json.dumps(dumped, ensure_ascii=False, default=str)
                except Exception:  # noqa: BLE001
                    dump_text = str(dumped)
            else:
                dump_text = repr(att)
            lines.append(dump_text[:1200])

        full_text = "\n".join(lines)
        if len(full_text) > 3000:
            full_text = full_text[:3000] + "\n…(обрезано)"

        await utils.answer(message, "🔧 **MaxToTgForwarder debug**\n" + utils.quote(full_text))

    @loader.command()
    async def tgfwdinfocmd(self, message):
        """— показать настройки и статистику пересылки"""
        cfg = self.config
        queue_size = self._queue.qsize() if self._queue is not None else 0
        worker_alive = self._worker is not None and not self._worker.done()
        st = self._stats
        info = (
            f"версия модуля: {MODULE_VERSION}\n"
            f"статус: {'включена' if cfg['enabled'] else 'выключена'}\n"
            f"медиа: {'да' if cfg['forward_media'] else 'нет, только текст'}\n"
            f"чат MAX (source_chat_id): {cfg['source_chat_id'] or 'не задан'}\n"
            f"чат Telegram (target_chat_id): {cfg['target_chat_id'] or 'не задан'}\n"
            f"ветка (thread_id): {cfg['thread_id'] or 'нет'}\n"
            f"ветка для одиночных фото: {cfg['single_photo_thread_id'] or 'не задана'}\n"
            f"порог текста для 'просто фото': {cfg['single_photo_max_text_length']} симв.\n"
            f"токен бота: {'задан' if cfg['bot_token'] else 'не задан'}\n"
            f"прокси: {self._mask_proxy(self._proxy_url()) or 'не используется'}\n"
            f"таймаут: {cfg['timeout']}с, повторов: {cfg['retries']}\n"
            f"воркер: {'работает' if worker_alive else 'не запущен'}, в очереди: {queue_size}\n"
            f"переслано: {st['forwarded']}, с ошибками: {st['failed']}, "
            f"дублей: {st['duplicates']}, потеряно (очередь): {st['dropped']}"
        )
        await utils.answer(message, "🔀 **MaxToTgForwarder**\n" + utils.quote(info))
