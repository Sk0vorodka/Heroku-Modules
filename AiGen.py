"""
    🤖 AIGen - генератор и фиксатор модулей/плагинов через AI API

    Модуль генерирует и исправляет Heroku/Hikka-модули и ExteraGram-плагины
    через совместимый AI API. Все настройки провайдера и прокси вынесены
    в конфиг модуля, без отдельных команд управления.

    Возможности:
    - генерация модулей по описанию;
    - исправление .py модулей по реплаю;
    - генерация .plugin для ExteraGram;
    - исправление .plugin по реплаю;
    - выбор модели через inline-меню;
    - кастомный API provider URL через конфиг;
    - прокси через конфиг: HTTP/HTTPS/SOCKS4/SOCKS5;
    - автоматическая нормализация endpoint'ов;
    - краткий changelog после фикса.
"""

__version__ = (1, 2, 1)

# meta developer: @Sk0lovek_plugins
# meta name: AIGen
# meta version: 1.2.1
# scope: hikka_only
# requires: aiohttp aiohttp_socks

import ast
import difflib
import html
import io
import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from aiohttp_socks import ProxyConnector

from .. import loader, utils
from herokutl.types import Message

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER_URL = "https://api.onlysq.ru/ai/v2"
DEFAULT_MODELS_URLS = (
    "https://api.onlysq.ru/ai/v2/models",
    "https://api.onlysq.ru/ai/models",
)
FORBIDDEN_COMMANDS = {
    "help",
    "ping",
    "info",
    "id",
    "dl",
    "exec",
    "eval",
    "term",
    "sh",
    "restart",
    "update",
    "alias",
    "modules",
    "load",
    "unload",
}
MAX_CONTEXT_BYTES = 350_000
MAX_PREVIEW_DESCRIPTION = 140


