__version__ = (1, 0, 27)
# meta banner: https://yufic.ru/api/hc/?a=Timer&b=@PluginIDEbot
# meta developer: @Sk0lovek & @kchemniy
# scope: hikka_only
# scope: hikka_min 1.2.10

import asyncio
import re
from telethon.tl.types import Message
from telethon.errors import FloodWaitError
from .. import loader, utils

def _parse_time_string(time_str: str) -> int:
    """
    Парсит строку времени (например, "10s", "5m 30s", "2h 10m", "1d 3h") и возвращает общее количество секунд.
    Поддерживает комбинации единиц: s, m, h, d.
    """
    total_seconds = 0
    # Разбиваем на части по пробелам и парсим каждую
    parts = re.split(r'\s+', time_str.strip())
    for part in parts:
        match = re.match(r'(\d+)([smhd])', part)
        if not match:
            raise ValueError(f"Неверный формат части времени: '{part}'. Используйте '10s', '5m' и т.д.")
        
        value = int(match.group(1))
        unit = match.group(2).lower()

        if unit == 's':
            total_seconds += value
        elif unit == 'm':
            total_seconds += value * 60
        elif unit == 'h':
            total_seconds += value * 60 * 60
        elif unit == 'd':
            total_seconds += value * 24 * 60 * 60
        else:
            raise ValueError(f"Неизвестная единица: '{unit}'.")
    
    if total_seconds <= 0:
        raise ValueError("Время должно быть положительным.")
    
    return total_seconds

def _format_seconds_to_hms(seconds: int) -> str:
    """
    Форматирует общее количество секунд в строку HH:MM:SS.
    """
    seconds = max(0, seconds)  # Гарантируем, что не будет отрицательного времени
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

