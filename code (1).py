# meta developer: @claude
# meta pic: https://em-content.zobj.net/source/microsoft-teams/337/incoming-envelope_1f4e8.png
# requires: aiohttp
# min-maxli: 100

"""
MaxToTgForwarder — модуль для юзербота Maxli (https://github.com/YouRooni/Maxli)

Слушает выбранный чат в MAX и пересылает новые сообщения в Telegram
через Bot API (метод sendMessage), в конкретный чат/канал/ветку.

Настройка (после .loadmod):
  .config MaxToTgForwarder source_chat_id  <ID чата MAX>
  .config MaxToTgForwarder bot_token       <токен от @BotFather>
  .config MaxToTgForwarder target_chat_id  <ID чата Telegram>
  .config MaxToTgForwarder thread_id       <ID ветки, необязательно>

Как узнать ID:
  - ID чата MAX: команда .id, отправленная в нужном чате (модуль Messages).
  - chat_id Telegram: перешлите любое сообщение из нужного чата боту
    @RawDataBot / @userinfobot, там будет "chat":{"id": ...}. Для супергрупп
    и каналов id обычно отрицательный и начинается с -100.
  - thread_id (ID топика в форум-группе): правый клик по сообщению в топике
    в Telegram → Copy Message Link, число после "/c/.../<chat>/<thread_id>/...".

Команды:
  .tgfwd      — включить/выключить пересылку
  .tgfwdtest  — отправить тестовое сообщение в Telegram
  .tgfwdinfo  — показать текущие настройки
"""

import logging

import aiohttp

from maxli import loader, utils

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


@loader.tds
class MaxToTgForwarderMod(loader.Module):
    """Пересылает сообщения из выбранного чата MAX в Telegram через Bot API"""

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
                "📨 <b>{sender}</b>\n{text}",
                lambda: (
                    "Шаблон пересылаемого сообщения. Доступны {sender} и {text}. "
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
        )
        self._session: aiohttp.ClientSession | None = None

    async def client_ready(self):
        self._session = aiohttp.ClientSession()

    async def on_unload(self):
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _escape_html(text: str) -> str:
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    async def _send_to_telegram(self, text: str):
        token = self.config["bot_token"]
        chat_id = self.config["target_chat_id"]

        if not token or not chat_id:
            msg = "не настроен bot_token или target_chat_id (см. .tgfwdinfo)"
            logger.warning("MaxToTgForwarder: %s", msg)
            return False, msg

        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        thread_id = self.config["thread_id"]
        if thread_id:
            payload["message_thread_id"] = thread_id

        url = TELEGRAM_API_URL.format(token=token)

        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()

            timeout = aiohttp.ClientTimeout(total=15)
            async with self._session.post(url, json=payload, timeout=timeout) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200 or not data.get("ok"):
                    error = data.get("description", str(data))
                    logger.error("MaxToTgForwarder: ошибка Telegram API: %s", error)
                    return False, error
                return True, None
        except Exception as exc:  # noqa: BLE001
            logger.exception("MaxToTgForwarder: исключение при отправке в Telegram")
            return False, str(exc)

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
        attaches = getattr(message, "attaches", None) or getattr(message, "attachments", None)

        if not text and not attaches:
            return

        sender = getattr(message, "sender", None) or "MAX"
        sender = self._escape_html(str(sender))

        body = self._escape_html(text) if text else "<i>[сообщение без текста]</i>"

        if attaches:
            body += "\n\n📎 <i>в сообщении есть вложение (файл/фото/стикер), оно не пересылается</i>"

        formatted = self.config["template"].format(sender=sender, text=body)

        ok, error = await self._send_to_telegram(formatted)
        if not ok:
            logger.error("MaxToTgForwarder: не удалось переслать сообщение: %s", error)

    @loader.command(alias="tgfwd")
    async def tgfwdcmd(self, message):
        """— включить/выключить пересылку MAX → Telegram"""
        self.config["enabled"] = not self.config["enabled"]
        state = "включена ✅" if self.config["enabled"] else "выключена ❌"
        await utils.answer(message, f"🔀 Пересылка в Telegram {state}")

    @loader.command(alias="tgfwdtest")
    async def tgfwdtestcmd(self, message):
        """— отправить тестовое сообщение в Telegram"""
        ok, error = await self._send_to_telegram(
            "✅ Тестовое сообщение от Maxli (MaxToTgForwarder)"
        )
        if ok:
            await utils.answer(message, "✅ Тестовое сообщение успешно отправлено в Telegram")
        else:
            await utils.answer(message, f"❌ Не удалось отправить: {error}")

    @loader.command(alias="tgfwdinfo")
    async def tgfwdinfocmd(self, message):
        """— показать текущие настройки пересылки"""
        cfg = self.config
        info = (
            f"статус: {'включена' if cfg['enabled'] else 'выключена'}\n"
            f"чат MAX (source_chat_id): {cfg['source_chat_id'] or 'не задан'}\n"
            f"чат Telegram (target_chat_id): {cfg['target_chat_id'] or 'не задан'}\n"
            f"ветка (thread_id): {cfg['thread_id'] or 'нет'}\n"
            f"токен бота: {'задан' if cfg['bot_token'] else 'не задан'}"
        )
        await utils.answer(
            message,
            "🔀 **MaxToTgForwarder**\n" + utils.quote(info),
        )