@loader.tds
class AiGenMod(loader.Module):
    """Генерация и исправление модулей/плагинов через AI API"""

    strings = {
        "name": "AiGen",
        "no_args": "❌ Введите описание.",
        "no_reply_py": "❌ Сделай реплай на файл .py.",
        "no_reply_plugin": "❌ Сделай реплай на .plugin или сообщение с кодом.",
        "thinking": "🧠 Думаю ({})...",
        "analyzing_module": "🧩 Анализирую код...",
        "analyzing_plugin": "🧩 Анализирую .plugin...",
        "api_error": "❌ Ошибка API:\n{}",
        "read_error": "❌ Ошибка чтения файла: {}",
        "empty_code": "❌ Не удалось прочитать код.",
        "empty_plugin": "❌ Не удалось прочитать .plugin.",
        "module_ready": "✅ Модуль готов!\n🧩 Модель: <code>{}</code>",
        "module_fixed": "✅ Исправлено!",
        "plugin_ready": "✅ Плагин создан!\n🧩 Модель: <code>{}</code>",
        "plugin_fixed": "✅ Плагин исправлён!",
        "changelog": "Changelog",
        "models_loading": "🔄 Загружаю список моделей...",
        "models_error": "❌ Ошибка загрузки списка моделей.",
        "models_title": "🤖 Доступные модели",
        "current_model": "🧠 Текущая: <code>{}</code>",
        "page": "📄 Стр {}/{}",
        "close": "❌ Закрыть",
        "select_model": "Выбрать {}",
        "model_set": "✅ Установлена: {}",
        "proxy_invalid": "⚠️ Прокси из конфига некорректен, использую прямое подключение",
    }

    strings_ru = {
        "no_args": "❌ Введите описание.",
        "no_reply_py": "❌ Сделай реплай на файл .py.",
        "no_reply_plugin": "❌ Сделай реплай на .plugin или сообщение с кодом.",
        "thinking": "🧠 Думаю ({})...",
        "analyzing_module": "🧩 Анализирую код...",
        "analyzing_plugin": "🧩 Анализирую .plugin...",
        "api_error": "❌ Ошибка API:\n{}",
        "read_error": "❌ Ошибка чтения файла: {}",
        "empty_code": "❌ Не удалось прочитать код.",
        "empty_plugin": "❌ Не удалось прочитать .plugin.",
        "module_ready": "✅ Модуль готов!\n🧩 Модель: <code>{}</code>",
        "module_fixed": "✅ Исправлено!",
        "plugin_ready": "✅ Плагин создан!\n🧩 Модель: <code>{}</code>",
        "plugin_fixed": "✅ Плагин исправлён!",
        "changelog": "Changelog",
        "models_loading": "🔄 Загружаю список моделей...",
        "models_error": "❌ Ошибка загрузки списка моделей.",
        "models_title": "🤖 Доступные модели",
        "current_model": "🧠 Текущая: <code>{}</code>",
        "page": "📄 Стр {}/{}",
        "close": "❌ Закрыть",
        "select_model": "Выбрать {}",
        "model_set": "✅ Установлена: {}",
        "proxy_invalid": "⚠️ Прокси из конфига некорректен, использую прямое подключение",
    }

    def __init__(self):
        self.config = loader.ModuleConfig(
            loader.ConfigValue(
                "API_KEY",
                "openai",
                lambda: "API ключ провайдера",
                validator=loader.validators.Hidden(loader.validators.String()),
            ),
            loader.ConfigValue(
                "CURRENT_MODEL",
                "gpt-5",
                lambda: "Модель по умолчанию",
                validator=loader.validators.String(),
            ),
            loader.ConfigValue(
                "MAX_TOKENS",
                8000,
                lambda: "Максимум токенов в ответе",
                validator=loader.validators.Integer(minimum=256, maximum=64000),
            ),
            loader.ConfigValue(
                "PROVIDER_URL",
                DEFAULT_PROVIDER_URL,
                lambda: (
                    "Базовый URL AI провайдера. "
                    "Можно указать /ai/v2, /v1 или полный /chat/completions"
                ),
                validator=loader.validators.String(),
            ),
            loader.ConfigValue(
                "MODELS_URL",
                "",
                lambda: (
                    "URL для списка моделей. Если пусто, модуль попытается "
                    "определить автоматически"
                ),
                validator=loader.validators.String(),
            ),
            loader.ConfigValue(
                "REQUEST_TIMEOUT",
                300,
                lambda: "Таймаут запросов к API в секундах",
                validator=loader.validators.Integer(minimum=10, maximum=3600),
            ),
            loader.ConfigValue(
                "USE_PROXY",
                False,
                lambda: "Использовать прокси для API запросов",
                validator=loader.validators.Boolean(),
            ),
            loader.ConfigValue(
                "PROXY_URL",
                "",
                lambda: (
                    "URL прокси. Поддерживаются http:// https:// socks4:// "
                    "socks5:// socks5h:// и другие совместимые схемы"
                ),
                validator=loader.validators.String(),
            ),
            loader.ConfigValue(
                "VERIFY_SSL",
                True,
                lambda: "Проверять SSL сертификаты",
                validator=loader.validators.Boolean(),
            ),
            loader.ConfigValue(
                "EXTRA_HEADERS",
                "",
                lambda: (
                    "Дополнительные HTTP заголовки в формате "
                    "Header1: value1|Header2: value2"
                ),
                validator=loader.validators.String(),
            ),
        )
        self._models_cache: List[Dict[str, Any]] = []
        self._models_per_page = 6
        self._client = None
        self._db = None

    async def client_ready(self, client, db):
        """Инициализация модуля"""
        self._client = client
        self._db = db

    # ---------------------------------------------------------
    # Команды
    # ---------------------------------------------------------

    @loader.command(
        ru_doc="<описание> - Сгенерировать модуль по описанию. Можно прикрепить файл к команде"
    )
    async def genmodcmd(self, message: Message):
        """Generate module by prompt"""
        args = utils.get_args_raw(message)
        if not args:
            await utils.answer(message, self.strings("no_args"))
            return

        status = await utils.answer(
            message,
            self.strings("thinking").format(
                html.escape(str(self.config["CURRENT_MODEL"]))
            ),
        )

        attached_text = await self._read_attached_text_from_message(message)
        system_prompt = self._build_module_generation_system_prompt()
        user_prompt = self._build_module_generation_user_prompt(args, attached_text)

        code = await self._api_request(system_prompt, user_prompt)
        code = self._postprocess_code(code, plugin=False)

        if code.startswith("ERROR:"):
            await utils.answer(status, self.strings("api_error").format(utils.escape_html(code)))
            return

        file = io.BytesIO(code.encode("utf-8"))
        file.name = f"mod_{utils.rand(4)}.py"

        await self._client.send_file(
            message.chat_id,
            file,
            caption=self.strings("module_ready").format(
                html.escape(str(self.config["CURRENT_MODEL"]))
            ),
            reply_to=message.id,
        )
        await status.delete()

    @loader.command(
        ru_doc="<описание> - Исправить модуль из реплая на .py. Можно прикрепить файл контекста к команде"
    )
    async def fixmodcmd(self, message: Message):
        """Fix python module from reply"""
        reply = await message.get_reply_message()
        args = utils.get_args_raw(message) or "Исправь синтаксические, архитектурные и логические ошибки"

        if not reply:
            await utils.answer(message, self.strings("no_reply_py"))
            return

        status = await utils.answer(message, self.strings("analyzing_module"))

        try:
            code_content = await self._read_code_from_reply(reply)
        except Exception as e:
            await utils.answer(status, self.strings("read_error").format(utils.escape_html(str(e))))
            return

        if not code_content:
            await utils.answer(status, self.strings("empty_code"))
            return

        attached_text = await self._read_attached_text_from_message(message)
        system_prompt = self._build_module_fix_system_prompt()
        user_prompt = self._build_module_fix_user_prompt(args, attached_text, code_content)

        fixed_code = await self._api_request(system_prompt, user_prompt)
        fixed_code = self._postprocess_code(fixed_code, plugin=False)

        if fixed_code.startswith("ERROR:"):
            await utils.answer(status, self.strings("api_error").format(utils.escape_html(fixed_code)))
            return

        file = io.BytesIO(fixed_code.encode("utf-8"))
        file.name = "fixed_module.py"

        changelog = self._build_changelog(code_content, fixed_code)
        caption = f"<b>{self.strings('module_fixed')}</b>"
        if changelog:
            caption += (
                f"\n\n<b>{self.strings('changelog')}</b>:\n"
                f"<blockquote><span class=\"tg-spoiler\">{changelog}</span></blockquote>"
            )

        await self._client.send_file(
            message.chat_id,
            file,
            caption=caption,
            reply_to=message.id,
        )
        await status.delete()

    @loader.command(
        ru_doc="<описание> - Сгенерировать exteraGram .plugin по описанию. Можно прикрепить файл к команде"
    )
    async def genplugcmd(self, message: Message):
        """Generate ExteraGram plugin"""
        args = utils.get_args_raw(message)
        if not args:
            await utils.answer(message, "❌ Введите описание плагина для exteraGram.")
            return

        status = await utils.answer(
            message,
            self.strings("thinking").format(
                html.escape(str(self.config["CURRENT_MODEL"]))
            ),
        )

        attached_text = await self._read_attached_text_from_message(message)
        system_prompt = self._build_plugin_generation_system_prompt()
        user_prompt = self._build_plugin_generation_user_prompt(args, attached_text)

        code = await self._api_request(system_prompt, user_prompt)
        code = self._postprocess_code(code, plugin=True)

        if code.startswith("ERROR:"):
            await utils.answer(status, self.strings("api_error").format(utils.escape_html(code)))
            return

        file = io.BytesIO(code.encode("utf-8"))
        file.name = f"plugin_{utils.rand(4)}.plugin"

        await self._client.send_file(
            message.chat_id,
            file,
            caption=self.strings("plugin_ready").format(
                html.escape(str(self.config["CURRENT_MODEL"]))
            ),
            reply_to=message.id,
        )
        await status.delete()

    @loader.command(
        ru_doc="<описание> - Исправить exteraGram .plugin из реплая. Можно прикрепить файл контекста"
    )
    async def fixplugcmd(self, message: Message):
        """Fix ExteraGram plugin from reply"""
        reply = await message.get_reply_message()
        args = (
            utils.get_args_raw(message)
            or "Исправь ошибки и доведи до рабочего exteraGram .plugin по документациям"
        )

        if not reply:
            await utils.answer(message, self.strings("no_reply_plugin"))
            return

        status = await utils.answer(message, self.strings("analyzing_plugin"))

        try:
            code_content = await self._read_code_from_reply(reply)
        except Exception as e:
            await utils.answer(status, self.strings("read_error").format(utils.escape_html(str(e))))
            return

        if not code_content:
            await utils.answer(status, self.strings("empty_plugin"))
            return

        attached_text = await self._read_attached_text_from_message(message)
        system_prompt = self._build_plugin_fix_system_prompt()
        user_prompt = self._build_plugin_fix_user_prompt(args, attached_text, code_content)

        fixed_code = await self._api_request(system_prompt, user_prompt)
        fixed_code = self._postprocess_code(fixed_code, plugin=True)

        if fixed_code.startswith("ERROR:"):
            await utils.answer(status, self.strings("api_error").format(utils.escape_html(fixed_code)))
            return

        file = io.BytesIO(fixed_code.encode("utf-8"))
        file.name = "fixed_plugin.plugin"

        changelog = self._build_changelog(code_content, fixed_code)
        caption = f"<b>{self.strings('plugin_fixed')}</b>"
        if changelog:
            caption += (
                f"\n\n<b>{self.strings('changelog')}</b>:\n"
                f"<blockquote><span class=\"tg-spoiler\">{changelog}</span></blockquote>"
            )

        await self._client.send_file(
            message.chat_id,
            file,
            caption=caption,
            reply_to=message.id,
        )
        await status.delete()

    @loader.command(ru_doc="Открыть меню выбора модели")
    async def modelscmd(self, message: Message):
        """Show models menu"""
        await utils.answer(message, self.strings("models_loading"))
        models = await self._fetch_models()
        if not models:
            await utils.answer(message, self.strings("models_error"))
            return

        await self._show_models_page(message, 0)

    # ---------------------------------------------------------
    # Промпты
    # ---------------------------------------------------------

    def _build_module_generation_system_prompt(self) -> str:
        forbidden = ", ".join(sorted(FORBIDDEN_COMMANDS))
        return (
            "Ты senior Python-разработчик модулей для Heroku UserBot / Hikka.\n"
            "Нужно вернуть только полный рабочий Python-код одного модуля.\n\n"
            "Жёсткие правила:\n"
            "1. Возвращай только сырой код без Markdown и без пояснений.\n"
            "2. Используй архитектуру Heroku/Hikka: @loader.tds, класс-наследник loader.Module.\n"
            "3. Обязательно добавляй __version__, meta developer и корректные импорты.\n"
            "4. Основные импорты должны быть совместимы с framework, включая from .. import loader, utils.\n"
            "5. Команды должны оканчиваться на cmd.\n"
            f"6. Никогда не используй и не перезаписывай команды: {forbidden}.\n"
            "7. Код должен быть production-ready: валидация входных данных, try/except, безопасная работа с HTML.\n"
            "8. Если нужны настройки — используй loader.ModuleConfig и loader.ConfigValue.\n"
            "9. Если нужны БД-данные — используй self.get/self.set/self.pointer.\n"
            "10. Все комментарии и тексты в коде должны быть на русском языке.\n"
            "11. Не используй заглушки, TODO и псевдокод.\n"
            "12. Сразу возвращай финальный файл целиком."
        )

    def _build_module_generation_user_prompt(
        self, description: str, attached_text: Optional[str]
    ) -> str:
        parts = [
            f"Задача пользователя:\n{description}",
            (
                "Сгенерируй полноценный модуль под Heroku UserBot. "
                "Если в описании не хватает деталей, выбери разумную и безопасную реализацию."
            ),
        ]
        if attached_text:
            parts.append(f"Дополнительный контекст:\n{attached_text}")
        return "\n\n".join(parts)

    def _build_module_fix_system_prompt(self) -> str:
        forbidden = ", ".join(sorted(FORBIDDEN_COMMANDS))
        return (
            "Ты senior Python-debugger для Heroku UserBot / Hikka.\n"
            "Нужно исправить присланный модуль и вернуть только полный финальный код.\n\n"
            "Правила:\n"
            "1. Верни только сырой Python-код без Markdown и без пояснений.\n"
            "2. Сохрани полезную существующую логику, но исправь синтаксис, архитектуру и баги.\n"
            "3. Приведи код к совместимой структуре Heroku/Hikka.\n"
            f"4. Не создавай команды с именами: {forbidden}.\n"
            "5. Исправь небезопасные места, ошибки работы с сообщениями, сетью, HTML и файлами.\n"
            "6. Если пользователь просит новые функции — добавь их аккуратно, не ломая старое поведение.\n"
            "7. Все комментарии и тексты должны быть на русском языке.\n"
            "8. Верни полный файл целиком."
        )

    def _build_module_fix_user_prompt(
        self, description: str, attached_text: Optional[str], code_content: str
    ) -> str:
        parts = [f"Запрос пользователя:\n{description}"]
        if attached_text:
            parts.append(f"Дополнительный контекст:\n{attached_text}")
        parts.append(f"Код для исправления:\n{code_content}")
        return "\n\n".join(parts)

    def _extera_reference_prompt(self, goal: str) -> str:
        return (
            "Ты ИИ-разработчик плагинов для ExteraGram.\n"
            "Ориентируйся на архитектуру BasePlugin, HookResult, HookStrategy, "
            "ui.settings, ui.bulletin, android_utils, client_utils, markdown_utils.\n"
            "Плагин должен соответствовать документации ExteraGram и быть готовым к использованию.\n\n"
            f"Цель плагина:\n{goal}"
        )

    def _build_plugin_generation_system_prompt(self) -> str:
        return (
            "Ты senior Python-разработчик плагинов для ExteraGram.\n"
            "Верни только полный код одного .plugin файла.\n\n"
            "Правила:\n"
            "1. Только сырой код, без Markdown и пояснений.\n"
            "2. Без сторонних библиотек, только стандартные и доступные в ExteraGram SDK модули.\n"
            "3. Обязательны метаданные: __id__, __name__, __description__, __version__, __author__, __min_version__, __icon__.\n"
            "4. Один основной класс, наследуемый от BasePlugin.\n"
            "5. Если есть тяжёлые операции — не блокируй UI.\n"
            "6. Комментарии в коде — короткие и на русском языке.\n"
            "7. Верни полный рабочий файл."
        )

    def _build_plugin_generation_user_prompt(
        self, description: str, attached_text: Optional[str]
    ) -> str:
        parts = [
            f"Задача пользователя:\n{description}",
            f"Референс:\n{self._extera_reference_prompt(description)}",
        ]
        if attached_text:
            parts.append(f"Дополнительный контекст:\n{attached_text}")
        return "\n\n".join(parts)

    def _build_plugin_fix_system_prompt(self) -> str:
        return (
            "Ты senior Python-debugger для ExteraGram plugin API.\n"
            "Исправь код и верни только полный рабочий .plugin файл.\n\n"
            "Правила:\n"
            "1. Только сырой код, без Markdown и комментариев вне кода.\n"
            "2. Исправь синтаксис, импорты, хуки, блокирующие вызовы и архитектурные ошибки.\n"
            "3. Сохрани полезную логику и добавь requested features, если они нужны.\n"
            "4. Используй только доступные модули ExteraGram/stdlib.\n"
            "5. Верни полный файл целиком."
        )

    def _build_plugin_fix_user_prompt(
        self, description: str, attached_text: Optional[str], code_content: str
    ) -> str:
        parts = [
            f"Запрос пользователя:\n{description}",
            f"Референс:\n{self._extera_reference_prompt(description)}",
        ]
        if attached_text:
            parts.append(f"Дополнительный контекст:\n{attached_text}")
        parts.append(f"Код для исправления:\n{code_content}")
        return "\n\n".join(parts)

    # ---------------------------------------------------------
    # API / HTTP / Proxy
    # ---------------------------------------------------------

    def _normalize_provider_url(self, raw_url: str) -> str:
        url = (raw_url or "").strip()
        if not url:
            url = DEFAULT_PROVIDER_URL

        url = url.rstrip("/")

        if url.endswith("/chat/completions"):
            return url

        if url.endswith("/v1"):
            return f"{url}/chat/completions"

        if url.endswith("/ai/v2"):
            return url

        if url.endswith("/v1/"):
            return f"{url.rstrip('/')}/chat/completions"

        if url.endswith("/openai"):
            return f"{url}/chat/completions"

        if "/chat/completions" in url:
            return url

        if re.search(r"/v\d+$", url):
            return f"{url}/chat/completions"

        return url

    def _guess_models_url(self) -> List[str]:
        custom = (self.config["MODELS_URL"] or "").strip()
        if custom:
            return [custom]

        provider = self._normalize_provider_url(self.config["PROVIDER_URL"])
        candidates: List[str] = []

        if provider.endswith("/chat/completions"):
            base = provider[: -len("/chat/completions")]
            candidates.extend(
                [
                    f"{base}/models",
                    base,
                ]
            )
        else:
            candidates.extend(
                [
                    f"{provider.rstrip('/')}/models",
                    provider.rstrip("/"),
                ]
            )

        for item in DEFAULT_MODELS_URLS:
            if item not in candidates:
                candidates.append(item)

        uniq = []
        for item in candidates:
            if item and item not in uniq:
                uniq.append(item)
        return uniq

    def _build_headers(self) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.config['API_KEY']}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        extra = (self.config["EXTRA_HEADERS"] or "").strip()
        if extra:
            for chunk in extra.split("|"):
                chunk = chunk.strip()
                if not chunk or ":" not in chunk:
                    continue
                key, value = chunk.split(":", 1)
                key = key.strip()
                value = value.strip()
                if key:
                    headers[key] = value

        return headers

    def _get_session_kwargs(self) -> Tuple[Dict[str, Any], Optional[str]]:
        kwargs: Dict[str, Any] = {
            "timeout": aiohttp.ClientTimeout(total=int(self.config["REQUEST_TIMEOUT"])),
        }

        ssl_value = bool(self.config["VERIFY_SSL"])
        kwargs["connector"] = None
        proxy_warning = None

        if self.config["USE_PROXY"]:
            proxy_url = (self.config["PROXY_URL"] or "").strip()
            if proxy_url:
                try:
                    lowered = proxy_url.lower()
                    if lowered.startswith(
                        (
                            "socks4://",
                            "socks5://",
                            "socks5h://",
                            "socks4a://",
                        )
                    ):
                        kwargs["connector"] = ProxyConnector.from_url(
                            proxy_url,
                            ssl=ssl_value,
                        )
                    elif lowered.startswith(("http://", "https://")):
                        kwargs["trust_env"] = False
                        kwargs["proxy"] = proxy_url
                        kwargs["connector"] = aiohttp.TCPConnector(ssl=ssl_value)
                    else:
                        kwargs["connector"] = ProxyConnector.from_url(
                            proxy_url,
                            ssl=ssl_value,
                        )
                except Exception:
                    logger.exception("Некорректный прокси в конфиге")
                    kwargs["connector"] = aiohttp.TCPConnector(ssl=ssl_value)
                    proxy_warning = self.strings("proxy_invalid")
            else:
                kwargs["connector"] = aiohttp.TCPConnector(ssl=ssl_value)
        else:
            kwargs["connector"] = aiohttp.TCPConnector(ssl=ssl_value)

        return kwargs, proxy_warning

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        json_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        session_kwargs, _ = self._get_session_kwargs()
        headers = self._build_headers()

        proxy = session_kwargs.pop("proxy", None)
        async with aiohttp.ClientSession(**session_kwargs) as session:
            async with session.request(
                method,
                url,
                headers=headers,
                json=json_data,
                proxy=proxy,
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}: {text[:2000]}")
                try:
                    return await resp.json(content_type=None)
                except Exception:
                    raise RuntimeError(f"Некорректный JSON ответ: {text[:2000]}")

    async def _api_request(self, system_prompt: str, user_prompt: str) -> str:
        url = self._normalize_provider_url(self.config["PROVIDER_URL"])
        headers = self._build_headers()
        session_kwargs, _ = self._get_session_kwargs()

        payload = self._build_payload(url, system_prompt, user_prompt)
        proxy = session_kwargs.pop("proxy", None)

        try:
            async with aiohttp.ClientSession(**session_kwargs) as session:
                async with session.post(
                    url,
                    headers=headers,
                    json=payload,
                    proxy=proxy,
                ) as resp:
                    raw_text = await resp.text()
                    if resp.status != 200:
                        return f"ERROR: HTTP {resp.status}\n{raw_text[:4000]}"

                    try:
                        result = await resp.json(content_type=None)
                    except Exception:
                        return f"ERROR: Некорректный JSON ответ\n{raw_text[:4000]}"

                    content = self._extract_content_from_response(result)
                    if not content:
                        return "ERROR: Empty response"

                    return self._clean_code(content)
        except Exception as e:
            logger.exception("Ошибка API запроса")
            return f"ERROR: {type(e).__name__}: {e}"

    def _build_payload(
        self, url: str, system_prompt: str, user_prompt: str
    ) -> Dict[str, Any]:
        model = str(self.config["CURRENT_MODEL"])
        max_tokens = int(self.config["MAX_TOKENS"])

        if url.endswith("/chat/completions"):
            return {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": max_tokens,
            }

        return {
            "model": model,
            "request": {
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_output_tokens": max_tokens,
            },
        }

    def _extract_content_from_response(self, result: Any) -> str:
        if not isinstance(result, dict):
            return ""

        if "choices" in result and isinstance(result["choices"], list) and result["choices"]:
            choice = result["choices"][0]
            if isinstance(choice, dict):
                message = choice.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str):
                        return content
                    if isinstance(content, list):
                        parts = []
                        for item in content:
                            if isinstance(item, dict):
                                text = item.get("text")
                                if text:
                                    parts.append(str(text))
                        if parts:
                            return "\n".join(parts)

                text = choice.get("text")
                if isinstance(text, str):
                    return text

                delta = choice.get("delta")
                if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                    return delta["content"]

        if isinstance(result.get("message"), dict):
            msg = result["message"]
            if isinstance(msg.get("content"), str):
                return msg["content"]
            if isinstance(msg.get("text"), str):
                return msg["text"]

        for key in ("content", "result", "output", "response", "text"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value

        return ""

    # ---------------------------------------------------------
    # Модели
    # ---------------------------------------------------------

    async def _fetch_models(self) -> Optional[List[Dict[str, Any]]]:
        endpoints = self._guess_models_url()
        session_kwargs, _ = self._get_session_kwargs()
        headers = self._build_headers()
        proxy = session_kwargs.pop("proxy", None)

        for url in endpoints:
            try:
                async with aiohttp.ClientSession(**session_kwargs) as session:
                    async with session.get(
                        url,
                        headers=headers,
                        proxy=proxy,
                    ) as resp:
                        if resp.status != 200:
                            continue

                        data = await resp.json(content_type=None)
                        models = self._normalize_models_response(data)
                        if models:
                            uniq = {}
                            for model in models:
                                uniq[model["id"]] = model
                            self._models_cache = list(uniq.values())
                            return self._models_cache
            except Exception:
                logger.debug("Не удалось получить список моделей с %s", url, exc_info=True)
                continue

        return None

    def _normalize_models_response(self, data: Any) -> List[Dict[str, Any]]:
        models: List[Dict[str, Any]] = []

        def add_model(mid: Optional[str], info: Any):
            if isinstance(info, str):
                models.append(
                    {
                        "id": info,
                        "name": info,
                        "description": "",
                        "modality": "",
                        "owner": "",
                        "cost": None,
                    }
                )
                return

            if not isinstance(info, dict):
                return

            model_id = (
                mid
                or info.get("id")
                or info.get("slug")
                or info.get("model")
                or info.get("name")
            )
            if not model_id:
                return

            models.append(
                {
                    "id": str(model_id),
                    "name": str(info.get("name") or model_id),
                    "description": str(
                        info.get("description") or info.get("about") or ""
                    ),
                    "modality": str(info.get("modality") or info.get("type") or ""),
                    "owner": str(info.get("owner") or info.get("provider") or ""),
                    "cost": info.get("cost") or info.get("price"),
                }
            )

        def parse(obj: Any):
            if isinstance(obj, list):
                for item in obj:
                    if isinstance(item, (dict, str)):
                        add_model(None, item)
                return

            if not isinstance(obj, dict):
                return

            if isinstance(obj.get("models"), list):
                for item in obj["models"]:
                    add_model(None, item)

            if isinstance(obj.get("models"), dict):
                for mid, item in obj["models"].items():
                    add_model(mid, item)

            if isinstance(obj.get("data"), list):
                for item in obj["data"]:
                    add_model(None, item)

            if isinstance(obj.get("data"), dict):
                parse(obj["data"])

            if isinstance(obj.get("classified"), dict):
                for _, bucket in obj["classified"].items():
                    if isinstance(bucket, dict):
                        for mid, item in bucket.items():
                            add_model(mid, item)
                    elif isinstance(bucket, list):
                        for item in bucket:
                            add_model(None, item)

            special = {"models", "classified", "data", "api-version", "object"}
            direct_items = [
                (k, v) for k, v in obj.items() if k not in special and isinstance(v, dict)
            ]
            if direct_items:
                for mid, item in direct_items:
                    add_model(mid, item)

        parse(data)

        uniq: Dict[str, Dict[str, Any]] = {}
        for model in models:
            prev = uniq.get(model["id"])
            if not prev:
                uniq[model["id"]] = model
                continue

            if len(model.get("description", "")) > len(prev.get("description", "")):
                uniq[model["id"]] = model

        return list(uniq.values())

    async def _show_models_page(self, target, page: int = 0):
        if not self._models_cache:
            await self._fetch_models()

        models = self._models_cache or []
        total_pages = max(1, math.ceil(len(models) / self._models_per_page))
        page = max(0, min(page, total_pages - 1))

        start = page * self._models_per_page
        end = (page + 1) * self._models_per_page
        page_models = models[start:end]

        current_id = str(self.config["CURRENT_MODEL"])
        current_name = next((m["name"] for m in models if m["id"] == current_id), None)

        text = f"<b>{self.strings('models_title')}</b>\n"
        text += self.strings("current_model").format(html.escape(current_id))
        if current_name and current_name != current_id:
            text += f" — {html.escape(current_name)}"
        text += "\n"
        text += self.strings("page").format(page + 1, total_pages)
        text += "\n\n"

        buttons = []
        for model in page_models:
            selected = "✅" if model["id"] == current_id else "▪️"
            desc = model.get("description") or ""
            if len(desc) > MAX_PREVIEW_DESCRIPTION:
                desc = desc[: MAX_PREVIEW_DESCRIPTION - 3] + "..."

            text += f"{selected} <b>{html.escape(model['name'])}</b>\n"
            text += f"<code>{html.escape(model['id'])}</code>\n"
            if desc:
                text += f"{html.escape(desc)}\n"
            text += "\n"

            buttons.append(
                [
                    {
                        "text": self.strings("select_model").format(model["name"]),
                        "callback": self._set_model_callback,
                        "args": (model["id"], page),
                    }
                ]
            )

        nav = []
        if page > 0:
            nav.append({"text": "◀️", "callback": self._page_callback, "args": (page - 1,)})
        if page < total_pages - 1:
            nav.append({"text": "▶️", "callback": self._page_callback, "args": (page + 1,)})
        if nav:
            buttons.append(nav)

        buttons.append([{"text": self.strings("close"), "action": "close"}])

        if getattr(target, "__class__", None).__name__ == "InlineCall":
            await target.edit(text=text, reply_markup=buttons)
        else:
            await self.inline.form(text=text, message=target, reply_markup=buttons)

    async def _page_callback(self, call, page: int):
        await self._show_models_page(call, page)

    async def _set_model_callback(self, call, model_id: str, page: int):
        self.config["CURRENT_MODEL"] = model_id
        try:
            await call.answer(self.strings("model_set").format(model_id))
        except Exception:
            pass
        await self._show_models_page(call, page)

    # ---------------------------------------------------------
    # Чтение/обработка кода
    # ---------------------------------------------------------

    async def _read_code_from_reply(self, reply: Message) -> str:
        if getattr(reply, "document", None):
            file_bytes = await self._client.download_media(reply, bytes)
            if not file_bytes:
                return ""
            try:
                return file_bytes.decode("utf-8")
            except Exception:
                return file_bytes.decode("utf-8", errors="ignore")

        return reply.raw_text or ""

    async def _read_attached_text_from_message(self, message: Message) -> Optional[str]:
        try:
            if not getattr(message, "document", None):
                return None

            file_bytes = await self._client.download_media(message, bytes)
            if not file_bytes:
                return None

            if len(file_bytes) > MAX_CONTEXT_BYTES:
                file_bytes = file_bytes[:MAX_CONTEXT_BYTES]

            try:
                return file_bytes.decode("utf-8")
            except Exception:
                return file_bytes.decode("utf-8", errors="ignore")
        except Exception:
            logger.debug("Не удалось прочитать прикреплённый контекст", exc_info=True)
            return None

    def _strip_code_fences(self, text: str) -> str:
        if not isinstance(text, str):
            return ""
        text = text.strip()
        text = re.sub(r"^```[\w+-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        return text.strip()

    def _clean_code(self, text: str) -> str:
        return str(text).strip()

    def _postprocess_code(self, code: str, plugin: bool = False) -> str:
        code = self._strip_code_fences(code).strip()
        if code.startswith("ERROR:"):
            return code

        if not plugin:
            code = self._sanitize_module_code(code)

        return code.strip()

    def _sanitize_module_code(self, code: str) -> str:
        for forbidden in FORBIDDEN_COMMANDS:
            pattern = rf"async\s+def\s+{re.escape(forbidden)}cmd\s*\("
            if re.search(pattern, code):
                code = re.sub(
                    pattern,
                    f"async def {forbidden}_safe_cmd(",
                    code,
                )

        if "from .. import loader, utils" not in code:
            lines = code.splitlines()
            insert_pos = 0

            for i, line in enumerate(lines):
                if line.startswith(("import ", "from ")):
                    insert_pos = i + 1

            lines.insert(insert_pos, "from .. import loader, utils")
            code = "\n".join(lines)

        return code

    # ---------------------------------------------------------
    # Changelog
    # ---------------------------------------------------------

    def _build_changelog(self, old: str, new: str) -> str:
        def safe_join(items):
            return ", ".join(f"<code>{html.escape(x)}</code>" for x in items if x)

        def extract_meta(code: str):
            meta = {
                "funcs": set(),
                "afuncs": set(),
                "commands": set(),
                "classes": set(),
                "imports": set(),
            }

            try:
                tree = ast.parse(code)
            except Exception:
                return meta

            class Visitor(ast.NodeVisitor):
                def visit_FunctionDef(self, node: ast.FunctionDef):
                    if node.name.endswith("cmd"):
                        meta["commands"].add(node.name)
                    else:
                        meta["funcs"].add(node.name)
                    self.generic_visit(node)

                def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
                    if node.name.endswith("cmd"):
                        meta["commands"].add(node.name)
                    else:
                        meta["afuncs"].add(node.name)
                    self.generic_visit(node)

                def visit_ClassDef(self, node: ast.ClassDef):
                    meta["classes"].add(node.name)
                    self.generic_visit(node)

                def visit_Import(self, node: ast.Import):
                    for alias in node.names:
                        if alias.asname:
                            meta["imports"].add(f"import {alias.name} as {alias.asname}")
                        else:
                            meta["imports"].add(f"import {alias.name}")

                def visit_ImportFrom(self, node: ast.ImportFrom):
                    mod = node.module or ""
                    names = []
                    for alias in node.names:
                        if alias.asname:
                            names.append(f"{alias.name} as {alias.asname}")
                        else:
                            names.append(alias.name)
                    meta["imports"].add(f"from {mod} import {', '.join(names)}")

            Visitor().visit(tree)
            return meta

        try:
            old_meta = extract_meta(old or "")
            new_meta = extract_meta(new or "")

            old_funcs_all = old_meta["funcs"] | old_meta["afuncs"]
            new_funcs_all = new_meta["funcs"] | new_meta["afuncs"]

            added_cmds = sorted(new_meta["commands"] - old_meta["commands"])
            removed_cmds = sorted(old_meta["commands"] - new_meta["commands"])
            added_funcs = sorted(
                new_funcs_all - old_funcs_all - (new_meta["commands"] - old_meta["commands"])
            )
            removed_funcs = sorted(
                old_funcs_all - new_funcs_all - (old_meta["commands"] - new_meta["commands"])
            )
            added_classes = sorted(new_meta["classes"] - old_meta["classes"])
            removed_classes = sorted(old_meta["classes"] - new_meta["classes"])
            added_imports = sorted(new_meta["imports"] - old_meta["imports"])
            removed_imports = sorted(old_meta["imports"] - new_meta["imports"])

            lines = []

            if added_cmds:
                lines.append(f"• добавил команды: {safe_join(added_cmds)}")
            if removed_cmds:
                lines.append(f"• убрал команды: {safe_join(removed_cmds)}")
            if added_funcs:
                lines.append(f"• добавил функции: {safe_join(added_funcs)}")
            if removed_funcs:
                lines.append(f"• убрал функции: {safe_join(removed_funcs)}")
            if added_classes:
                lines.append(f"• добавил классы: {safe_join(added_classes)}")
            if removed_classes:
                lines.append(f"• убрал классы: {safe_join(removed_classes)}")
            if added_imports:
                lines.append(f"• добавил импорты: {safe_join(added_imports)}")
            if removed_imports:
                lines.append(f"• убрал импорты: {safe_join(removed_imports)}")

            if not lines:
                old_lines = (old or "").splitlines()
                new_lines = (new or "").splitlines()
                diff = list(difflib.unified_diff(old_lines, new_lines, lineterm=""))
                added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
                removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))

                if added or removed:
                    lines.append(f"• внёс правки по коду (строк: +{added} / -{removed})")
                else:
                    lines.append("• изменений не обнаружено")

            return "\n".join(lines)
        except Exception:
            logger.exception("Ошибка сборки changelog")
            return ""