@loader.tds
class TimerMod(loader.Module):
    """
    Модуль для создания таймеров с интерактивным управлением.
    Позволяет ставить таймер на паузу, возобновлять и сбрасывать его.
    """
    strings = {
        "name": "Timer",
        "no_time_arg": (
            "❌ Не указано время для таймера.\n"
            "Используйте формат: .timer {время} [текст сообщения]\n"
            "Поддержка комбинаций: {10m 5s}, {2h 30m} и т.д.\n"
            "Пример: .timer 10m Привет, это тест!\n"
            "Таймер будет отображаться на инлайн-кнопке (пауза/возобновить) и обновляться каждую секунду."
        ),
        "invalid_time_format": (
            "❌ Неверный формат времени: {}\n"
            "Используйте формат: {10s}, {5m}, {2h 30m}, {1d 3h} и т.д."
        ),
        "too_many_timers": "❌ Слишком много таймеров в чате (макс. 5).",
        "timer_inactive": "Этот таймер больше не активен.",
        "timer_reset": "Таймер сброшен.",
        "timer_paused": "Таймер приостановлен",
        "timer_resumed": "Таймер возобновлен",
        "failed_to_reset": "Не удалось сбросить таймер: {}",
        "no_active_timers": "Нет активных таймеров для остановки.",
        "all_timers_stopped": "Все {0} таймера остановлены и удалены.",
        "config_running_timer_emoji_doc": "Emoji для кнопки таймера, когда он активен (по умолчанию: ⏸️).",
        "config_paused_timer_emoji_doc": "Emoji для кнопки таймера, когда он приостановлен (по умолчанию: ▶️).",
        "config_reset_button_emoji_doc": "Emoji для кнопки сброса таймера (по умолчанию: ⏹️).",
        "_cls_doc": "Устанавливает таймер: inline.form с текстом и кнопками \"Пауза/Возобновить\" и \"Сброс\" (появляется при паузе). При перезагрузке бота таймеры восстанавливаются.",
        "_cmd_doc_timer": (
            "Устанавливает таймер.\n"
            "Формат: .timer {время} [текст]\n"
            "Пример: .timer 10m Тест!\n"
            "Кнопка таймера позволяет ставить на паузу/возобновлять, кнопка сброса (видна при паузе) удаляет таймер. Исходная команда удаляется."
        ),
        "_cmd_doc_stoptimer": "Останавливает и удаляет все активные таймеры, запущенные модулем."
    }

    strings_ru = {
        "no_time_arg": (
            "❌ Не указано время для таймера.\n"
            "Используйте формат: .timer {время} [текст сообщения]\n"
            "Поддержка комбинаций: {10m 5s}, {2h 30m} и т.д.\n"
            "Пример: .timer 10m Привет, это тест!\n"
            "Таймер будет отображаться на инлайн-кнопке (пауза/возобновить) и обновляться каждую секунду."
        ),
        "invalid_time_format": (
            "❌ Неверный формат времени: {}\n"
            "Используйте формат: {10s}, {5m}, {2h 30m}, {1d 3h} и т.д."
        ),
        "too_many_timers": "❌ Слишком много таймеров в чате (макс. 5).",
        "timer_inactive": "Этот таймер больше не активен.",
        "timer_reset": "Таймер сброшен.",
        "timer_paused": "Таймер приостановлен",
        "timer_resumed": "Таймер возобновлен",
        "failed_to_reset": "Не удалось сбросить таймер: {}",
        "no_active_timers": "Нет активных таймеров для остановки.",
        "all_timers_stopped": "Все {0} таймера остановлены и удалены.",
        "config_running_timer_emoji_doc": "Emoji для кнопки таймера, когда он активен (по умолчанию: ⏸️).",
        "config_paused_timer_emoji_doc": "Emoji для кнопки таймера, когда он приостановлен (по умолчанию: ▶️).",
        "config_reset_button_emoji_doc": "Emoji для кнопки сброса таймера (по умолчанию: ⏹️).",
        "_cls_doc": "Устанавливает таймер: inline.form с текстом и кнопками \"Пауза/Возобновить\" и \"Сброс\" (появляется при паузе). При перезагрузке бота таймеры восстанавливаются.",
        "_cmd_doc_timer": (
            "Устанавливает таймер.\n"
            "Формат: .timer {время} [текст]\n"
            "Пример: .timer 10m Тест!\n"
            "Кнопка таймера позволяет ставить на паузу/возобновлять, кнопка сброса (видна при паузе) удаляет таймер. Исходная команда удаляется."
        ),
        "_cmd_doc_stoptimer": "Останавливает и удаляет все активные таймеры, запущенные модулем."
    }

    def __init__(self):
        # {form_id: {'text': str, 'total_duration': int, 'remaining': int, 'chat_id': int, 'form_obj': obj, 'is_paused': bool, 'task': asyncio.Task, 'resume_event': asyncio.Event, 'render_fails': int, 'prev_paused': bool}}
        self.timers = {} 
        self.config = loader.ModuleConfig(
            loader.ConfigValue(
                "running_timer_emoji",
                "⏸️",  # Emoji to show when timer is running
                lambda: self.strings("config_running_timer_emoji_doc"),
                validator=loader.validators.String(),
            ),
            loader.ConfigValue(
                "paused_timer_emoji",
                "▶️",  # Emoji to show when timer is paused
                lambda: self.strings("config_paused_timer_emoji_doc"),
                validator=loader.validators.String(),
            ),
            loader.ConfigValue(
                "reset_button_emoji",
                "⏹️",  # Emoji for the reset button
                lambda: self.strings("config_reset_button_emoji_doc"),
                validator=loader.validators.String(),
            ),
        )


    async def client_ready(self, client, db):
        self.client = client
        self.db = db

        saved_timers = self.db.get("TimerMod", "active_timers", {})
        updated_saved_timers = {}  # Для сохранения только валидных таймеров с новыми ID
        has_changes = False

        for form_id_str, data in saved_timers.items():
            try:
                old_form_id = int(form_id_str)
                if old_form_id in self.timers:
                    continue  # Уже обработан

                # Проверяем формат data: ожидается кортеж/список из ровно 4 элементов
                if not isinstance(data, (tuple, list)) or len(data) != 4:
                    print(f"Skipping invalid saved timer {form_id_str}: data has {len(data) if isinstance(data, (tuple, list)) else 'unknown'} elements, expected 4.")
                    has_changes = True
                    continue

                original_text, remaining_seconds, chat_id, is_paused_state = data
                
                if remaining_seconds > 0:
                    # Create a fallback message as we can't restore InlineForm directly
                    fallback_msg = await self.client.send_message(chat_id, original_text)
                    new_form_id = fallback_msg.id  # Новый ID для восстановленного таймера
                    
                    # Create resume_event for the restored timer
                    resume_event = asyncio.Event()
                    if is_paused_state:
                        resume_event.clear()  # Ensure it's cleared if paused on restore
                    
                    # Store timer data with the new message object and NEW form_id
                    self.timers[new_form_id] = {
                        'text': original_text,
                        'total_duration': remaining_seconds, # On restore, total_duration becomes remaining_seconds
                        'remaining': remaining_seconds,
                        'chat_id': chat_id,
                        'form_obj': fallback_msg,
                        'is_paused': is_paused_state,
                        'task': None, # Task will be set when _run_timer starts
                        'resume_event': resume_event,
                        'render_fails': 0,  # Counter for failed renders
                        'prev_paused': is_paused_state # Previous pause state for render check
                    }
                    
                    # Render initial state with correct buttons using NEW form_id
                    await self._render_timer_buttons(fallback_msg, original_text, remaining_seconds, is_paused_state, new_form_id)

                    # Start the timer task and store its handle with NEW form_id
                    task_handle = asyncio.create_task(self._run_timer(new_form_id))
                    self.timers[new_form_id]['task'] = task_handle

                    # Сохраняем валидный таймер с НОВЫМ ключом в обновленной БД
                    updated_saved_timers[str(new_form_id)] = data
                else:
                    # If remaining_seconds is 0 or less, it means timer finished, remove from DB
                    has_changes = True

            except (ValueError, TypeError) as e:
                print(f"Skipping saved timer {form_id_str} due to unpack error: {e}")
                has_changes = True
                continue

        # Обновляем БД только если были изменения (удалены некорректные таймеры или созданы новые)
        if has_changes or updated_saved_timers:
            self.db.set("TimerMod", "active_timers", updated_saved_timers)


    async def _save_timers(self):
        """Сохраняет текущее состояние таймеров в базу данных (без form_obj и task)."""
        timers_to_save = {}
        for form_id, timer_data in self.timers.items():
            # Проверяем наличие всех необходимых ключей перед сохранением
            required_keys = ['text', 'remaining', 'chat_id', 'is_paused']
            if all(key in timer_data for key in required_keys) and timer_data['remaining'] > 0:
                timers_to_save[form_id] = (timer_data['text'], timer_data['remaining'], timer_data['chat_id'], timer_data['is_paused'])
        self.db.set("TimerMod", "active_timers", timers_to_save)

    async def _render_timer_buttons(self, form_or_msg, original_text, remaining_seconds, is_paused, form_id):
        """Helper to render or update the timer message buttons."""
        timer_text_formatted = _format_seconds_to_hms(remaining_seconds)
        
        timer_emoji = self.config["paused_timer_emoji"] if is_paused else self.config["running_timer_emoji"]
        timer_display_text = f"{timer_emoji} {timer_text_formatted}"

        buttons = [
            [{"text": timer_display_text, "callback": self._toggle_timer_callback, "args":(form_id,)}],
        ]
        
        # Кнопка сброса появляется только когда таймер приостановлен
        if is_paused:
            buttons.append([{"text": f"{self.config['reset_button_emoji']} Сбросить", "callback": self._reset_timer_callback, "args":(form_id,)}])

        try:
            if hasattr(form_or_msg, 'edit'):
                await form_or_msg.edit(original_text, reply_markup=buttons)
            else:
                # Fallback for plain Message objects (e.g., from client_ready)
                msg_entity = await self.client.get_messages(self.timers[form_id]['chat_id'], ids=form_id)
                if msg_entity:
                    await msg_entity.edit(original_text, buttons=buttons)
                    # Update the stored form_obj if we successfully found and edited it
                    self.timers[form_id]['form_obj'] = msg_entity
                else:
                    # If message not found (deleted?), remove timer
                    print(f"Message for timer {form_id} not found during render, removing timer.")
                    if form_id in self.timers:
                        del self.timers[form_id]
                    await self._save_timers()
                    return  # Exit to avoid further errors
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds)  # Авто-сон при flood
            # Retry once after sleep
            if hasattr(form_or_msg, 'edit'):
                await form_or_msg.edit(original_text, reply_markup=buttons)
            else:
                msg_entity = await self.client.get_messages(self.timers[form_id]['chat_id'], ids=form_id)
                if msg_entity:
                    await msg_entity.edit(original_text, buttons=buttons)
                    self.timers[form_id]['form_obj'] = msg_entity
                else:
                    print(f"Retry failed: Message for timer {form_id} not found, removing timer.")
                    if form_id in self.timers:
                        del self.timers[form_id]
                    await self._save_timers()
                    return
        except Exception as e:
            print(f"Failed to render buttons for timer {form_id}: {e}")
            # Increment fail counter
            if form_id in self.timers:
                timer_data = self.timers[form_id]
                timer_data['render_fails'] = timer_data.get('render_fails', 0) + 1
                if timer_data['render_fails'] >= 3:
                    print(f"Too many render fails for timer {form_id}, removing it.")
                    del self.timers[form_id]
                    await self._save_timers()
                self.timers[form_id] = timer_data
            # For now, continue without removing to allow recovery.


    async def _toggle_timer_callback(self, call, form_id: int):
        """Callback to pause/resume the timer."""
        timer_data = self.timers.get(form_id)
        if not timer_data or timer_data['remaining'] <= 0:
            await call.answer(self.strings("timer_inactive"))
            return

        # Toggle the pause state
        new_paused = not timer_data['is_paused']
        timer_data['is_paused'] = new_paused
        timer_data['prev_paused'] = new_paused  # Update prev_paused to new state for immediate consistency
        
        # If resuming (new_paused=False), signal the event to wake the task immediately
        if not new_paused:
            timer_data['resume_event'].set()
        else:
            timer_data['resume_event'].clear()  # Clear for next pause cycle
        
        self.timers[form_id] = timer_data # Update the state

        # Re-render the board immediately to show the new state of buttons
        try:
            await self._render_timer_buttons(timer_data['form_obj'], timer_data['text'], timer_data['remaining'], new_paused, form_id)
        except Exception as e:
            print(f"Render failed in toggle for timer {form_id}: {e}")
            # Retry once after short delay
            await asyncio.sleep(0.5)
            try:
                await self._render_timer_buttons(timer_data['form_obj'], timer_data['text'], timer_data['remaining'], new_paused, form_id)
            except Exception as retry_e:
                print(f"Retry render also failed for timer {form_id}: {retry_e}")
        
        await self._save_timers()  # Persist after render

        try:
            await call.answer(self.strings("timer_paused") if new_paused else self.strings("timer_resumed"))
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds)  # Handle flood in answer
            await call.answer(self.strings("timer_paused") if new_paused else self.strings("timer_resumed"))

    async def _reset_timer_callback(self, call, form_id: int):
        """Callback to reset (delete) the timer message."""
        timer_data = self.timers.get(form_id)
        if not timer_data:
            await call.answer(self.strings("timer_inactive"))
            return

        # Cancel the associated asyncio task
        if timer_data['task']:
            timer_data['task'].cancel()

        try:
            # Delete the message itself
            if hasattr(timer_data['form_obj'], 'delete'):
                await timer_data['form_obj'].delete()
            else:
                await self.client.delete_messages(timer_data['chat_id'], [form_id])
            await call.answer(self.strings("timer_reset"))
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds)  # Auto-sleep on flood
            if hasattr(timer_data['form_obj'], 'delete'):
                await timer_data['form_obj'].delete()
            else:
                await self.client.delete_messages(timer_data['chat_id'], [form_id])
            await call.answer(self.strings("timer_reset"))
        except Exception as e:
            await call.answer(self.strings("failed_to_reset").format(e))
            print(f"Failed to delete timer {form_id} on reset: {e}")
        finally:
            if form_id in self.timers:
                del self.timers[form_id]
            await self._save_timers() # Ensure it's removed from DB

    async def _run_timer(self, form_id: int):
        """
        Асинхронная функция для обратного отсчёта с обновлением кнопки.
        """
        try:
            resume_event = self.timers[form_id]['resume_event']
            while True:
                timer_data = self.timers.get(form_id)
                if not timer_data:
                    break # Timer was removed/reset externally

                original_text = timer_data['text']
                remaining = timer_data['remaining']
                form_or_msg = timer_data['form_obj']
                is_paused = timer_data['is_paused']
                prev_paused = timer_data.get('prev_paused', False)

                if remaining <= 0:
                    break # Timer finished

                # На паузе: ждем либо сигнала resume_event, либо 1с таймаута
                if is_paused:
                    done, pending = await asyncio.wait(
                        [resume_event.wait(), asyncio.sleep(1)],
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    # If resume_event finished, it means we resumed, clear pending
                    for p in pending:
                        p.cancel()
                    # Render only if pause state changed (should be handled in callback, but safety)
                    render_needed = (is_paused != prev_paused)
                    if render_needed:
                        await self._render_timer_buttons(form_or_msg, original_text, remaining, is_paused, form_id)
                        timer_data['prev_paused'] = is_paused
                        self.timers[form_id] = timer_data
                    continue  # Back to loop to check new is_paused

                # Если running: всегда рендерим каждую секунду для гарантии обновления
                try:
                    await self._render_timer_buttons(form_or_msg, original_text, remaining, is_paused, form_id)
                except Exception as e:
                    print(f"Render failed in run_timer for {form_id}: {e}")
                    timer_data['render_fails'] = timer_data.get('render_fails', 0) + 1
                    if timer_data['render_fails'] >= 3:
                        print(f"Too many render fails for timer {form_id}, auto-removing.")
                        del self.timers[form_id]
                        await self._save_timers()
                        break
                    self.timers[form_id] = timer_data

                # Декрементируем remaining
                remaining -= 1
                timer_data['remaining'] = remaining
                self.timers[form_id] = timer_data
                await self._save_timers() # Сохраняем каждую секунду для персистентности

                if remaining > 0:
                    await asyncio.sleep(1) # Ждем 1 секунду перед следующим обновлением
                else:
                    break # Выходим без лишнего sleep на 0
            
            # Таймер закончился или был отменен, выполняем финальное удаление (если еще не удален)
            if form_id in self.timers:
                timer_data = self.timers[form_id]
                original_text = timer_data['text']
                form_or_msg_final = timer_data['form_obj']
                chat_id = timer_data['chat_id']

                # Убедимся, что на кнопке отображается 00:00:00 перед удалением
                try:
                    await self._render_timer_buttons(form_or_msg_final, original_text, 0, False, form_id)
                    await asyncio.sleep(1) # Дадим время, чтобы отобразилось 0
                except Exception as e:
                    print(f"Final render failed for timer {form_id}: {e}")

                try:
                    if hasattr(form_or_msg_final, 'delete'):
                        await form_or_msg_final.delete()
                    else:
                        await self.client.delete_messages(chat_id, [form_id])
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds)
                    if hasattr(form_or_msg_final, 'delete'):
                        await form_or_msg_final.delete()
                    else:
                        await self.client.delete_messages(chat_id, [form_id])
                except Exception as e:
                    print(f"Delete failed for timer {form_id}: {e}")

        except asyncio.CancelledError:
            print(f"Timer task {form_id} was cancelled.")
            # Удаление сообщения обрабатывается в _reset_timer_callback
        except Exception as e:
            print(f"Error in _run_timer for {form_id}: {e}")
        finally:
            if form_id in self.timers: # Убедимся, что таймер удален из списка активных
                del self.timers[form_id]
            await self._save_timers() # Финальное сохранение, чтобы удалить завершенный таймер из БД

    @loader.command()
    async def timer(self, message: Message):
        """
        Устанавливает таймер с интерактивными кнопками паузы/возобновления и сброса.
        """
        raw_args = utils.get_args_raw(message)

        # Новая регулярка для формата "время текст"
        time_pattern = r'^(\S+)\s*(.*)' 
        time_match = re.match(time_pattern, raw_args)

        if not time_match:
            return await utils.answer(message, self.strings("no_time_arg"))

        duration_str_parsed = time_match.group(1).strip()
        original_text = time_match.group(2).strip()

        if not original_text:
            return await utils.answer(message, self.strings("no_time_arg"))

        try:
            delay_seconds = _parse_time_string(duration_str_parsed)
        except ValueError as e:
            return await utils.answer(message, self.strings("invalid_time_format").format(str(e)))

        # Лимит на активные таймеры в чате
        active_in_chat = sum(1 for fid, data in self.timers.items() if data['chat_id'] == message.chat_id)
        if active_in_chat >= 5:
            await utils.answer(message, self.strings("too_many_timers"))
            return

        # Создаём inline.form
        # Initial buttons will be rendered by _render_timer_buttons after form creation
        # Используем message.id как временный form_id для начального рендера, он будет обновлен реальным form.id
        # после создания формы.
        initial_form_id = message.id 
        initial_buttons = [
            [{"text": f"{self.config['running_timer_emoji']} {_format_seconds_to_hms(delay_seconds)}", "callback": self._toggle_timer_callback, "args":(initial_form_id,)}],
        ]

        timer_form = await self.inline.form(
            message=message,
            text=original_text,
            reply_markup=initial_buttons, # Передаем начальные кнопки для формы
            silent=True
        )

        form_id = timer_form.id if hasattr(timer_form, 'id') else message.id 
        
        # Create resume_event for the new timer
        resume_event = asyncio.Event()
        resume_event.clear()  # Initial state: not waiting
        
        # Store initial timer data
        self.timers[form_id] = {
            'text': original_text,
            'total_duration': delay_seconds,
            'remaining': delay_seconds,
            'chat_id': message.chat_id,
            'form_obj': timer_form,
            'is_paused': False,
            'task': None, # Task will be set below
            'resume_event': resume_event,
            'render_fails': 0,  # Counter for failed renders
            'prev_paused': False # Initial pause state
        }

        # Start the timer task and store its handle
        task_handle = asyncio.create_task(self._run_timer(form_id))
        self.timers[form_id]['task'] = task_handle

        await self._save_timers()

        # Update message with correct form_id in buttons
        # Эта функция теперь также отвечает за добавление кнопки сброса при необходимости
        await self._render_timer_buttons(timer_form, original_text, delay_seconds, False, form_id)

        # Удаляем исходную команду
        try:
            await message.delete()
        except Exception:
            pass # Игнорируем

    @loader.command()
    async def stoptimer(self, message: Message):
        """Останавливает и удаляет все активные таймеры."""
        if not self.timers:
            await utils.answer(message, self.strings("no_active_timers"))
            return

        stopped_count = 0
        timer_ids_to_stop = list(self.timers.keys()) # Iterate over a copy to avoid RuntimeError on dict change

        for form_id in timer_ids_to_stop:
            timer_data = self.timers.get(form_id)
            if timer_data:
                # Cancel task
                if timer_data['task']:
                    timer_data['task'].cancel()
                
                # Delete message
                try:
                    if hasattr(timer_data['form_obj'], 'delete'):
                        await timer_data['form_obj'].delete()
                    else:
                        await self.client.delete_messages(timer_data['chat_id'], [form_id])
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds)
                    if hasattr(timer_data['form_obj'], 'delete'):
                        await timer_data['form_obj'].delete()
                    else:
                        await self.client.delete_messages(timer_data['chat_id'], [form_id])
                except Exception as e:
                    print(f"Failed to delete timer message {form_id} during stoptimer: {e}")
                
                # Remove from tracking
                if form_id in self.timers: # Ensure it's still there before deleting
                    del self.timers[form_id]
                stopped_count += 1
        
        await self._save_timers() # Persist the empty/reduced state
        await utils.answer(message, self.strings("all_timers_stopped").format(stopped_count))
        
        # Удаляем исходную команду
        try:
            await message.delete()
        except Exception:
            pass # Игнорируем