# meta developer: @claude
# meta pic: https://em-content.zobj.net/source/microsoft-teams/337/incoming-envelope_1f4e8.png
# requires: aiohttp
# min-maxli: 100

"""
MaxToTgForwarder — модуль для юзербота Maxli (https://github.com/YouRooni/Maxli)

Слушает выбранный чат в MAX и пересылает новые сообщения в Telegram через
Bot API: текст — как обычное сообщение (имя отправителя жирным, текст
цитатой), медиа (фото/видео/файлы/голосовые/кружки/стикеры) — соответствующим
методом (sendPhoto/sendVideo/sendDocument/sendVoice/sendVideoNote/sendSticker),
подпись оформлена так же (имя + цитата).

Настройка (после .loadmod):
  .config MaxToTgForwarder source_chat_id  <ID чата MAX>
  .config MaxToTgForwarder bot_token       <токен от @BotFather>
  .config MaxToTgForwarder target_chat_id  <ID чата Telegram>
  .config MaxToTgForwarder thread_id       <ID ветки, необязательно>
  .config MaxToTgForwarder proxy_url       <http://user:pass@ip:port или
                                             socks5://user:pass@ip:port, если
                                             Telegram недоступен напрямую>

Как узнать ID:
  - ID чата MAX: команда .id, отправленная в нужном чате.
  - chat_id Telegram: перешлите сообщение из нужного чата боту @RawDataBot.
  - thread_id: ID топика форум-группы (см. .tgfwdinfo / ссылку на сообщение
    в теме).

Команды:
  .tgfwd        — включить/выключить пересылку
  .tgfwdtest    — тестовое сообщение в Telegram
  .tgfwdinfo    — текущие настройки

Важные оговорки:
  - Точная структура вложений во внутреннем API MAX (через PyMax) нигде
    официально не задокументирована для всех типов, поэтому определение
    типа вложения (фото/видео/голос/кружок/стикер) сделано эвристически,
    по названию класса/поля type. Если для какого-то типа вложения
    пересылка не сработает — в логах Maxli будет запись с repr() объекта
    вложения, это поможет донастроить сопоставление.
  - Видео-стикеры и анимации Telegram не всегда можно переслать как
    sendSticker — в этом случае модуль автоматически пробует переслать
    как документ.
"""

import asyncio
import inspect
import json
import logging

import aiohttp

from maxli import loader, utils

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"

# Для socks5-прокси нужен пакет aiohttp_socks (pip install aiohttp_socks).
# http(s)-прокси работает и без него, через параметр aiohttp `proxy=`.
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


