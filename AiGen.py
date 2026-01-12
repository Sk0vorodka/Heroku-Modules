# meta developer: @Sk0lovek - @Sk0lovek_plugins
# scope: hikka_only
# meta name: AIGen
# meta version: 1.0.0

import aiohttp
import io
import re
import math
import asyncio
import difflib
import html
import ast
from .. import loader, utils

@loader.tds
class AiGenMod(loader.Module):
    """🤖 Генератор и фиксатор модулей через OnlySq API v2 с защитой от перезаписи команд"""
    strings = {"name": "AiGen"}

    def __init__(self):
        self.config = loader.ModuleConfig(
            loader.ConfigValue("API_KEY", "openai", "🔑 API ключ OnlySq (или 'openai' для публичного доступа)"),
            loader.ConfigValue("CURRENT_MODEL", "gpt-5", "🧠 Модель по умолчанию"),
            loader.ConfigValue("MAX_TOKENS", 8000, "Максимум токенов для ответа")
        )
        self._models_cache = []
        self._models_per_page = 6

    async def client_ready(self, client, db):
        self.client = client

    async def genmodcmd(self, message):
        """<описание> — Сгенерировать модуль по описанию. Можно прикрепить файл к команде — он будет учтён после промпта"""
        args = utils.get_args_raw(message)
        if not args:
            return await utils.answer(message, "<b>❌ Введите описание модуля!</b>")

        status = await utils.answer(message, f"<b>🧠 Думаю ({self.config['CURRENT_MODEL']})...</b>")

        attached_text = await self._read_attached_text_from_message(message)

        sys_prompt = (
            "You are the Lead Architect of the Hikka Userbot Framework (Python 3.10+ & Telethon). "
            "Your task is to generate PRODUCTION-READY, ERROR-FREE Python code for a userbot module based on the user's request.\n\n"
            "⛔️ CRITICAL OUTPUT RULES:\n"
            "1. RETURN ONLY RAW CODE. NO Markdown code fences, no extra text.\n"
            "2. Ensure imports start with: from .. import loader, utils\n"
            "3. Forbid overwriting core commands: help, ping, info, id, dl, exec, eval, term, sh, restart, update, alias, modules, load, unload.\n"
            "4. Use async def and await.\n\n"
            "ARCHITECTURE:\n"
            "- Class must inherit from loader.Module, decorated with @loader.tds.\n"
            "- strings = {'name': 'ModuleName'} (+ strings_ru recommended).\n"
            "- If settings are needed, use loader.ModuleConfig and loader.ConfigValue.\n"
            "- Use self.db.get/set for persistence.\n"
            "- Commands: methods ending with 'cmd'.\n"
            "- Interactions via utils.get_args_raw(message), utils.answer(message, ...).\n"
            "- Inline via self.inline.form if necessary.\n\n"
            "Return only final code. No commentary."
        )

        user_prompt = f"REQUEST: {args}"
        if attached_text:
            user_prompt += f"\n\nCONTEXT_FILE (Use this logic/text if relevant):\n{attached_text}"

        code = await self._api_request(sys_prompt, user_prompt)

        code = self._strip_code_fences(code).strip()

        if code.startswith("ERROR:"):
            return await utils.answer(status, f"<b>❌ Ошибка API:</b>\n{code}")

        file = io.BytesIO(code.encode("utf-8"))
        file.name = f"mod_{utils.rand(4)}.py"
        
        await self.client.send_file(
            message.chat_id,
            file,
            caption=f"<b>✅ Модуль готов!</b>\n🧩 Модель: <code>{html.escape(str(self.config['CURRENT_MODEL']))}</code>",
            reply_to=message.id
        )
        await status.delete()

    async def fixmodcmd(self, message):
        """<описание> (реплай на .py) — Исправить модуль. Можно прикрепить файл к команде: сначала читается промпт, затем файл, затем код плагина из реплая"""
        reply = await message.get_reply_message()
        args = utils.get_args_raw(message) or "Fix syntax and logic errors"

        if not reply:
            return await utils.answer(message, "<b>❌ Сделай реплай на файл .py.</b>")

        status = await utils.answer(message, "<b>🧩 Анализирую код...</b>")

        code_content = None
        if getattr(reply, "document", None):
            try:
                file_bytes = await self.client.download_media(reply, bytes)
                code_content = file_bytes.decode("utf-8", errors="ignore")
            except Exception as e:
                return await utils.answer(status, f"<b>Ошибка чтения файла:</b> {e}")
        else:
            code_content = reply.raw_text

        if not code_content:
            return await utils.answer(status, "<b>❌ Не удалось прочитать код.</b>")

        attached_text = await self._read_attached_text_from_message(message)

        sys_prompt = (
            "You are a Senior Python Debugger for the Hikka Userbot framework. "
            "Your task is to fix bugs, optimize performance, and ensure the code follows Hikka architecture.\n"
            "RULES:\n"
            "1. Return ONLY raw Python code. No Markdown.\n"
            "2. Ensure imports are correct (`from .. import loader, utils`).\n"
            "3. Check for command name conflicts (do not use 'help', 'exec', etc.).\n"
            "4. Fix indentation and syntax errors.\n"
            "5. If the user requests new features, add them while maintaining existing logic."
        )
        
        user_prompt_parts = [f"USER_REQUEST: {args}"]
        if attached_text:
            user_prompt_parts.append(f"REFERENCE_FILE:\n{attached_text}")
        user_prompt_parts.append(f"BROKEN_CODE:\n{code_content}")
        user_prompt = "\n\n".join(user_prompt_parts)

        fixed_code = await self._api_request(sys_prompt, user_prompt)
        
        fixed_code = self._strip_code_fences(fixed_code).strip()

        if fixed_code.startswith("ERROR:"):
            return await utils.answer(status, f"<b>❌ Ошибка API:</b>\n{fixed_code}")

        file = io.BytesIO(fixed_code.encode("utf-8"))
        file.name = "fixed_module.py"

        changelog = self._build_changelog(code_content, fixed_code)
        caption = "<b>✅ Исправлено!</b>"
        if changelog:
            caption += f"\n\n<b>Changelog</b>:\n<blockquote><span class=\"tg-spoiler\">{changelog}</span></blockquote>"

        await self.client.send_file(message.chat_id, file, caption=caption, reply_to=message.id)
        await status.delete()

    def _extera_reference_prompt(self, goal: str) -> str:
        tpl = """Ты — искусственный интеллект-разработчик, специализирующийся на создании плагинов для Telegram-клиента ExteraGram. Используй следующие источники:

1. Документация ExteraGram:
   - Setup: https://plugins.exteragram.app/docs/setup  
   - First Plugin: https://plugins.exteragram.app/docs/first-plugin  
   - Plugin Class: https://plugins.exteragram.app/docs/plugin-class  
   - Xposed Hooking: https://plugins.exteragram.app/docs/xposed-hooking  
   - Android Utils: https://plugins.exteragram.app/docs/android-utils  
   - Client Utils: https://plugins.exteragram.app/docs/client-utils  
   - Markdown Utils: https://plugins.exteragram.app/docs/markdown-utils  
   - AlertDialog Builder: https://plugins.exteragram.app/docs/alert-dialog-builder  
   - Bulletin Helper: https://plugins.exteragram.app/docs/bulletin-helper  
   - Common Source Classes: https://plugins.exteragram.app/docs/common-source-classes  

2. Пример плагина «GoogleThat»:

   - Метаданные: `__id__`, `__name__`, `__version__`, `__min_version__`, `__author__`, `__description__`, `__icon__`.
   - Локализация через класс `Locales` и функция `localise(key)`.
   - Проверка зависимости от внешнего модуля `zwylib`.
   - Регистрация команды через dispatcher: `dp.register_command("gt")`.
   - Хук-результаты: `HookResult(strategy=HookStrategy.MODIFY, params=params)`.
   - Методы жизненного цикла: `on_plugin_load`, `on_plugin_unload`.
   - Настройки через UI-элементы: `Header`, `Selector`, `Divider`.

3. Возможности SDK и утилит:

   - Hook-и: `pre_request_hook`, `post_request_hook`, `on_update_hook`, `on_send_message_hook`.
   - Утилиты клиентские: `send_text`, `edit_message`, `get_setting`, `get_account_instance`.
   - Android-утилиты: запуск на UI-потоке, логирование, Runnable / слушатели.
   - UI: диалоги (AlertDialogBuilder), уведомления (BulletinHelper), меню настроек.

4. Методы Telegram (TL-методы):

   - Возможность перехватывать методы, такие как `TL_messages_sendMessage`, `TL_updateNewMessage`, `TL_messages_readHistory` и др., через хуки в `add_hook(...)`.

---

### Инструкция (цель):

Напиши плагин, который выполняет [твоя цель — здесь чётко сформулируй, что должен делать плагин, например: автоматическая очистка спама, фильтрация определённых ключевых слов, статистика чатов, перевод текста командой и др.].

---

### Что должен содержать ответ:

- Название и уникальный `__id__` плагина.  
- Полный список метаданных: `__name__`, `__description__`, `__version__`, `__author__`, `__icon__`, `__min_version__`.  
- Проект структуры плагина: файлы (если необходимые), зависимости (например, внешние модули типа `zwylib`, или стандартные утилиты).  
- Класс, наследуемый `BasePlugin`, с методами: `on_plugin_load`, `on_plugin_unload`, возможно `on_app_event`.  
- Если нужно — регистрация команд через dispatcher (как `.gt` пример).  
- Пример hook’ов, которые будут использоваться (какой TL-метод или событие, какая стратегия: MODIFY, CANCEL или DEFAULT).  
- Использование утилит: `client_utils`, `android_utils`, `alert-dialog-builder`, `bulletin-helper`.  
- Настройки плагина через `create_settings()` с UI-элементами (`Header`, `Selector`, `Divider` и др.).  
- Локализация (если актуально) через `Locales` и `localise(...)`.  
- Примеры логирования, ошибок и их обработки.  

---

### Пример части кода/функций, которые можно включить:
python
from base_plugin import BasePlugin, HookResult, HookStrategy

from ui.settings import Header, Selector, Divider

Пример добавления хука
self.add_hook("TL_messages_sendMessage", match_substring=False, priority=0)

Обработка hook-а:
def on_send_message_hook(self, account, params):

if should_modify(params):

params.message = modify_message(params.message)

return HookResult(strategy=HookStrategy.MODIFY, params=params)

return HookResult.DEFAULT


---

Используй вышеуказанные источники документации и пример «GoogleThat» как ориентиры. Постарайся, чтобы твой плагин соответствовал стандартам ExteraGram, использовал корректные хуки и утилиты, имел удобные настройки и локализацию, если нужно.

---

Теперь сформируй полный код-плагин и структуру, исходя из моей цели: **[твоя конкретная цель здесь]**.
---

Ты можешь подставить вместо [твоя конкретная цель здесь] задачу, которую нужно реализовать — и с этим шаблоном запрос к ИИ будет максимально полным, ориентированным на документацию и примеры."""
        return tpl.replace("[твоя конкретная цель здесь]", str(goal))

    async def genplugcmd(self, message):
        """<описание> — Сгенерировать exteraGram .plugin по описанию. Можно прикрепить файл к команде — он будет учтён после промпта"""
        args = utils.get_args_raw(message)
        if not args:
            return await utils.answer(message, "<b>❌ Введите описание плагина для exteraGram!</b>")

        status = await utils.answer(message, f"<b>🧠 Генерирую .plugin ({self.config['CURRENT_MODEL']})...</b>")

        attached_text = await self._read_attached_text_from_message(message)

        sys_prompt = (
            "Всегда генерируй рабочий Python-код плагина для exteraGram (.plugin), с корректными импортами, "
            "из поддерживаемых модулей и без сторонних библиотек. Пользователь получит только этот код — он должен быть полным.\n\n"
            "ОБЩИЕ ПРАВИЛА ВЫВОДА:\n"
            "1) Возвращай ТОЛЬКО сырой код одного плагина. Без Markdown, без комментариев до/после кода.\n"
            "2) Вставляй короткие человеческие комментарии в код (немного), и один намёк: '# сгенерировано в @Username'.\n"
            "3) Без внешних библиотек. Разрешены стандартные и модули из документаций exteraGram (android_utils, client_utils, markdown_utils, ui.settings, ui.bulletin и т.п.).\n"
            "4) Если есть сетевые вызовы/тяжёлые задачи — не блокируй UI; используй client_utils.run_on_queue и android_utils.run_on_ui_thread при необходимости.\n"
            "5) Команды (если ты создаёшь перехват сообщения): регистрируй self.add_on_send_message_hook() в on_plugin_load и обрабатывай в on_send_message_hook с HookResult.\n"
            "6) Пиши весь код целиком — один класс, который наследуется от BasePlugin.\n\n"
            "МЕТАДАННЫЕ (обязательны в начале файла, как простые строки):\n"
            "__id__ = \"<snake_or_kebab_like_id>\"\n"
            "__name__ = \"<читаемое имя>\"\n"
            "__description__ = \"<краткое описание>\"\n"
            "__version__ = \"1.0.0\"\n"
            "__author__ = \"@Username\"\n"
            "__min_version__ = \"11.12.0\"\n"
            "__icon__ = \"sPluginIDE/0\"  # или подходящая из списка\n\n"
            "СТРУКТУРА:\n"
            "- Один класс: class SomethingPlugin(BasePlugin):\n"
            "- on_plugin_load / on_plugin_unload при необходимости.\n"
            "- create_settings() возвращает список контролов для настроек (если нужны) из ui.settings.\n"
            "- Если перехватываешь отправку сообщения, возвращай HookResult(strategy=HookStrategy.MODIFY/CANCEL/DEFAULT ...)\n"
            "- Используй markdown_utils.parse_markdown для форматирования.\n"
            "- Для уведомлений — ui.bulletin.BulletinHelper.\n"
            "- Для отправки сообщений — client_utils.send_message.\n\n"
            "ДОПОЛНИТЕЛЬНО:\n"
            "- Выбирай подходящую __icon__ из каталога иконок.\n"
            "- Все команды и тексты локализуй по необходимости кратко, но можно без отдельного словаря.\n"
            "- Пиши понятный, рабочий код по примерам документаций (Plugin Class, First Plugin, Android/Client/Markdown utils, Dialog Builder, Bulletin Helper).\n"
            "Верни итоговый плагин полностью.\n\n"
            "Справочные материалы (для ориентира при необходимости):\n"
            "- Исходный код Telegram: https://github.com/DrKLO/Telegram\n"
            "- SDK Telegram Passport (JavaScript): https://core.telegram.org/passport/sdk-javascript"
        )

        # Добавляем референсный промпт с подстановкой цели
        reference_prompt = self._extera_reference_prompt(args)

        user_prompt_parts = [f"USER_REQUEST: {args}", f"REFERENCE_FILE:\n{reference_prompt}"]
        if attached_text:
            user_prompt_parts.append(f"CONTEXT_FILE (Use this as additional context):\n{attached_text}")
        user_prompt_parts.append(
            "RESOURCES:\n"
            "Исходный код Telegram: https://github.com/DrKLO/Telegram\n"
            "SDK Telegram Passport (JavaScript): https://core.telegram.org/passport/sdk-javascript"
        )
        user_prompt = "\n\n".join(user_prompt_parts)

        code = await self._api_request(sys_prompt, user_prompt)
        code = self._strip_code_fences(code).strip()

        if code.startswith("ERROR:"):
            return await utils.answer(status, f"<b>❌ Ошибка API:</b>\n{code}")

        file = io.BytesIO(code.encode("utf-8"))
        file.name = f"plugin_{utils.rand(4)}.plugin"

        await self.client.send_file(
            message.chat_id,
            file,
            caption=f"<b>✅ Плагин создан!</b>\n🧩 Модель: <code>{html.escape(str(self.config['CURRENT_MODEL']))}</code>",
            reply_to=message.id
        )
        await status.delete()

    async def fixplugcmd(self, message):
        """<описание> (реплай на .plugin) — Исправить exteraGram .plugin. Можно прикрепить файл контекста к команде"""
        reply = await message.get_reply_message()
        args = utils.get_args_raw(message) or "Исправь ошибки и доведи до рабочего exteraGram .plugin по документациям"

        if not reply:
            return await utils.answer(message, "<b>❌ Сделай реплай на .plugin (или вставь код в текст).</b>")

        status = await utils.answer(message, "<b>🧩 Анализирую .plugin...</b>")

        code_content = None
        if getattr(reply, "document", None):
            try:
                file_bytes = await self.client.download_media(reply, bytes)
                code_content = file_bytes.decode("utf-8", errors="ignore")
            except Exception as e:
                return await utils.answer(status, f"<b>Ошибка чтения файла:</b> {e}")
        else:
            code_content = reply.raw_text

        if not code_content:
            return await utils.answer(status, "<b>❌ Не удалось прочитать .plugin.</b>")

        attached_text = await self._read_attached_text_from_message(message)

        sys_prompt = (
            "Ты Senior Python Debugger для exteraGram (.plugin). "
            "Задача: исправить, оптимизировать и привести код к рабочему, следуя архитектуре exteraGram.\n\n"
            "ТРЕБОВАНИЯ К ВЫХОДУ:\n"
            "1) Верни ТОЛЬКО сырой полный код одного .plugin. Без Markdown и лишнего текста.\n"
            "2) Корректные импорты из доступных модулей (android_utils, client_utils, markdown_utils, ui.settings, ui.bulletin, и т.д.). Без сторонних библиотек.\n"
            "3) Сохрани/исправь метаданные вверху файла:\n"
            "   __id__, __name__, __description__, __version__ (оставь/установи 1.0.0, если нет), __author__ = \"@Username\", __min_version__ = \"11.12.0\", __icon__ подходящая.\n"
            "4) Один класс-наследник BasePlugin; соблюдай хуки (add_on_send_message_hook и on_send_message_hook) и возвращай HookResult.\n"
            "5) Исправь синтаксис/отступы, проверь блокирующие вызовы; при сетевых/тяжёлых операциях — используй client_utils.run_on_queue и android_utils.run_on_ui_thread.\n"
            "6) Комментарии короткие и по делу; один намёк: '# сгенерировано в @Username'.\n"
            "7) Если пользователь просит новые фичи — добавь, сохранив текущую логику.\n\n"
            "Справочные материалы (для ориентира при необходимости):\n"
            "- Исходный код Telegram: https://github.com/DrKLO/Telegram\n"
            "- SDK Telegram Passport (JavaScript): https://core.telegram.org/passport/sdk-javascript"
        )

        # Добавляем референсный промпт с подстановкой цели
        reference_prompt = self._extera_reference_prompt(args)

        user_prompt_parts = [f"USER_REQUEST: {args}", f"REFERENCE_FILE:\n{reference_prompt}"]
        if attached_text:
            user_prompt_parts.append(f"ADDITIONAL_CONTEXT_FILE:\n{attached_text}")
        user_prompt_parts.append(f"BROKEN_CODE (.plugin):\n{code_content}")
        user_prompt_parts.append(
            "RESOURCES:\n"
            "Исходный код Telegram: https://github.com/DrKLO/Telegram\n"
            "SDK Telegram Passport (JavaScript): https://core.telegram.org/passport/sdk-javascript"
        )
        user_prompt = "\n\n".join(user_prompt_parts)

        fixed_code = await self._api_request(sys_prompt, user_prompt)
        fixed_code = self._strip_code_fences(fixed_code).strip()

        if fixed_code.startswith("ERROR:"):
            return await utils.answer(status, f"<b>❌ Ошибка API:</b>\n{fixed_code}")

        file = io.BytesIO(fixed_code.encode("utf-8"))
        file.name = "fixed_plugin.plugin"

        changelog = self._build_changelog(code_content, fixed_code)
        caption = "<b>✅ Плагин исправлён!</b>"
        if changelog:
            caption += f"\n\n<b>Changelog</b>:\n<blockquote><span class=\"tg-spoiler\">{changelog}</span></blockquote>"

        await self.client.send_file(message.chat_id, file, caption=caption, reply_to=message.id)
        await status.delete()

    async def modelscmd(self, message):
        """Меню выбора модели"""
        await utils.answer(message, "<b>🔄 Загружаю список моделей...</b>")
        models = await self._fetch_models()
        if not models:
            return await utils.answer(message, "<b>❌ Ошибка загрузки списка моделей.</b>")
        await self._show_models_page(message, 0)

    async def _fetch_models(self):
        # Попытка получить список моделей через v2 API с откатом на старый endpoint
        endpoints = [
            "https://api.onlysq.ru/ai/v2/models",
            "https://api.onlysq.ru/ai/models"
        ]
        headers = {
            "Authorization": f"Bearer {self.config['API_KEY']}",
            "Accept": "application/json"
        }
        for url in endpoints:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers, timeout=30) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json(content_type=None)
                        models = self._normalize_models_response(data)
                        if models:
                            # Уникализируем модели по id, сохраняем
                            uniq = {}
                            for m in models:
                                uniq[m["id"]] = m
                            self._models_cache = list(uniq.values())
                            return self._models_cache
            except Exception:
                continue
        return None

    def _normalize_models_response(self, data):
        models = []

        def add_model(mid, info):
            if not isinstance(info, dict):
                return
            _mid = mid or info.get("id") or info.get("slug") or info.get("model") or info.get("name")
            if not _mid:
                return
            name = info.get("name") or _mid
            desc = info.get("description") or info.get("about") or ""
            modality = info.get("modality") or info.get("type") or ""
            owner = info.get("owner") or info.get("provider") or ""
            cost = info.get("cost") or info.get("price")
            models.append({
                "id": str(_mid),
                "name": str(name),
                "description": str(desc),
                "modality": str(modality) if modality else "",
                "owner": str(owner) if owner else "",
                "cost": cost,
            })

        def parse(obj):
            if isinstance(obj, dict):
                # Direct dict with models mapping id->object
                if "models" in obj:
                    m = obj["models"]
                    if isinstance(m, dict):
                        for mid, info in m.items():
                            add_model(mid, info)
                    elif isinstance(m, list):
                        for info in m:
                            if isinstance(info, dict):
                                add_model(None, info)
                # Classified buckets
                if "classified" in obj and isinstance(obj["classified"], dict):
                    for _, bucket in obj["classified"].items():
                        if isinstance(bucket, dict):
                            for mid, info in bucket.items():
                                add_model(mid, info)
                        elif isinstance(bucket, list):
                            for info in bucket:
                                if isinstance(info, dict):
                                    add_model(None, info)
                # Sometimes content is under "data"
                if "data" in obj:
                    parse(obj["data"])
                # If dict looks like id->model entries directly (no special keys)
                special = {"models", "classified", "data", "api-version"}
                if all(isinstance(v, dict) for k, v in obj.items() if k not in special) and obj:
                    for mid, info in obj.items():
                        if mid in special:
                            continue
                        add_model(mid, info)
            elif isinstance(obj, list):
                for item in obj:
                    if isinstance(item, dict):
                        add_model(None, item)
                    elif isinstance(item, str):
                        models.append({"id": item, "name": item, "description": "", "modality": "", "owner": "", "cost": None})

        parse(data)

        # deduplicate by id, prefer entries with description/modality if duplicates
        uniq = {}
        for m in models:
            prev = uniq.get(m["id"])
            if not prev:
                uniq[m["id"]] = m
            else:
                # prefer richer description
                if len(m.get("description", "")) > len(prev.get("description", "")):
                    uniq[m["id"]] = m
        return list(uniq.values())

    async def _show_models_page(self, target, page: int = 0):
        if not self._models_cache:
            await self._fetch_models()
        models = self._models_cache or []
        total_pages = max(1, math.ceil(len(models) / self._models_per_page))
        page = max(0, min(page, total_pages - 1))
        start, end = page * self._models_per_page, (page + 1) * self._models_per_page
        page_models = models[start:end]

        current_id = str(self.config["CURRENT_MODEL"])
        current_name = next((m["name"] for m in models if m["id"] == current_id), None)

        header = f"<b>🤖 Доступные модели</b>\n🧠 Текущая: <code>{html.escape(current_id)}</code>"
        if current_name and current_name != current_id:
            header += f" — {html.escape(current_name)}"
        header += f"\n📄 Стр {page + 1}/{total_pages}\n\n"

        text = header
        buttons = []
        for m in page_models:
            sel = "✅" if m["id"] == current_id else "▪️"
            desc = m.get("description") or ""
            if len(desc) > 140:
                desc = desc[:137] + "..."
            text += f"{sel} <b>{html.escape(m['name'])}</b>\n<code>{html.escape(m['id'])}</code>\n"
            if desc:
                text += f"{html.escape(desc)}\n"
            text += "\n"
            buttons.append([{"text": f"Выбрать {m['name']}", "callback": self._set_model_callback, "args": [m["id"], page]}])

        nav = []
        if page > 0:
            nav.append({"text": "◀️", "callback": self._page_callback, "args": [page - 1]})
        if page < total_pages - 1:
            nav.append({"text": "▶️", "callback": self._page_callback, "args": [page + 1]})
        if nav:
            buttons.append(nav)
        buttons.append([{"text": "❌ Закрыть", "action": "close"}])

        if getattr(target, "__class__", None).__name__ == "InlineCall":
            await target.edit(text, reply_markup=buttons)
        else:
            await self.inline.form(text=text, message=target, reply_markup=buttons)

    async def _page_callback(self, call, page: int):
        await self._show_models_page(call, page)

    async def _set_model_callback(self, call, model_id: str, page: int):
        self.config["CURRENT_MODEL"] = model_id
        try:
            await call.answer(f"✅ Установлена: {model_id}")
        except Exception:
            pass
        await self._show_models_page(call, page)

    async def _api_request(self, system_prompt, user_prompt):
        url = "https://api.onlysq.ru/ai/v2"
        headers = {
            "Authorization": f"Bearer {self.config['API_KEY']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        data = {
            "model": self.config["CURRENT_MODEL"],
            "request": {
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_output_tokens": int(self.config["MAX_TOKENS"]),
            },
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=data, timeout=300) as resp:
                    if resp.status != 200:
                        err_text = await resp.text()
                        return f"ERROR: HTTP {resp.status}\n{err_text}"
                    result = await resp.json(content_type=None)
                    content = None
                    try:
                        if isinstance(result, dict):
                            if "choices" in result and isinstance(result["choices"], list) and result["choices"]:
                                choice = result["choices"][0]
                                if isinstance(choice, dict):
                                    if "message" in choice and isinstance(choice["message"], dict):
                                        content = choice["message"].get("content")
                                    if content is None:
                                        content = choice.get("text") or choice.get("delta", {}).get("content")
                            if content is None and "message" in result:
                                msg = result["message"]
                                if isinstance(msg, dict):
                                    content = msg.get("content") or msg.get("text")
                            if content is None:
                                content = result.get("content") or result.get("result") or result.get("output")
                    except Exception:
                        content = None
                    if not content:
                        return f"ERROR: Empty response"
                    return self._clean_code(str(content))
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            return f"ERROR: {type(e).__name__}: {e}"
        except Exception as e:
            return f"ERROR: {e}"

    def _strip_code_fences(self, text: str) -> str:
        if not isinstance(text, str):
            return ""
        text = re.sub(r"^```[\w-]*\s*", "", text.strip())
        text = re.sub(r"\s*```$", "", text.strip())
        return text

    def _clean_code(self, text):
        return str(text).strip()

    async def _read_attached_text_from_message(self, message):
        try:
            if getattr(message, "document", None):
                file_bytes = await self.client.download_media(message, bytes)
                if not file_bytes:
                    return None
                try:
                    return file_bytes.decode("utf-8")
                except Exception:
                    return file_bytes.decode("utf-8", errors="ignore")
        except Exception:
            return None
        return None

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
                    name = node.name
                    if name.endswith("cmd"):
                        meta["commands"].add(name)
                    else:
                        meta["funcs"].add(name)
                    self.generic_visit(node)

                def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
                    name = node.name
                    if name.endswith("cmd"):
                        meta["commands"].add(name)
                    else:
                        meta["afuncs"].add(name)
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

            # Sets
            old_funcs_all = old_meta["funcs"] | old_meta["afuncs"]
            new_funcs_all = new_meta["funcs"] | new_meta["afuncs"]

            added_cmds = sorted(new_meta["commands"] - old_meta["commands"])
            removed_cmds = sorted(old_meta["commands"] - new_meta["commands"])

            added_funcs = sorted(new_funcs_all - old_funcs_all - (new_meta["commands"] - old_meta["commands"]))
            removed_funcs = sorted(old_funcs_all - new_funcs_all - (old_meta["commands"] - new_meta["commands"]))

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
                # Если не удалось выделить сущности — кратко показать, что были изменения
                old_lines = (old or "").splitlines()
                new_lines = (new or "").splitlines()
                diff = list(difflib.unified_diff(old_lines, new_lines, lineterm=""))
                added = sum(1 for ln in diff if ln.startswith("+") and not ln.startswith("+++"))
                removed = sum(1 for ln in diff if ln.startswith("-") and not ln.startswith("---"))
                if added or removed:
                    lines.append(f"• внёс правки по коду (строк: +{added} / -{removed})")
                else:
                    lines.append("• изменений не обнаружено")

            return "\n".join(lines)
        except Exception:
            return ""
