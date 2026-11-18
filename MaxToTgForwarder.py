# meta developer: @claude
# meta pic: https://em-content.zobj.net/source/microsoft-teams/337/incoming-envelope_1f4e8.png
# requires: aiohttp
# min-maxli: 100

"""
MaxToTgForwarder — модуль для юзербота Maxli (https://github.com/YouRooni/Maxli)

Пересылает сообщения из чатов MAX в Telegram через Bot API: текст (с
жирным/курсивом/ссылками), фото, видео, кружки, голосовые, стикеры, файлы.
Ответы остаются ответами, правки и удаления в MAX повторяются в Telegram.

Быстрая настройка (один чат):
  .config MaxToTgForwarder bot_token       <токен от @BotFather>
  .config MaxToTgForwarder source_chat_id  <ID чата MAX>
  .config MaxToTgForwarder target_chat_id  <ID чата Telegram>
  .config MaxToTgForwarder thread_id       <ID ветки, необязательно>

Несколько чатов (routes), через «;» или с новой строки:
  .config MaxToTgForwarder routes -111=-1001234/5; -222=-1005678
  Формат: MAX_ID=TG_CHAT[/ВЕТКА[/ВЕТКА_ДЛЯ_ОДИНОЧНЫХ_ФОТО]]

Фильтры (через запятую, регистр не важен):
  ignore_senders / only_senders  — ID или имена отправителей
  ignore_keywords                — слова, при которых сообщение не пересылается

Уведомления о сбоях: alert_chat_id — ваш ID в Telegram (сначала нажмите
/start у бота). Не чаще раза в минуту.

Как узнать ID:
  - ID чата MAX: .id в нужном чате.
  - chat_id Telegram: перешлите сообщение из чата боту @RawDataBot.
  - thread_id: из ссылки на сообщение в теме форума.

Команды:
  .tgfwd        — включить/выключить пересылку
  .tgfwdtest    — тестовое сообщение во все маршруты
  .tgfwdinfo    — настройки и статистика
  .tgfwddebug   — дамп полей сообщения (в ответ на сообщение)

Ограничения Bot API:
  - бот может удалить только свои сообщения моложе 48 часов;
  - правка доходит до текста/подписи; заменить само медиа нельзя;
  - у стикеров и кружков нет подписи — перед ними уходит строка с автором.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextvars
import hashlib
import html
import inspect
import json
import logging
import re
import socket
import time
from collections import OrderedDict, namedtuple
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from maxli import loader, utils

try:
    from pymax import Message as _PyMaxMessage
except Exception:  # noqa: BLE001
    _PyMaxMessage = None

try:
    from aiohttp_socks import ProxyConnector

    _HAS_SOCKS = True
except ImportError:
    _HAS_SOCKS = False

logger = logging.getLogger(__name__)

MODULE_VERSION = "2.0 (2026-11-18): routes, replies, edits, deletes, formatting, filters, alerts"

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
UPLOAD_LIMIT = 50 * 1024 * 1024
ALBUM_MAX = 10
QUEUE_MAX = 500
DEDUP_MAX = 2000
MAP_MAX = 3000
NAME_CACHE_MAX = 2000
NAME_NEGATIVE_TTL = 600
DOWNLOAD_CONCURRENCY = 4
MAX_FLOOD_WAITS = 5
MAX_FLOOD_WAIT_SEC = 120
ALERT_INTERVAL = 60

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
    "CONTACT", "LOCATION", "UNKNOWN", "POLL", "REPLY", "CALL",
)
LINK_ATTRS = (
    "link", "linked_message", "forward", "forwarded_message",
    "forward_message", "reply", "reply_to",
)
FORWARD_ATTRS = {"forward", "forwarded_message", "forward_message"}

# Типы элементов форматирования MAX -> теги Telegram HTML
ELEMENT_TAGS = {
    "STRONG": ("<b>", "</b>"),
    "EMPHASIZED": ("<i>", "</i>"),
    "UNDERLINE": ("<u>", "</u>"),
    "STRIKETHROUGH": ("<s>", "</s>"),
    "MONOSPACED": ("<code>", "</code>"),
    "CODE": ("<pre>", "</pre>"),
    "HEADING": ("<b>", "</b>"),
}

# CDN MAX может отвечать 403 клиентам без браузерного User-Agent/Referer
DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,video/*,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": "https://web.max.ru/",
}

_TAG_RE = re.compile(r"<[^>]+>")
_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")
_ROUTE_RE = re.compile(
    r"(-?\d+)\s*(?:=|->|→)\s*(-?\d+|@\w+)(?:\s*/\s*(\d+))?(?:\s*/\s*(\d+))?"
)

Route = namedtuple("Route", "target thread photo_thread")

# Контекст текущей пересылки: маршрут и сборщик ID отправленных сообщений
_ROUTE: contextvars.ContextVar = contextvars.ContextVar("mtt_route", default=None)
_COLLECT: contextvars.ContextVar = contextvars.ContextVar("mtt_collect", default=None)


class _TooLarge(Exception):
    """Файл больше, чем бот может загрузить в Telegram."""


def _get(obj, *names, default=None):
    """Достаёт поле из pydantic-модели или словаря (snake_case и camelCase)."""
    if obj is None:
        return default
    for name in names:
        if isinstance(obj, dict):
            if obj.get(name) is not None:
                return obj[name]
        else:
            val = getattr(obj, name, None)
            if val is not None:
                return val
    return default


def _enum_str(value) -> str:
    return str(getattr(value, "value", value) or "").upper()


def _normalize_attach(att):
    """Сырое вложение-словарь -> объект с snake_case-полями (на случай, если
    PyMax не распарсил вложение пересланного сообщения)."""
    if not isinstance(att, dict):
        return att
    fields = {_CAMEL_RE.sub("_", k).lower().lstrip("_"): v for k, v in att.items()}
    fields["type"] = att.get("_type") or att.get("type")
    return SimpleNamespace(**fields)


def _as_message(obj):
    if isinstance(obj, dict) and _PyMaxMessage is not None:
        try:
            return _PyMaxMessage.model_validate(obj)
        except Exception:  # noqa: BLE001
            pass
    return obj


def _text_hash(text: str) -> str:
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()[:12]


@loader.tds
class MaxToTgForwarderMod(loader.Module):
    """Пересылает сообщения (текст и медиа) из чатов MAX в Telegram через Bot API"""

    strings = {"name": "MaxToTgForwarder"}

    def __init__(self):
        cv = loader.ConfigValue
        v = loader.validators
        self.config = loader.ModuleConfig(
            cv("source_chat_id", 0,
               lambda: "ID чата MAX для пересылки (узнать: .id в нужном чате). Для нескольких чатов — routes",
               validator=v.Integer()),
            cv("bot_token", "", lambda: "Токен Telegram-бота от @BotFather", validator=v.String()),
            cv("target_chat_id", "",
               lambda: "ID чата/канала Telegram (у супергрупп начинается с -100)",
               validator=v.String()),
            cv("thread_id", 0,
               lambda: "ID ветки (топика) для всех сообщений, кроме одиночных фото. 0 — общий чат",
               validator=v.Integer()),
            cv("single_photo_thread_id", 0,
               lambda: "ID ветки для сообщений, где ровно одно фото и (почти) нет текста. 0 — не выделять",
               validator=v.Integer()),
            cv("single_photo_max_text_length", 0,
               lambda: "Сколько символов текста ещё считается 'просто фото'",
               validator=v.Integer()),
            cv("routes", "",
               lambda: (
                   "Дополнительные маршруты: MAX_ID=TG_CHAT[/ВЕТКА[/ВЕТКА_ФОТО]], "
                   "через ';' или с новой строки. Пример: -111=-1001234/5; -222=-1005678"
               ),
               validator=v.String()),
            cv("template", "<b>{sender}</b>\n{text}",
               lambda: "Шаблон сообщения/подписи: {sender} и {text} (текст уже в <blockquote>). HTML Telegram",
               validator=v.String()),
            cv("enabled", True, lambda: "Включена ли пересылка", validator=v.Boolean()),
            cv("forward_media", True,
               lambda: "Пересылать медиа (фото, видео, кружки, голосовые, стикеры, файлы)",
               validator=v.Boolean()),
            cv("format_text", True,
               lambda: "Переносить форматирование MAX: жирный, курсив, зачёркнутый, моно, ссылки",
               validator=v.Boolean()),
            cv("reply_threading", True,
               lambda: "Ответ в MAX -> ответ на ту же копию в Telegram (или цитата, если копии нет)",
               validator=v.Boolean()),
            cv("sync_edits", True,
               lambda: "Правка сообщения в MAX -> правка текста/подписи копии в Telegram",
               validator=v.Boolean()),
            cv("sync_deletes", True,
               lambda: "Удаление сообщения в MAX -> удаление копии в Telegram (бот может только <48ч)",
               validator=v.Boolean()),
            cv("ignore_senders", "",
               lambda: "Не пересылать от этих отправителей: ID или имена через запятую",
               validator=v.String()),
            cv("only_senders", "",
               lambda: "Пересылать ТОЛЬКО от этих отправителей: ID или имена через запятую. Пусто — от всех",
               validator=v.String()),
            cv("ignore_keywords", "",
               lambda: "Не пересылать сообщения с этими словами (через запятую, без учёта регистра)",
               validator=v.String()),
            cv("alert_chat_id", "",
               lambda: "Куда бот пишет о сбоях (ваш ID в Telegram, сначала /start боту). Пусто — не писать",
               validator=v.String()),
            cv("resolve_names", True,
               lambda: "Получать имя отправителя через клиент MAX вместо числового ID",
               validator=v.Boolean()),
            cv("proxy_url", "",
               lambda: "Прокси до Telegram: http://user:pass@ip:port или socks5://... (нужен aiohttp_socks)",
               validator=v.String()),
            cv("timeout", 30, lambda: "Таймаут запроса к Telegram, сек (загрузка файлов x4)",
               validator=v.Integer()),
            cv("retries", 2, lambda: "Повторы при ошибке сети (0 — без повторов)", validator=v.Integer()),
            cv("force_ipv4", True,
               lambda: "Ходить к MAX и Telegram по IPv4 (если IPv6 сломан). Применяется после перезагрузки",
               validator=v.Boolean()),
        )

        self._download_session: aiohttp.ClientSession | None = None
        self._fallback_session: aiohttp.ClientSession | None = None
        self._tg_session: aiohttp.ClientSession | None = None
        self._tg_session_proxy: str | None = None
        self._socks_warned = False

        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None

        self._name_cache: OrderedDict = OrderedDict()
        self._name_method: tuple | None = None
        self._seen: OrderedDict = OrderedDict()
        self._map: OrderedDict = OrderedDict()
        self._map_loaded = False

        self._routes_key = None
        self._routes_cache: dict = {}

        self._alert_last = 0.0
        self._alert_suppressed = 0
        self._last_error = ""

        self._stats = {
            "forwarded": 0, "failed": 0, "edited": 0, "deleted": 0,
            "filtered": 0, "dropped": 0, "duplicates": 0,
        }

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
        self._load_map()
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
        self._save_map()

        for session in (self._download_session, self._tg_session, self._fallback_session):
            if session is not None and not session.closed:
                await session.close()

    def _ensure_worker(self):
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=QUEUE_MAX)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._worker_loop())

    def _enqueue(self, job: tuple):
        self._ensure_worker()
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            self._stats["dropped"] += 1
            logger.error("MaxToTgForwarder: очередь переполнена (%s), событие пропущено", QUEUE_MAX)
            asyncio.ensure_future(self._alert("очередь переполнена, сообщения теряются"))

    async def _worker_loop(self):
        while True:
            job = await self._queue.get()
            kind, route = job[0], job[1]
            token = _ROUTE.set(route)
            try:
                if kind == "new":
                    await self._process_message(job[2])
                elif kind == "edit":
                    await self._process_edit(job[2])
                elif kind == "delete":
                    await self._process_delete(job[2], job[3])
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._stats["failed"] += 1
                logger.exception("MaxToTgForwarder: необработанная ошибка (%s)", kind)
                await self._alert(f"необработанная ошибка: {self._err_text(exc)}")
            finally:
                _ROUTE.reset(token)
                self._queue.task_done()

    # ------------------------------------------------------------------ #
    # Маршруты
    # ------------------------------------------------------------------ #

    def _routes(self) -> dict:
        """{str(max_chat_id): Route}. Пересобирается только при смене конфига."""
        raw = (self.config["routes"] or "").strip()
        legacy = (
            self.config["source_chat_id"],
            str(self.config["target_chat_id"] or "").strip(),
            self.config["thread_id"],
            self.config["single_photo_thread_id"],
        )
        key = (raw, legacy)
        if key == self._routes_key:
            return self._routes_cache

        routes = {}
        if legacy[0]:
            routes[str(int(legacy[0]))] = Route(legacy[1], int(legacy[2] or 0), int(legacy[3] or 0))

        for part in re.split(r"[;\n]+", raw):
            part = part.strip()
            if not part:
                continue
            m = _ROUTE_RE.fullmatch(part)
            if not m:
                logger.warning("MaxToTgForwarder: не понял маршрут %r (нужно MAX_ID=TG_CHAT[/ветка])", part)
                continue
            routes[str(int(m[1]))] = Route(m[2], int(m[3] or 0), int(m[4] or 0))

        self._routes_key = key
        self._routes_cache = routes
        return routes

    def _route_for(self, chat_id) -> Route | None:
        if chat_id is None:
            return None
        try:
            return self._routes().get(str(int(chat_id)))
        except (TypeError, ValueError):
            return None

    def _current_route(self) -> Route:
        route = _ROUTE.get()
        if route is not None:
            return route
        routes = self._routes()
        if routes:
            return next(iter(routes.values()))
        return Route(
            str(self.config["target_chat_id"] or "").strip(),
            int(self.config["thread_id"] or 0),
            int(self.config["single_photo_thread_id"] or 0),
        )

    # ------------------------------------------------------------------ #
    # Связка сообщений MAX -> Telegram (хранится в БД модуля)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _map_key(chat_id, message_id) -> str:
        return f"{chat_id}:{message_id}"

    def _load_map(self):
        if self._map_loaded:
            return
        self._map_loaded = True
        try:
            stored = self.get("msg_map", {}) or {}
            if isinstance(stored, dict):
                self._map = OrderedDict(stored)
        except Exception:  # noqa: BLE001
            logger.exception("MaxToTgForwarder: не удалось прочитать связку сообщений из БД")

    def _save_map(self):
        try:
            self.set("msg_map", dict(self._map))
        except Exception:  # noqa: BLE001
            logger.exception("MaxToTgForwarder: не удалось сохранить связку сообщений")

    def _remember(self, key: str, entry: dict):
        self._map[key] = entry
        self._map.move_to_end(key)
        while len(self._map) > MAP_MAX:
            self._map.popitem(last=False)
        self._save_map()

    # ------------------------------------------------------------------ #
    # Уведомления о сбоях
    # ------------------------------------------------------------------ #

    async def _alert(self, text: str):
        chat = str(self.config["alert_chat_id"] or "").strip()
        if not chat or not (self.config["bot_token"] or "").strip():
            return
        now = time.monotonic()
        if now - self._alert_last < ALERT_INTERVAL:
            self._alert_suppressed += 1
            return
        extra = ""
        if self._alert_suppressed:
            extra = f"\n<i>и ещё {self._alert_suppressed} сбоев за последнюю минуту</i>"
        self._alert_last = now
        self._alert_suppressed = 0
        token = _COLLECT.set(None)
        try:
            await self._tg_call(
                "sendMessage",
                {
                    "chat_id": chat,
                    "text": f"⚠️ <b>MaxToTgForwarder</b>\n{self._escape_html(text)}{extra}",
                    "parse_mode": "HTML",
                },
            )
        finally:
            _COLLECT.reset(token)

    # ------------------------------------------------------------------ #
    # Сессии/прокси
    # ------------------------------------------------------------------ #

    def _proxy_url(self) -> str:
        return (self.config["proxy_url"] or "").strip()

    async def _get_download_session(self) -> aiohttp.ClientSession:
        if self._download_session is None or self._download_session.closed:
            self._download_session = self._new_plain_session()
        return self._download_session

    async def _get_fallback_session(self) -> aiohttp.ClientSession:
        """Сессия без ограничения на IPv4 — второй заход при сетевой ошибке."""
        if self._fallback_session is None or self._fallback_session.closed:
            self._fallback_session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ttl_dns_cache=300, limit=10)
            )
        return self._fallback_session

    async def _get_telegram_session(self) -> aiohttp.ClientSession:
        proxy_url = self._proxy_url()

        if proxy_url.startswith(("socks4://", "socks5://")):
            if not _HAS_SOCKS:
                if not self._socks_warned:
                    logger.error(
                        "MaxToTgForwarder: указан socks-прокси, но aiohttp_socks не "
                        "установлен (pip install aiohttp_socks). Иду напрямую."
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
                self._tg_session = aiohttp.ClientSession(connector=ProxyConnector.from_url(proxy_url))
                self._tg_session_proxy = proxy_url
            return self._tg_session

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

    @staticmethod
    def _err_text(exc: BaseException) -> str:
        msg = str(exc).strip()
        return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__

    def _render(self, sender_html: str, text: str, body_html: str | None = None,
                reply_html: str | None = None) -> str:
        """Шаблон через replace, а не str.format: фигурные скобки в шаблоне
        не роняют модуль. body_html — текст с уже применённым форматированием."""
        inner = body_html if body_html is not None else (self._escape_html(text) if text else "")
        body = f"<blockquote>{inner}</blockquote>" if inner else ""
        if reply_html:
            body = f"{reply_html}\n{body}" if body else reply_html
        template = self.config["template"] or "<b>{sender}</b>\n{text}"
        return template.replace("{sender}", sender_html).replace("{text}", body).strip()

    def _text_budget(self, sender_html: str, limit: int, reply_html: str | None = None) -> int:
        """Сколько видимых символов текста влезет в лимит вместе с шаблоном."""
        overhead = len(self._strip_html(self._render(sender_html, "", reply_html=reply_html)))
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

    def _elements_to_html(self, text: str, elements) -> str | None:
        """Элементы форматирования MAX (смещения в UTF-16) -> HTML Telegram.
        Пересекающиеся (не вложенные) элементы отбрасываются: Telegram их не
        примет. None — форматирования нет."""
        if not text or not elements:
            return None
        units = text.encode("utf-16-le")
        n = len(units) // 2

        spans = []
        for el in elements:
            kind = _enum_str(_get(el, "type"))
            start = _get(el, "from_", "from")
            length = _get(el, "length")
            if start is None or not length:
                continue
            start = int(start)
            end = min(n, start + int(length))
            if start < 0 or start >= end:
                continue
            if kind == "LINK":
                url = _get(_get(el, "attributes"), "url")
                if not url:
                    continue
                spans.append((start, end, f'<a href="{html.escape(str(url), quote=True)}">', "</a>"))
            elif kind in ELEMENT_TAGS:
                spans.append((start, end, *ELEMENT_TAGS[kind]))
        if not spans:
            return None

        spans.sort(key=lambda s: (s[0], -s[1]))
        accepted = []
        for s in spans:
            if all(not (a[0] < s[0] < a[1] < s[1]) for a in accepted):
                accepted.append(s)

        bounds = sorted({0, n, *(s[0] for s in accepted), *(s[1] for s in accepted)})
        out, stack = [], []
        for i, pos in enumerate(bounds):
            while stack and stack[-1][1] <= pos:
                out.append(stack.pop()[3])
            for s in accepted:
                if s[0] == pos:
                    out.append(s[2])
                    stack.append(s)
            if i + 1 < len(bounds):
                seg = units[pos * 2: bounds[i + 1] * 2].decode("utf-16-le", errors="ignore")
                out.append(self._escape_html(seg))
        while stack:
            out.append(stack.pop()[3])
        return "".join(out)

    # ------------------------------------------------------------------ #
    # Разбор сообщения MAX
    # ------------------------------------------------------------------ #

    @staticmethod
    def _classify_attachment(att) -> str:
        type_name = _enum_str(_get(att, "type", "_type"))
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
            # В MAX video_type == 1 — кружок. Квадрат — запасной признак;
            # если такое видео не примется кружком, уйдёт обычным видео.
            if _get(att, "video_type") in (1, "1", "NOTE", "ROUND"):
                return "video_note"
            width, height = _get(att, "width"), _get(att, "height")
            if width and height and width == height:
                return "video_note"
            return "video"
        if "PHOTO" in combined or "IMAGE" in combined:
            return "photo"
        if "AUDIO" in combined:
            # В MAX аудио-вложение — это голосовое (музыка приходит файлом)
            return "voice"
        if "FILE" in combined or "DOCUMENT" in combined:
            return "document"
        return "skip"

    def _pick_thread_id(self, kinds: list, text: str = "") -> int:
        route = self._current_route()
        if (
            route.photo_thread
            and kinds == ["photo"]
            and len((text or "").strip()) <= self.config["single_photo_max_text_length"]
        ):
            return route.photo_thread
        return route.thread

    @staticmethod
    def _extract_attachment_urls(att) -> list:
        """Все ссылки-кандидаты из вложения, без дублей."""
        found = []

        def add(val):
            if val and isinstance(val, str) and val.startswith("http") and val not in found:
                found.append(val)

        for attr in ("url", "base_url", "base_raw_url", "src", "link", "photo_url", "video_url"):
            add(_get(att, attr))
        for nested_attr in ("payload", "video", "audio", "file", "media"):
            nested = _get(att, nested_attr)
            if nested is not None and not isinstance(nested, (str, bool, int)):
                for attr in ("url", "base_url", "base_raw_url", "src"):
                    add(_get(nested, attr))
        for el in _get(att, "elements") or []:
            add(_get(el, "url"))
        sizes = _get(att, "sizes", "photo_sizes")
        if sizes:
            try:
                add(_get(sizes[-1], "url"))
            except (IndexError, TypeError, KeyError):
                pass
        return found

    @staticmethod
    def _preview_bytes(att) -> bytes | None:
        """preview_data фото (bytes / base64 / data:-URI) — последний шанс."""
        raw = _get(att, "preview_data")
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
        if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" or data[8:12] == b"WEBP":
            return data
        return None

    @staticmethod
    def _ids_of(obj, default_chat_id=None):
        chat_id = _get(obj, "chat_id", "chatId")
        if chat_id is None:
            chat_id = default_chat_id
        message_id = _get(obj, "id", "message_id", "messageId", "mid")
        return chat_id, message_id

    @staticmethod
    def _payload_of(obj):
        text = _get(obj, "text")
        text = str(text) if text else ""
        attaches = _get(obj, "attaches", "attachments") or []
        try:
            attaches = [_normalize_attach(a) for a in attaches]
        except TypeError:
            attaches = []
        elements = _get(obj, "elements") or []
        return text, attaches, elements

    def _link_of(self, message):
        """Ссылка на связанное сообщение: (attr, link, inner, type)."""
        for link_attr in LINK_ATTRS:
            linked = getattr(message, link_attr, None)
            if linked is None or isinstance(linked, (str, bytes, int, float, bool)):
                continue
            inner = _as_message(_get(linked, "message"))
            link_type = _enum_str(_get(linked, "type", "_type"))
            if not link_type and link_attr in FORWARD_ATTRS:
                link_type = "FORWARD"
            if not link_type and link_attr in ("reply", "reply_to"):
                link_type = "REPLY"
            return link_attr, linked, inner, link_type
        return None

    def _reply_info(self, message):
        """(chat_id, message_id, inner) сообщения, на которое отвечают, или None."""
        found = self._link_of(message)
        if found is None or found[3] != "REPLY":
            return None
        _, linked, inner, _ = found
        msg_id = _get(linked, "message_id", "messageId") or _get(inner, "id")
        chat_id = _get(linked, "chat_id", "chatId") or getattr(message, "chat_id", None)
        if msg_id is None:
            return None
        return chat_id, msg_id, inner

    def _get_effective_content(self, message):
        """(text, attaches, elements, original_sender, id_pairs).

        Пересылка без комментария -> содержимое пересланного.
        Пересылка с комментарием -> комментарий + пересланное.
        Ответ -> только сам ответ (цитата отдельно, см. reply_threading).
        elements = None, если текст склеен из двух сообщений.
        """
        outer_ids = self._ids_of(message)
        text, attaches, elements = self._payload_of(message)

        found = self._link_of(message)
        fwd = None
        if found is not None and found[3] == "FORWARD" and found[2] is not None:
            _, linked, inner, _ = found
            f_text, f_attaches, f_elements = self._payload_of(inner)
            if f_text or f_attaches:
                linked_chat = _get(linked, "chat_id", "chatId")
                inner_ids = self._ids_of(inner, default_chat_id=linked_chat)
                pairs = [inner_ids]
                if linked_chat is not None and inner_ids[1] is not None and inner_ids[0] != linked_chat:
                    pairs.append((linked_chat, inner_ids[1]))
                sender = _get(inner, "sender") or _get(linked, "sender")
                fwd = (f_text, f_attaches, f_elements, sender, pairs)

        if fwd is None:
            return text, attaches, elements, None, [outer_ids]

        f_text, f_attaches, f_elements, f_sender, pairs = fwd
        if not (text or attaches):
            return f_text, f_attaches, f_elements, f_sender, [outer_ids] + pairs
        if f_text == text:
            return text, attaches, elements, f_sender, [outer_ids] + pairs
        combined = "\n\n".join(t for t in (text, f_text) if t)
        return combined, attaches + f_attaches, None, f_sender, [outer_ids] + pairs

    def _seen_before(self, message) -> bool:
        chat_id, message_id = self._ids_of(message)
        if message_id is None:
            return False
        key = self._map_key(chat_id, message_id)
        if key in self._seen:
            return True
        self._seen[key] = None
        while len(self._seen) > DEDUP_MAX:
            self._seen.popitem(last=False)
        return False

    # ------------------------------------------------------------------ #
    # Фильтры
    # ------------------------------------------------------------------ #

    def _csv(self, key: str) -> list:
        return [s.strip().lower() for s in re.split(r"[,;\n]+", self.config[key] or "") if s.strip()]

    def _filter_reason(self, sender_id, sender_name: str, text: str) -> str | None:
        ident = {str(sender_id).lower(), (sender_name or "").strip().lower()} - {"", "none"}
        only = self._csv("only_senders")
        if only and not ident & set(only):
            return "отправителя нет в only_senders"
        if ident & set(self._csv("ignore_senders")):
            return "отправитель в ignore_senders"
        low = (text or "").lower()
        for kw in self._csv("ignore_keywords"):
            if kw in low:
                return f"ключевое слово «{kw}»"
        return None

    # ------------------------------------------------------------------ #
    # Клиент MAX
    # ------------------------------------------------------------------ #

    async def _call_flexible(self, method, **kwargs):
        """Вызывает метод клиента, пробуя message_id как есть и как str/int."""
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
                "MaxToTgForwarder: вызов %s не удался: %r", getattr(method, "__name__", method), last_exc
            )
        return None

    async def _resolve_media_urls(self, att, kind: str, id_pairs) -> list:
        urls = self._extract_attachment_urls(att)
        api_url = await self._resolve_api_url(att, kind, id_pairs)
        if api_url:
            urls = [api_url] + [u for u in urls if u != api_url]
        return urls

    async def _resolve_api_url(self, att, kind: str, id_pairs):
        """У файлов, видео и кружков прямой ссылки нет: временную ссылку
        выдают client.get_file_by_id / client.get_video_by_id. У голосовых
        url обычно уже есть, иначе — тоже get_file_by_id по audio_id."""
        client = getattr(self, "client", None)
        if client is None:
            return None

        if kind == "document":
            method, id_key = getattr(client, "get_file_by_id", None), "file_id"
            media_id = _get(att, "file_id", "id")
        elif kind in ("video", "video_note"):
            method, id_key = getattr(client, "get_video_by_id", None), "video_id"
            media_id = _get(att, "video_id", "id")
        elif kind in ("voice", "audio") and not _get(att, "url"):
            method, id_key = getattr(client, "get_file_by_id", None), "file_id"
            media_id = _get(att, "audio_id", "file_id", "id")
        else:
            return None

        if method is None or media_id is None:
            return None

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
            url = _get(info, "url") if info is not None else None
            if url:
                return url
        return None

    # ------------------------------------------------------------------ #
    # Имя отправителя
    # ------------------------------------------------------------------ #

    @staticmethod
    def _name_from_obj(obj) -> str | None:
        if obj is None or isinstance(obj, (int, float, bool)):
            return None
        for attr in ("name", "display_name", "full_name", "title", "username"):
            val = _get(obj, attr)
            if val and isinstance(val, str):
                return val
        first, last = _get(obj, "first_name", "firstName"), _get(obj, "last_name", "lastName")
        if first or last:
            return " ".join(str(p) for p in (first, last) if p)
        names = _get(obj, "names")
        if names:
            try:
                entry = names[0]
                first = _get(entry, "first_name", "firstName")
                last = _get(entry, "last_name", "lastName")
                if first or last:
                    return " ".join(str(p) for p in (first, last) if p)
                n = _get(entry, "name")
                if n:
                    return str(n)
            except (IndexError, TypeError, KeyError):
                pass
        return None

    async def _fetch_user_name(self, user_id) -> str | None:
        """Через клиент PyMax. Сработавший метод дальше пробуется первым."""
        client = getattr(self, "client", None)
        if client is None:
            return None

        candidates = [
            ("get_cached_user", False), ("get_user", False), ("fetch_user", False),
            ("get_contact", False), ("get_users", True), ("fetch_users", True),
            ("get_contacts", True),
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
        if isinstance(sender, str) and not sender.strip().lstrip("-").isdigit():
            return sender

        user_id = sender
        if sender is not None and not isinstance(sender, (int, str)):
            name = self._name_from_obj(sender)
            if name:
                return name
            user_id = _get(sender, "id", "user_id")

        if user_id is None:
            return "MAX"
        if isinstance(user_id, str):
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

    @staticmethod
    def _result_ids(result) -> list:
        if isinstance(result, dict) and "message_id" in result:
            return [result["message_id"]]
        if isinstance(result, list):
            return [m["message_id"] for m in result if isinstance(m, dict) and "message_id" in m]
        return []

    def _collect(self, data: dict, result, role: str | None):
        collector = _COLLECT.get()
        if collector is None:
            return
        ids = self._result_ids(result)
        collector["ids"].extend(ids)
        if role and ids and collector.get(role) is None:
            collector[role] = ids[0]
        if "reply_parameters" in data:
            collector["reply_to"] = None

    async def _tg_call(self, method: str, data: dict, files: dict | None = None, role: str | None = None):
        """Возвращает (ok, error, result). role ('text'/'caption') — пометка
        для связки сообщений: какое сообщение несёт текст/подпись."""
        token = (self.config["bot_token"] or "").strip()
        if not token:
            return False, "не настроен bot_token (см. .tgfwdinfo)", None
        if "chat_id" in data and not str(data.get("chat_id") or "").strip():
            return False, "не настроен target_chat_id / routes (см. .tgfwdinfo)", None

        url = TELEGRAM_API_BASE.format(token=token, method=method)
        base_timeout = max(5, self.config["timeout"] or 30)
        timeout = aiohttp.ClientTimeout(total=base_timeout * (4 if files else 1), connect=15)

        proxy_url = self._proxy_url()
        http_proxy = proxy_url if proxy_url.startswith(("http://", "https://")) else None

        max_attempts = max(0, self.config["retries"] or 0) + 1
        data = {k: v for k, v in data.items() if v is not None}
        last_error = "неизвестная ошибка"

        attempt = flood_waits = 0
        thread_fixed = markup_fixed = False

        while attempt < max_attempts:
            attempt += 1
            result = status = None
            try:
                session = await self._get_telegram_session()
                if files:
                    form = aiohttp.FormData()
                    for key, value in data.items():
                        form.add_field(key, self._form_value(value))
                    for field_name, (filename, content, content_type) in files.items():
                        form.add_field(field_name, content, filename=filename, content_type=content_type)
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
                    "не удалось подключиться к api.telegram.org (таймаут). Возможно, "
                    "Telegram заблокирован в этой сети — задайте proxy_url"
                )
            except aiohttp.ClientConnectorError as exc:
                last_error = f"не удалось подключиться к api.telegram.org: {exc}. Проверьте сеть или proxy_url"
            except (aiohttp.ClientError, OSError) as exc:
                last_error = f"сетевая ошибка: {self._err_text(exc)}"
            except Exception as exc:  # noqa: BLE001
                logger.exception("MaxToTgForwarder: исключение при обращении к Telegram")
                self._last_error = self._mask(self._err_text(exc))
                return False, self._last_error, None
            else:
                if not isinstance(result, dict):
                    last_error = f"Telegram вернул не-JSON ответ (HTTP {status})"
                elif result.get("ok"):
                    self._collect(data, result.get("result"), role)
                    return True, None, result.get("result")
                else:
                    desc = str(result.get("description") or result)
                    desc_low = desc.lower()
                    code = result.get("error_code") or status or 0
                    params = result.get("parameters") or {}

                    if "message is not modified" in desc_low:
                        return True, None, None

                    if code == 429:
                        wait = int(params.get("retry_after") or 5)
                        if flood_waits < MAX_FLOOD_WAITS and wait <= MAX_FLOOD_WAIT_SEC:
                            flood_waits += 1
                            logger.warning("MaxToTgForwarder: flood-лимит Telegram, жду %sс", wait)
                            await asyncio.sleep(wait + 0.5)
                            attempt -= 1
                            continue
                        self._last_error = desc
                        return False, desc, None

                    if "thread not found" in desc_low and "message_thread_id" in data and not thread_fixed:
                        logger.warning(
                            "MaxToTgForwarder: ветка %s не найдена, шлю в общий чат", data["message_thread_id"]
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
                        self._last_error = desc
                        return False, desc, None

            if attempt < max_attempts:
                await asyncio.sleep(1.5 * attempt)

        last_error = self._mask(last_error)
        self._last_error = last_error
        logger.error("MaxToTgForwarder: %s: %s", method, last_error)
        return False, last_error, None

    def _base_payload(self, thread_id) -> dict:
        payload = {"chat_id": self._current_route().target}
        if thread_id:
            payload["message_thread_id"] = thread_id
        collector = _COLLECT.get()
        if collector is not None and collector.get("reply_to"):
            payload["reply_parameters"] = {
                "message_id": collector["reply_to"],
                "allow_sending_without_reply": True,
            }
        return payload

    async def _send_text_message(self, html_text: str, thread_id: int | None = None, role: str | None = None):
        if thread_id is None:
            thread_id = self._current_route().thread
        payload = self._base_payload(thread_id)
        payload.update(text=html_text, parse_mode="HTML", link_preview_options={"is_disabled": True})
        ok, error, _ = await self._tg_call("sendMessage", payload, role=role)
        return ok, error

    async def _send_long_text(self, sender: str, text: str, thread_id, body_html: str | None = None,
                              reply_html: str | None = None):
        """Текст с форматированием — одним сообщением, если влезает. Длиннее
        4096 — режется по абзацам (уже без форматирования)."""
        if body_html is not None:
            rendered = self._render(sender, text, body_html=body_html, reply_html=reply_html)
            if len(self._strip_html(rendered)) <= TEXT_LIMIT:
                return await self._send_text_message(rendered, thread_id, role="text")

        budget = self._text_budget(sender, TEXT_LIMIT, reply_html)
        chunks = self._split_text(text or "", budget) or [""]
        ok, error = await self._send_text_message(
            self._render(sender, chunks[0], reply_html=reply_html), thread_id, role="text"
        )
        if len(chunks) > 1:
            collector = _COLLECT.get()
            if collector is not None:
                collector["multi"] = True
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
        if "." in filename.rsplit("/", 1)[-1]:
            return filename
        return filename + DEFAULT_EXT.get(kind, "")

    @staticmethod
    def _new_item(att, kind: str, name=None, error=None) -> dict:
        return {
            "att": att, "kind": kind, "name": str(name) if name else None,
            "url": None, "data": None, "ct": "", "filename": None,
            "error": error, "url_ok": True, "skip": False, "is_preview": False,
        }

    def _use_preview(self, item: dict) -> dict:
        """Оригинал фото не скачался — берём превью из самого сообщения."""
        if item["kind"] != "photo":
            return item
        preview = self._preview_bytes(item["att"])
        if preview is None:
            return item
        logger.warning("MaxToTgForwarder: оригинал фото не скачался, есть превью из сообщения")
        item.update(
            data=preview,
            ct="image/png" if preview[:4] == b"\x89PNG" else "image/jpeg",
            filename="photo_preview.jpg",
            is_preview=True,
        )
        return item

    async def _prepare_item(self, att, kind: str, id_pairs) -> dict:
        """Получает ссылки и скачивает вложение. Ничего не отправляет."""
        item = self._new_item(att, kind, _get(att, "name", "file_name"))

        urls = await self._resolve_media_urls(att, kind, id_pairs)
        if not urls:
            logger.warning("MaxToTgForwarder: нет ссылки на вложение (%s): %r", kind, att)
            item["error"] = "в вложении нет ссылки на файл (пришлите вывод .tgfwddebug)"
            return self._use_preview(item)
        item["url"] = urls[0]

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
                kind, item["error"], ", ".join(u[:120] for u in urls),
            )
            return self._use_preview(item)

        ct_lower = (content_type or "").lower()
        head = data[:200].lstrip().lower()
        if "text/html" in ct_lower or head.startswith((b"<!doctype html", b"<html")):
            logger.warning("MaxToTgForwarder: по ссылке вложения (%s) пришла HTML-страница, пропускаю", kind)
            item["skip"] = True
            return item

        url = item["url"]
        filename = item["name"] or (url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1] or kind)
        item.update(
            data=data,
            ct=content_type or "application/octet-stream",
            filename=self._ensure_extension(filename, kind),
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
        # Если в заметке есть подпись, правка в MAX заменит её текстом (без заметки)
        await self._send_text_message(note, thread_id=thread_id, role="text" if caption else None)

    async def _upload(self, method, field, item, caption, thread_id, content_type=None):
        payload = self._base_payload(thread_id)
        with_caption = bool(caption) and method not in ("sendSticker", "sendVideoNote")
        if with_caption:
            payload["caption"] = caption
            payload["parse_mode"] = "HTML"
        files = {field: (item["filename"], item["data"], content_type or item["ct"])}
        ok, error, _ = await self._tg_call(method, payload, files=files, role="caption" if with_caption else None)
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
        ok, tg_err, _ = await self._tg_call(method, payload, role="caption" if caption else None)
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

        if item["is_preview"]:
            if await self._send_by_url(item, caption, thread_id):
                return True
            mark = "<i>(превью: оригинал фото не скачался)</i>"
            caption = f"{caption}\n{mark}" if caption else mark

        method, field = MEDIA_METHOD_MAP.get(kind, ("sendDocument", "document"))
        ok, error = await self._upload(method, field, item, caption, thread_id)
        if ok:
            return True

        if kind == "video_note":
            fallback = ("sendVideo", "video", "video/mp4")
        elif kind == "sticker" and item["ct"].startswith(("image/png", "image/jpeg")):
            fallback = ("sendPhoto", "photo", None)
        elif kind != "document":
            fallback = ("sendDocument", "document", None)
        else:
            fallback = None

        if fallback is not None:
            logger.warning("MaxToTgForwarder: %s не удался (%s), пробую %s", method, error, fallback[0])
            ok, error = await self._upload(fallback[0], fallback[1], item, caption, thread_id, content_type=fallback[2])
            if ok:
                return True

        logger.error("MaxToTgForwarder: не удалось переслать вложение (%s): %s", kind, error)
        item["error"] = error
        await self._send_failure_note(item, caption, thread_id)
        return False

    async def _send_album(self, items: list, caption: str | None, thread_id) -> bool:
        media, files = [], {}
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
        ok, error, _ = await self._tg_call("sendMediaGroup", payload, files=files, role="caption" if caption else None)
        if ok:
            return True

        logger.warning("MaxToTgForwarder: альбом не ушёл (%s), шлю по одному", error)
        all_ok = True
        for i, item in enumerate(items):
            sent = await self._send_item(item, caption if i == 0 else None, thread_id)
            all_ok = all_ok and sent
        return all_ok

    # ------------------------------------------------------------------ #
    # Новое сообщение (вызывается воркером)
    # ------------------------------------------------------------------ #

    async def _reply_target(self, message, collector: dict):
        """Ответ в MAX: если копия исходного сообщения есть в Telegram — отвечаем
        на неё. Если нет — возвращаем строку-цитату «↩️ Имя: текст…»."""
        if not self.config["reply_threading"]:
            return None
        info = self._reply_info(message)
        if info is None:
            return None
        r_chat, r_id, inner = info

        entry = self._map.get(self._map_key(r_chat, r_id))
        if entry and entry.get("ids") and str(entry.get("c")) == str(self._current_route().target):
            collector["reply_to"] = entry["ids"][0]
            return None

        if inner is None:
            return None
        q_text, q_attaches, _ = self._payload_of(inner)
        q_sender = _get(inner, "sender")
        q_name = await self._resolve_name(q_sender) if q_sender is not None else ""
        snippet = " ".join(q_text.split())
        if len(snippet) > 80:
            snippet = snippet[:80].rstrip() + "…"
        if not snippet and q_attaches:
            snippet = "[вложение]"
        if not (q_name or snippet):
            return None
        parts = [self._escape_html(q_name)] if q_name else []
        if snippet:
            parts.append(self._escape_html(snippet))
        return f"<i>↩️ {': '.join(parts)}</i>"

    async def _process_message(self, message):
        text, attaches, elements, original_sender, id_pairs = self._get_effective_content(message)

        items = []
        for att in attaches:
            kind = self._classify_attachment(att)
            if kind != "skip":
                items.append((att, kind))
        if not text and not items:
            return

        sender_id = getattr(message, "sender", None)
        own_name = await self._resolve_sender_name(message)
        reason = self._filter_reason(sender_id, own_name, text)
        if reason:
            self._stats["filtered"] += 1
            logger.info("MaxToTgForwarder: сообщение не переслано, фильтр: %s", reason)
            return

        sender_name = own_name
        if original_sender is not None:
            sender_name = f"{own_name} ↪️ {await self._resolve_name(original_sender)}"
        sender = self._escape_html(sender_name)

        kinds = [k for _, k in items]
        thread_id = self._pick_thread_id(kinds, text)
        body_html = (
            self._elements_to_html(text, elements)
            if self.config["format_text"] and elements
            else None
        )

        route = self._current_route()
        chat_id, msg_id = self._ids_of(message)
        logger.info(
            "MaxToTgForwarder: MAX %s -> TG %s: текст %d симв., вложения: %s, ветка %s",
            chat_id, route.target, len(text), ", ".join(kinds) or "нет", thread_id or "общая",
        )

        collector = {"ids": [], "text": None, "caption": None, "multi": False, "reply_to": None}
        token = _COLLECT.set(collector)
        try:
            reply_html = await self._reply_target(message, collector)
            ok = await self._send_content(sender, text, body_html, reply_html, items, id_pairs, thread_id)
        finally:
            _COLLECT.reset(token)

        if collector["ids"] and msg_id is not None:
            self._remember(
                self._map_key(chat_id, msg_id),
                {
                    "c": route.target,
                    "ids": collector["ids"],
                    "t": collector["text"],
                    "cap": collector["caption"],
                    "m": collector["multi"],
                    "h": _text_hash(text),
                    "s": sender,
                    "r": reply_html,
                },
            )

        self._stats["forwarded" if ok else "failed"] += 1
        if not ok:
            await self._alert(
                f"сообщение из чата MAX {chat_id} от {sender_name} переслано не полностью: "
                f"{self._last_error or 'см. логи'}"
            )

    async def _send_content(self, sender, text, body_html, reply_html, items, id_pairs, thread_id) -> bool:
        if not items or not self.config["forward_media"]:
            if items and not text:
                text, body_html = "[вложение, пересылка медиа выключена]", None
            ok, error = await self._send_long_text(sender, text, thread_id, body_html, reply_html)
            if not ok:
                logger.error("MaxToTgForwarder: не удалось переслать сообщение: %s", error)
            return ok

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
            if text or reply_html:
                ok, _ = await self._send_long_text(sender, text, thread_id, body_html, reply_html)
                return ok
            return True

        # Текст — подписью к первому медиа, если влезает в 1024 и у медиа
        # бывает подпись. Иначе — отдельным сообщением перед медиа.
        caption = None
        if prepared[0]["kind"] not in NO_CAPTION_KINDS:
            candidate = self._render(sender, text, body_html=body_html, reply_html=reply_html)
            if len(self._strip_html(candidate)) <= CAPTION_LIMIT:
                caption = candidate
        all_ok = True
        if caption is None:
            all_ok, _ = await self._send_long_text(sender, text, thread_id, body_html, reply_html)

        i = 0
        while i < len(prepared):
            j = i
            while (
                j < len(prepared)
                and j - i < ALBUM_MAX
                and prepared[j]["data"] is not None
                and not prepared[j]["is_preview"]
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
        return all_ok

    # ------------------------------------------------------------------ #
    # Правки и удаления
    # ------------------------------------------------------------------ #

    async def _process_edit(self, message):
        chat_id, msg_id = self._ids_of(message)
        key = self._map_key(chat_id, msg_id)
        entry = self._map.get(key)
        if not entry:
            self._stats["duplicates"] += 1
            return

        text, _, elements, _, _ = self._get_effective_content(message)
        new_hash = _text_hash(text)
        if entry.get("h") == new_hash:
            self._stats["duplicates"] += 1
            return
        if entry.get("m"):
            logger.info("MaxToTgForwarder: правка длинного (многочастного) сообщения %s не переносится", key)
            return

        body_html = self._elements_to_html(text, elements) if self.config["format_text"] else None
        rendered = self._render(entry.get("s") or "MAX", text, body_html=body_html, reply_html=entry.get("r"))
        visible = len(self._strip_html(rendered))

        if entry.get("t"):
            if visible > TEXT_LIMIT:
                logger.info("MaxToTgForwarder: правка %s длиннее 4096, не переношу", key)
                return
            ok, error, _ = await self._tg_call(
                "editMessageText",
                {
                    "chat_id": entry["c"], "message_id": entry["t"], "text": rendered,
                    "parse_mode": "HTML", "link_preview_options": {"is_disabled": True},
                },
            )
        elif entry.get("cap"):
            if visible > CAPTION_LIMIT:
                logger.info("MaxToTgForwarder: правка %s длиннее 1024 для подписи, не переношу", key)
                return
            ok, error, _ = await self._tg_call(
                "editMessageCaption",
                {"chat_id": entry["c"], "message_id": entry["cap"], "caption": rendered, "parse_mode": "HTML"},
            )
        else:
            return

        if ok:
            entry["h"] = new_hash
            self._remember(key, entry)
            self._stats["edited"] += 1
            logger.info("MaxToTgForwarder: правка %s перенесена в Telegram", key)
        else:
            logger.warning("MaxToTgForwarder: не удалось перенести правку %s: %s", key, error)

    async def _process_delete(self, chat_id, message_ids):
        by_chat: dict = {}
        for mid in message_ids:
            entry = self._map.pop(self._map_key(chat_id, mid), None)
            if entry and entry.get("ids"):
                by_chat.setdefault(str(entry["c"]), []).extend(entry["ids"])
        if not by_chat:
            return
        self._save_map()

        for tg_chat, ids in by_chat.items():
            for i in range(0, len(ids), 100):
                batch = ids[i:i + 100]
                ok, error, _ = await self._tg_call("deleteMessages", {"chat_id": tg_chat, "message_ids": batch})
                if ok:
                    self._stats["deleted"] += len(batch)
                else:
                    logger.warning(
                        "MaxToTgForwarder: не удалось удалить %s сообщ. в Telegram (бот удаляет только "
                        "свои и не старше 48ч): %s", len(batch), error,
                    )

    # ------------------------------------------------------------------ #
    # Watcher и сырые события
    # ------------------------------------------------------------------ #

    @loader.watcher("no_commands", "in")
    async def watcher(self, message):
        if not self.config["enabled"]:
            return

        chat_id = getattr(message, "chat_id", None)
        route = self._route_for(chat_id)
        if route is None:
            return

        status = _enum_str(getattr(message, "status", None))
        if status == "REMOVED":
            if self.config["sync_deletes"]:
                _, msg_id = self._ids_of(message)
                if msg_id is not None:
                    self._enqueue(("delete", route, chat_id, [msg_id]))
            return

        # Maxli отдаёт в watcher и правки (MESSAGE_EDIT) — с тем же id
        if self._seen_before(message) or status == "EDITED":
            if self.config["sync_edits"]:
                self._enqueue(("edit", route, message))
            else:
                self._stats["duplicates"] += 1
            return

        self._enqueue(("new", route, message))

    @loader.raw_handler("NOTIF_MSG_DELETE", "142")
    async def delete_raw_handler(self, frame):
        """Удаление сообщений в MAX (опкод NOTIF_MSG_DELETE)."""
        if not (self.config["enabled"] and self.config["sync_deletes"]):
            return
        payload = getattr(frame, "payload", None)
        if not isinstance(payload, dict):
            return

        chat_id = payload.get("chatId", payload.get("chat_id"))
        chat = payload.get("chat")
        if chat_id is None and isinstance(chat, dict):
            chat_id = chat.get("id")
        ids = list(payload.get("messageIds") or payload.get("message_ids") or [])
        msg = payload.get("message")
        if not ids and isinstance(msg, dict) and msg.get("id") is not None:
            ids = [msg["id"]]

        route = self._route_for(chat_id)
        if route is None or not ids:
            return
        self._enqueue(("delete", route, chat_id, ids))

    # ------------------------------------------------------------------ #
    # Команды
    # ------------------------------------------------------------------ #

    @staticmethod
    def _route_label(max_id, route: Route) -> str:
        label = f"MAX {max_id} → TG {route.target or '?'}"
        if route.thread:
            label += f" / ветка {route.thread}"
        if route.photo_thread:
            label += f" / фото → {route.photo_thread}"
        return label

    @loader.command()
    async def tgfwdcmd(self, message):
        """— включить/выключить пересылку MAX → Telegram"""
        self.config["enabled"] = not self.config["enabled"]
        state = "включена ✅" if self.config["enabled"] else "выключена ❌"
        await utils.answer(message, f"🔀 Пересылка в Telegram {state}")

    @loader.command()
    async def tgfwdtestcmd(self, message):
        """— тестовое сообщение во все маршруты (и в alert_chat_id)"""
        routes = self._routes() or {"—": self._current_route()}
        lines = []
        for max_id, route in routes.items():
            token = _ROUTE.set(route)
            try:
                ok, error = await self._send_text_message(
                    "<b>MaxToTgForwarder</b>\n<blockquote>✅ Тестовое сообщение</blockquote>"
                )
            finally:
                _ROUTE.reset(token)
            lines.append(f"{'✅' if ok else '❌'} {self._route_label(max_id, route)}" + ("" if ok else f": {error}"))

        alert_chat = str(self.config["alert_chat_id"] or "").strip()
        if alert_chat:
            ok, error, _ = await self._tg_call(
                "sendMessage", {"chat_id": alert_chat, "text": "🔔 MaxToTgForwarder: тест уведомлений о сбоях"}
            )
            lines.append(f"{'✅' if ok else '❌'} уведомления → {alert_chat}" + ("" if ok else f": {error}"))

        await utils.answer(message, "🔀 **MaxToTgForwarder test**\n" + utils.quote("\n".join(lines)))

    @loader.command()
    async def tgfwddebugcmd(self, message):
        """— (отладка) дамп sender, связей и вложений (в ответ на сообщение)"""
        target = message
        get_reply = getattr(utils, "get_reply", None)
        if get_reply is not None:
            try:
                replied = await get_reply(message)
                if replied is not None:
                    target = replied
            except Exception:  # noqa: BLE001
                pass

        sender = getattr(target, "sender", None)
        lines = [
            f"id: {getattr(target, 'id', None)}, chat_id: {getattr(target, 'chat_id', None)}, "
            f"status: {_enum_str(getattr(target, 'status', None)) or '—'}",
            f"sender: type={type(sender).__name__}, value={sender!r}",
        ]
        found = self._link_of(target)
        if found is not None:
            lines.append(f"\nсвязь '{found[0]}' типа {found[3] or '?'}: {found[1]!r}"[:1000])

        text, attaches, elements = self._payload_of(target)
        lines.append(f"текст: {len(text)} симв., элементов форматирования: {len(elements)}")
        lines.append(f"attaches: {len(attaches)} шт.")

        for i, att in enumerate(attaches):
            lines.append(f"\n--- вложение {i} ({att.__class__.__name__}) -> {self._classify_attachment(att)} ---")
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
            if dumped is None and isinstance(att, SimpleNamespace):
                dumped = vars(att)
            try:
                dump_text = json.dumps(dumped, ensure_ascii=False, default=str) if dumped is not None else repr(att)
            except Exception:  # noqa: BLE001
                dump_text = str(dumped)
            lines.append(dump_text[:1200])

        full_text = "\n".join(lines)
        if len(full_text) > 3000:
            full_text = full_text[:3000] + "\n…(обрезано)"
        await utils.answer(message, "🔧 **MaxToTgForwarder debug**\n" + utils.quote(full_text))

    @loader.command()
    async def tgfwdinfocmd(self, message):
        """— показать настройки и статистику пересылки"""
        cfg = self.config
        routes = self._routes()
        queue_size = self._queue.qsize() if self._queue is not None else 0
        worker_alive = self._worker is not None and not self._worker.done()
        st = self._stats

        def onoff(key):
            return "да" if cfg[key] else "нет"

        route_lines = "\n".join(f"  • {self._route_label(k, r)}" for k, r in routes.items()) or "  не заданы"
        filters = ", ".join(
            f"{name}: {cfg[name]}" for name in ("ignore_senders", "only_senders", "ignore_keywords") if cfg[name]
        ) or "нет"
        info = (
            f"версия: {MODULE_VERSION}\n"
            f"статус: {'включена' if cfg['enabled'] else 'выключена'}\n"
            f"маршруты:\n{route_lines}\n"
            f"медиа: {onoff('forward_media')}, форматирование: {onoff('format_text')}\n"
            f"ответы: {onoff('reply_threading')}, правки: {onoff('sync_edits')}, удаления: {onoff('sync_deletes')}\n"
            f"фильтры: {filters}\n"
            f"уведомления о сбоях: {cfg['alert_chat_id'] or 'выключены'}\n"
            f"токен бота: {'задан' if cfg['bot_token'] else 'не задан'}\n"
            f"прокси: {self._mask_proxy(self._proxy_url()) or 'не используется'}\n"
            f"таймаут: {cfg['timeout']}с, повторов: {cfg['retries']}\n"
            f"воркер: {'работает' if worker_alive else 'не запущен'}, в очереди: {queue_size}, "
            f"в связке: {len(self._map)} сообщ.\n"
            f"переслано: {st['forwarded']}, с ошибками: {st['failed']}, правок: {st['edited']}, "
            f"удалено: {st['deleted']}, отфильтровано: {st['filtered']}, дублей: {st['duplicates']}, "
            f"потеряно: {st['dropped']}"
        )
        await utils.answer(message, "🔀 **MaxToTgForwarder**\n" + utils.quote(info))