@loader.tds
class MaxToTgForwarderMod(loader.Module):
    """Пересылает сообщения (текст и медиа) из чата MAX в Telegram через Bot API"""

    strings = {"name": "MaxToTgForwarder"}

    def __init__(self):
        self.config = loader.ModuleConfig(
            loader.ConfigValue(
                "source_chat_id",
                0,
                lambda: (
                    "ID чата MAX, из которого нужно пересылать сообщения "
                    "(узнать: команда .id в нужном чате)"
                ),
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
                    "ID чата/канала Telegram, куда пересылать сообщения "
                    "(для супергрупп/каналов обычно начинается с -100)"
                ),
                validator=loader.validators.String(),
            ),
            loader.ConfigValue(
                "thread_id",
                0,
                lambda: (
                    "ID ветки (топика) в Telegram-группе с форумом. "
                    "0 — отправлять в общий чат без ветки"
                ),
                validator=loader.validators.Integer(),
            ),
            loader.ConfigValue(
                "template",
                "<b>{sender}</b>\n{text}",
                lambda: (
                    "Шаблон текстового сообщения/подписи к медиа. Доступны "
                    "{sender} и {text} (текст уже обёрнут в <blockquote>). "
                    "Поддерживается HTML-разметка Telegram"
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
                lambda: (
                    "Пересылать медиа (фото, видео, кружки, голосовые, стикеры, "
                    "файлы), а не только текст"
                ),
                validator=loader.validators.Boolean(),
            ),
            loader.ConfigValue(
                "resolve_names",
                True,
                lambda: (
                    "Пытаться получить имя отправителя через клиент MAX вместо "
                    "числового ID (если сервер MAX не отдаёт имя напрямую в "
                    "сообщении). Если не работает — проверьте .tgfwddebug"
                ),
                validator=loader.validators.Boolean(),
            ),
            loader.ConfigValue(
                "proxy_url",
                "",
                lambda: (
                    "Прокси для доступа к Telegram API, если он заблокирован "
                    "напрямую. Примеры: http://user:pass@ip:port, "
                    "socks5://user:pass@ip:port (нужен пакет aiohttp_socks). "
                    "Оставить пустым, если прямое подключение работает"
                ),
                validator=loader.validators.String(),
            ),
            loader.ConfigValue(
                "timeout",
                30,
                lambda: "Таймаут запроса к Telegram API в секундах",
                validator=loader.validators.Integer(),
            ),
            loader.ConfigValue(
                "retries",
                2,
                lambda: "Сколько раз повторить отправку при ошибке сети (0 — без повторов)",
                validator=loader.validators.Integer(),
            ),
        )

        self._download_session: aiohttp.ClientSession | None = None
        self._tg_session: aiohttp.ClientSession | None = None
        self._tg_session_proxy: str | None = None
        self._name_cache: dict = {}

    # ------------------------------------------------------------------ #
    # Жизненный цикл
    # ------------------------------------------------------------------ #

    async def client_ready(self):
        self._download_session = aiohttp.ClientSession()

    async def on_unload(self):
        for session in (self._download_session, self._tg_session):
            if session and not session.closed:
                await session.close()

    # ------------------------------------------------------------------ #
    # Сессии/прокси
    # ------------------------------------------------------------------ #

    async def _get_download_session(self) -> aiohttp.ClientSession:
        # Загрузка вложений из MAX всегда идёт напрямую, без прокси,
        # заданного для Telegram.
        if self._download_session is None or self._download_session.closed:
            self._download_session = aiohttp.ClientSession()
        return self._download_session

    async def _get_telegram_session(self) -> aiohttp.ClientSession:
        proxy_url = self.config["proxy_url"].strip()

        if proxy_url.startswith("socks4://") or proxy_url.startswith("socks5://"):
            if not _HAS_SOCKS:
                logger.error(
                    "MaxToTgForwarder: указан socks-прокси, но пакет aiohttp_socks "
                    "не установлен (pip install aiohttp_socks). Иду напрямую."
                )
                return await self._get_download_session()

            needs_new = (
                self._tg_session is None
                or self._tg_session.closed
                or proxy_url != self._tg_session_proxy
            )
            if needs_new:
                if self._tg_session and not self._tg_session.closed:
                    await self._tg_session.close()
                connector = ProxyConnector.from_url(proxy_url)
                self._tg_session = aiohttp.ClientSession(connector=connector)
                self._tg_session_proxy = proxy_url
            return self._tg_session

        # http(s)-прокси или отсутствие прокси — используем обычную сессию,
        # прокси передаётся через параметр `proxy=` в самом запросе.
        return await self._get_download_session()

    # ------------------------------------------------------------------ #
    # Вспомогательное
    # ------------------------------------------------------------------ #

    @staticmethod
    def _escape_html(text: str) -> str:
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    def _format_text(self, sender: str, text: str, limit: int = 900) -> str:
        if text and len(text) > limit:
            text = text[:limit].rstrip() + "…"
        body = self._escape_html(text) if text else ""
        wrapped = f"<blockquote>{body}</blockquote>" if body else ""
        return self.config["template"].format(sender=sender, text=wrapped)

    @staticmethod
    def _classify_attachment(att) -> str:
        type_val = getattr(att, "type", None)
        type_name = str(getattr(type_val, "value", type_val) or "").upper()
        cls_name = att.__class__.__name__.upper()
        combined = f"{type_name} {cls_name}"

        if "STICKER" in combined:
            return "sticker"
        if "VIDEO" in combined and any(
            k in combined for k in ("NOTE", "ROUND", "CIRCLE")
        ):
            return "video_note"
        if "VOICE" in combined:
            return "voice"
        if "VIDEO" in combined:
            kind = "video"
            # Круглые видеосообщения в MAX почти всегда квадратные (width == height),
            # обычные видео — нет. Явного признака "это кружок" в имени класса может
            # не быть, поэтому подстраховываемся размерами.
            width = getattr(att, "width", None)
            height = getattr(att, "height", None)
            if width and height and width == height:
                kind = "video_note"
            return kind
        if "PHOTO" in combined or "IMAGE" in combined:
            return "photo"
        if "AUDIO" in combined:
            # У PyMax голосовые и обычные аудио вполне могут приходить одним и тем
            # же типом вложения без явного флага "voice" — дальше в _forward_attachment
            # это дополнительно уточняется по расширению/Content-Type скачанного файла.
            is_voice = getattr(att, "is_voice", None) or getattr(att, "voice", None)
            return "voice" if is_voice else "audio"
        if "FILE" in combined or "DOCUMENT" in combined:
            return "document"
        return "document"

    @staticmethod
    def _extract_attachment_url(att):
        candidates = []

        for attr in ("url", "base_url", "src", "link", "photo_url", "video_url"):
            val = getattr(att, attr, None)
            if val:
                candidates.append(val)

        payload = getattr(att, "payload", None)
        if payload is not None:
            for attr in ("url", "base_url", "src"):
                val = getattr(payload, attr, None)
                if val:
                    candidates.append(val)

        # Некоторые вложения (в т.ч. видео/кружки) могут хранить фактический
        # медиа-объект во вложенном поле, а не в самом Attachment
        for nested_attr in ("video", "audio", "file", "media"):
            nested = getattr(att, nested_attr, None)
            if nested is not None:
                for attr in ("url", "base_url", "src"):
                    val = getattr(nested, attr, None)
                    if val:
                        candidates.append(val)

        elements = getattr(att, "elements", None) or []
        for el in elements:
            val = getattr(el, "url", None)
            if val:
                candidates.append(val)

        sizes = getattr(att, "sizes", None) or getattr(att, "photo_sizes", None)
        if sizes:
            try:
                biggest = sizes[-1]
                val = getattr(biggest, "url", None)
                if val:
                    candidates.append(val)
            except (IndexError, TypeError):
                pass

        return candidates[0] if candidates else None

    # ------------------------------------------------------------------ #
    # Имя отправителя
    # ------------------------------------------------------------------ #

    @staticmethod
    def _name_from_obj(obj) -> str | None:
        """Пытается вытащить человеко-читаемое имя из произвольного объекта
        пользователя/контакта, названия полей у которого заранее неизвестны."""
        if obj is None:
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
        """Best-effort резолв имени пользователя через клиент PyMax.
        Точное имя метода в публичной документации PyMax не зафиксировано,
        поэтому перебираем несколько вероятных вариантов. Если ни один не
        сработал — используйте .tgfwddebug, чтобы найти реальное имя метода
        и сообщить о нём."""
        client = getattr(self, "client", None)
        if client is None:
            return None

        single_methods = ("get_user", "fetch_user", "get_contact", "fetch_contact")
        batch_methods = ("get_users", "get_contacts", "fetch_users")

        for method_name in single_methods:
            method = getattr(client, method_name, None)
            if method is None:
                continue
            try:
                result = method(user_id)
                if inspect.isawaitable(result):
                    result = await result
            except Exception:  # noqa: BLE001
                continue

            name = self._name_from_obj(result)
            if name:
                return name

        for method_name in batch_methods:
            method = getattr(client, method_name, None)
            if method is None:
                continue
            try:
                result = method([user_id])
                if inspect.isawaitable(result):
                    result = await result
            except Exception:  # noqa: BLE001
                continue

            if result:
                try:
                    first_item = result[0]
                except (TypeError, IndexError, KeyError):
                    first_item = result
                name = self._name_from_obj(first_item)
                if name:
                    return name

        return None

    async def _resolve_sender_name(self, message) -> str:
        sender = getattr(message, "sender", None)

        # Готовое непустое строковое имя (не просто цифры) — используем как есть
        if isinstance(sender, str) and not sender.strip().isdigit():
            return sender

        user_id = sender
        if sender is not None and not isinstance(sender, (int, str)):
            name = self._name_from_obj(sender)
            if name:
                return name
            user_id = getattr(sender, "id", None) or getattr(sender, "user_id", None)

        # Иногда готовое имя может лежать прямо в самом сообщении
        for attr in ("sender_name", "from_name", "author_name", "user_name"):
            val = getattr(message, attr, None)
            if val:
                return str(val)

        if user_id is None:
            return "MAX"

        if user_id in self._name_cache:
            return self._name_cache[user_id]

        if not self.config["resolve_names"]:
            return str(user_id)

        name = await self._fetch_user_name(user_id)
        if name:
            self._name_cache[user_id] = name
            return name

        return str(user_id)

    # ------------------------------------------------------------------ #
    # Telegram API
    # ------------------------------------------------------------------ #

    async def _tg_call(self, method: str, data: dict, files: dict | None = None):
        token = self.config["bot_token"]
        if not token:
            return False, "не настроен bot_token (см. .tgfwdinfo)"

        if not self.config["target_chat_id"]:
            return False, "не настроен target_chat_id (см. .tgfwdinfo)"

        url = TELEGRAM_API_BASE.format(token=token, method=method)
        timeout = aiohttp.ClientTimeout(total=self.config["timeout"] or 30)

        proxy_url = self.config["proxy_url"].strip()
        http_proxy = proxy_url if proxy_url.startswith("http") else None

        retries = max(0, self.config["retries"])
        last_error = "неизвестная ошибка"

        clean_data = {k: v for k, v in data.items() if v is not None}

        for attempt in range(retries + 1):
            try:
                session = await self._get_telegram_session()

                if files:
                    form = aiohttp.FormData()
                    for key, value in clean_data.items():
                        form.add_field(key, str(value))
                    for field_name, (filename, content, content_type) in files.items():
                        form.add_field(
                            field_name,
                            content,
                            filename=filename,
                            content_type=content_type,
                        )
                    request_ctx = session.post(
                        url, data=form, timeout=timeout, proxy=http_proxy
                    )
                else:
                    request_ctx = session.post(
                        url, json=clean_data, timeout=timeout, proxy=http_proxy
                    )

                async with request_ctx as resp:
                    result = await resp.json(content_type=None)
                    if not result.get("ok"):
                        error = result.get("description", str(result))
                        return False, error
                    return True, None

            except (asyncio.TimeoutError, TimeoutError):
                last_error = (
                    "не удалось подключиться к api.telegram.org (таймаут). "
                    "Возможно, Telegram заблокирован в этой сети — задайте "
                    "proxy_url (.config MaxToTgForwarder proxy_url http://... "
                    "или socks5://...)"
                )
            except aiohttp.ClientConnectorError as exc:
                last_error = (
                    f"не удалось подключиться к api.telegram.org: {exc}. "
                    "Проверьте интернет/DNS или задайте proxy_url"
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("MaxToTgForwarder: исключение при обращении к Telegram")
                last_error = str(exc)
                break

            if attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))

        logger.error("MaxToTgForwarder: %s", last_error)
        return False, last_error

    async def _send_text_message(self, html_text: str):
        payload = {
            "chat_id": self.config["target_chat_id"],
            "text": html_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        thread_id = self.config["thread_id"]
        if thread_id:
            payload["message_thread_id"] = thread_id
        return await self._tg_call("sendMessage", payload)

    async def _download(self, url: str):
        session = await self._get_download_session()
        timeout = aiohttp.ClientTimeout(total=60)
        async with session.get(url, timeout=timeout) as resp:
            resp.raise_for_status()
            data = await resp.read()
            content_type = resp.headers.get("Content-Type", "")
            return data, content_type

    # ------------------------------------------------------------------ #
    # Пересылка вложений
    # ------------------------------------------------------------------ #

    async def _forward_attachment(self, att, sender: str, text: str):
        kind = self._classify_attachment(att)
        url = self._extract_attachment_url(att)
        caption = self._format_text(sender, text, limit=900) if text else f"<b>{sender}</b>"

        if not url:
            logger.warning(
                "MaxToTgForwarder: не удалось получить URL вложения (%s): %r",
                kind,
                att,
            )
            note = (
                f"{caption}\n\n📎 <i>вложение ({self._escape_html(kind)}), "
                "не удалось получить ссылку на файл</i>"
            )
            await self._send_text_message(note)
            return

        try:
            data, content_type = await self._download(url)
        except Exception as exc:  # noqa: BLE001
            logger.exception("MaxToTgForwarder: ошибка загрузки вложения %s", url)
            note = (
                f"{caption}\n\n📎 <i>не удалось загрузить вложение: "
                f"{self._escape_html(str(exc))}</i>"
            )
            await self._send_text_message(note)
            return

        filename = url.split("/")[-1].split("?")[0] or f"{kind}.bin"

        # Уточняем voice/audio по факту скачанного файла: голосовые в MAX — это
        # почти всегда OGG/Opus, обычные аудио — как правило mp3/m4a и т.п.
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        ct = (content_type or "").lower()
        if kind == "audio" and (
            ext in ("ogg", "oga", "opus") or "ogg" in ct or "opus" in ct
        ):
            kind = "voice"

        method, field = MEDIA_METHOD_MAP.get(kind, ("sendDocument", "document"))

        payload = {"chat_id": self.config["target_chat_id"]}
        thread_id = self.config["thread_id"]
        if thread_id:
            payload["message_thread_id"] = thread_id

        # sendVideoNote в Bot API не поддерживает caption
        if method != "sendVideoNote":
            payload["caption"] = caption
            payload["parse_mode"] = "HTML"

        files = {field: (filename, data, content_type or "application/octet-stream")}

        ok, error = await self._tg_call(method, payload, files=files)

        if not ok and method == "sendSticker":
            logger.warning(
                "MaxToTgForwarder: sendSticker не удался (%s), пробую как документ",
                error,
            )
            fallback_payload = {
                "chat_id": self.config["target_chat_id"],
                "caption": caption,
                "parse_mode": "HTML",
            }
            if thread_id:
                fallback_payload["message_thread_id"] = thread_id
            ok, error = await self._tg_call(
                "sendDocument",
                fallback_payload,
                files={"document": (filename, data, content_type or "application/octet-stream")},
            )

        if not ok and method == "sendVideoNote":
            logger.warning(
                "MaxToTgForwarder: sendVideoNote не удался (%s), пробую как видео",
                error,
            )
            fallback_payload = {
                "chat_id": self.config["target_chat_id"],
                "caption": caption,
                "parse_mode": "HTML",
            }
            if thread_id:
                fallback_payload["message_thread_id"] = thread_id
            ok, error = await self._tg_call(
                "sendVideo",
                fallback_payload,
                files={"video": (filename, data, content_type or "video/mp4")},
            )

        if not ok:
            logger.error(
                "MaxToTgForwarder: не удалось переслать вложение (%s): %s", kind, error
            )
            note = (
                f"{caption}\n\n📎 <i>не удалось переслать вложение "
                f"({self._escape_html(kind)}): {self._escape_html(str(error))}</i>"
            )
            await self._send_text_message(note)

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

        try:
            same_chat = int(message.chat_id) == int(source_chat_id)
        except (TypeError, ValueError):
            same_chat = message.chat_id == source_chat_id

        if not same_chat:
            return

        text = message.text or ""
        attaches = (
            getattr(message, "attaches", None)
            or getattr(message, "attachments", None)
            or []
        )

        if not text and not attaches:
            return

        sender_name = await self._resolve_sender_name(message)
        sender = self._escape_html(sender_name)

        if attaches and self.config["forward_media"]:
            for att in attaches:
                try:
                    await self._forward_attachment(att, sender, text)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "MaxToTgForwarder: необработанная ошибка при пересылке вложения: %r",
                        att,
                    )
            return

        if attaches and not self.config["forward_media"]:
            text = text or "[вложение, пересылка медиа выключена]"

        formatted = self._format_text(sender, text, limit=3500)
        ok, error = await self._send_text_message(formatted)
        if not ok:
            logger.error("MaxToTgForwarder: не удалось переслать сообщение: %s", error)

    # ------------------------------------------------------------------ #
    # Команды
    # ------------------------------------------------------------------ #

    @loader.command(alias="tgfwd")
    async def tgfwdcmd(self, message):
        """— включить/выключить пересылку MAX → Telegram"""
        self.config["enabled"] = not self.config["enabled"]
        state = "включена ✅" if self.config["enabled"] else "выключена ❌"
        await utils.answer(message, f"🔀 Пересылка в Telegram {state}")

    @loader.command(alias="tgfwdtest")
    async def tgfwdtestcmd(self, message):
        """— отправить тестовое сообщение в Telegram"""
        ok, error = await self._send_text_message(
            "<b>MaxToTgForwarder</b>\n<blockquote>✅ Тестовое сообщение</blockquote>"
        )
        if ok:
            await utils.answer(message, "✅ Тестовое сообщение успешно отправлено в Telegram")
        else:
            await utils.answer(message, f"❌ Не удалось отправить: {error}")

    @loader.command(alias="tgfwddebug")
    async def tgfwddebugcmd(self, message):
        """— (отладка) дамп полей sender и вложений сообщения (используй в ответ на сообщение с кружком/гс/стикером)"""
        target = message
        get_reply = getattr(message, "get_reply_message", None)
        if get_reply is not None:
            try:
                replied = await get_reply()
                if replied is not None:
                    target = replied
            except Exception:  # noqa: BLE001
                pass

        lines = []

        sender = getattr(target, "sender", None)
        lines.append(f"sender: type={type(sender).__name__}, value={sender!r}")

        attaches = (
            getattr(target, "attaches", None)
            or getattr(target, "attachments", None)
            or []
        )
        lines.append(f"attaches: {len(attaches)} шт.")

        for i, att in enumerate(attaches):
            lines.append(f"\n--- вложение {i} ({att.__class__.__name__}) ---")
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
                    text = json.dumps(dumped, ensure_ascii=False, default=str, indent=None)
                except Exception:  # noqa: BLE001
                    text = str(dumped)
            else:
                text = repr(att)

            lines.append(text[:1200])

        full_text = "\n".join(lines) or "нет данных"
        if len(full_text) > 3000:
            full_text = full_text[:3000] + "\n…(обрезано)"

        await utils.answer(
            message,
            "🔧 **MaxToTgForwarder debug**\n" + utils.quote(full_text),
        )

    @loader.command(alias="tgfwdinfo")
    async def tgfwdinfocmd(self, message):
        """— показать текущие настройки пересылки"""
        cfg = self.config
        info = (
            f"статус: {'включена' if cfg['enabled'] else 'выключена'}\n"
            f"медиа: {'да' if cfg['forward_media'] else 'нет, только текст'}\n"
            f"чат MAX (source_chat_id): {cfg['source_chat_id'] or 'не задан'}\n"
            f"чат Telegram (target_chat_id): {cfg['target_chat_id'] or 'не задан'}\n"
            f"ветка (thread_id): {cfg['thread_id'] or 'нет'}\n"
            f"токен бота: {'задан' if cfg['bot_token'] else 'не задан'}\n"
            f"прокси: {cfg['proxy_url'] or 'не используется'}\n"
            f"таймаут: {cfg['timeout']}с, повторов: {cfg['retries']}"
        )
        await utils.answer(
            message,
            "🔀 **MaxToTgForwarder**\n" + utils.quote(info),
          )
