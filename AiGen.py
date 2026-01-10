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
            "API_KEY", "openai", "🔑 API ключ OnlySq (или 'openai' для публичного доступа)",
            "CURRENT_MODEL", "google/gemini-2.0-flash-exp:free", "🧠 Модель по умолчанию",
            "MAX_TOKENS", 8000, "Максимум токенов для ответа"
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
            "⛔️ **CRITICAL OUTPUT RULES (VIOLATION = FAILURE):**\n"
            "1. RETURN ONLY RAW CODE. NO Markdown blocks (), NO backticks, NO intro/outro text.\n"
            "2. DO NOT overwrite core commands. FORBIDDEN NAMES: ['help', 'ping', 'info', 'id', 'dl', 'exec', 'eval', 'term', 'sh', 'restart', 'update', 'alias', 'modules', 'load', 'unload'].\n"
            "3. If the user asks for a common name (e.g., 'spam'), append a unique suffix (e.g., 'spam_pro' or 'spam_mod').\n"
            "4. Always use asynchronous programming (`async def`, `await`).\n\n"
            "🏗 **ARCHITECTURAL STANDARDS:**\n"
            "1. **Imports:** MUST start with `from .. import loader, utils`. Use `import asyncio`, `import io` only if needed.\n"
            "2. **Class:** Inherit from `loader.Module`. Use `@loader.tds` decorator for translation support.\n"
            "3. **Metadata:** Inside the class, define `strings = {'name': 'ModuleName'}`. ADD RUSSIAN TRANSLATIONS in `strings_ru` if possible.\n"
            "4. **Configuration:** If the module needs settings (API keys, IDs, prefixes), define `self.config` in `__init__` using `loader.ModuleConfig` and `loader.ConfigValue`.\n"
            "5. **Database:** To save data permanently, use `self.db.set(key, value)` and `self.db.get(key, default)`. NEVER use global variables.\n"
            "6. **Commands:** Methods must end with `cmd` (e.g., `async def examplecmd(self, message):`).\n"
            "   - Use `@loader.command()` decorator ONLY if specific translation keys are needed, otherwise standard naming is enough.\n"
            "7. **Interaction:**\n"
            "   - Get arguments: `args = utils.get_args_raw(message)`.\n"
            "   - Send/Edit answers: `await utils.answer(message, 'response')`. THIS IS MANDATORY. Do not use `message.edit()` directly unless necessary.\n"
            "   - Inline Buttons: Use `await self.inline.form(text='...', message=message, reply_markup=[...])` if requested.\n\n"
            "📝 **CODE SKELETON EXAMPLE:**\n"
            "from .. import loader, utils\n\n"
            "@loader.tds\n"
            "class ProModule(loader.Module):\n"
            "    \"\"\"Advanced module description\"\"\"\n"
            "    strings = {'name': 'ProModule', 'done': '<b>Done!</b>'}\n"
            "    strings_ru = {'done': '<b>Готово!</b>'}\n\n"
            "    def __init__(self):\n"
            "        self.config = loader.ModuleConfig(\n"
            "            'interval', 60, 'Update interval',\n"
            "            'status', True, 'Enable status'\n"
            "        )\n\n"
            "    async def client_ready(self, client, db):\n"
            "        self.client = client\n"
            "        self.db = db\n\n"
            "    async def runcmd(self, message):\n"
            "        \"\"\"<args> - Description of command\"\"\"\n"
            "        args = utils.get_args_raw(message)\n"
            "        await utils.answer(message, self.strings('done'))\n"
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
        text = re.sub(r"^\s*", "", text)
        text = re.sub(r"^\s*", "", text)
        text = re.sub(r"$", "", text)
        return text.strip()

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
