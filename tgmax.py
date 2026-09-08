# name: TG Forwarder (PyMax)
# version: 2.0.0
# description: Пересылка сообщений в TG с отправкой ошибок в Избранное
# developer: AI Assistant

import aiohttp
from pymax import Client, ClientRouter, Message

# Создаем роутер для модуля, как того требует PyMax
router = ClientRouter()

# === НАСТРОЙКИ ===
# ID чата в Max Messenger, из которого мы перехватываем сообщения
TARGET_MAX_CHAT_ID = "ID_ВАШЕГО_ЧАТА_В_MAX" 

# Настройки для прямой отправки в Telegram
TG_BOT_TOKEN = "ВАШ_ТОКЕН_БОТА_В_ТГ"
TG_CHAT_ID = "ID_ЧАТА_ИЛИ_КАНАЛА_В_ТГ"
# =================

# Регистрируем обработчик на все входящие сообщения через декоратор роутера
@router.on_message()
async def tg_forwarder_handler(message: Message, client: Client) -> None:
    # 1. Проверяем, из нужного ли чата пришло сообщение
    if str(message.chat_id) != TARGET_MAX_CHAT_ID:
        return
        
    # 2. Проверяем, есть ли текст в сообщении
    if not message.text:
        return
        
    # Извлекаем отправителя (в PyMax это свойство message.sender)
    sender = str(getattr(message, "sender", "Неизвестно"))
    
    try:
        async with aiohttp.ClientSession() as session:
            # Формируем красивое сообщение для отправки в Telegram
            tg_text = f"📩 <b>Новое сообщение в Max</b>\n👤 От: <code>{sender}</code>\n\n{message.text}"
            tg_url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
            tg_payload = {
                "chat_id": TG_CHAT_ID,
                "text": tg_text,
                "parse_mode": "HTML"
            }
            
            # Отправляем в TG
            async with session.post(tg_url, json=tg_payload) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    raise Exception(f"Ошибка Telegram API ({resp.status}): {err_text}")
                    
    except Exception as e:
        # === ОБРАБОТКА ОШИБОК ===
        # Получаем свой собственный ID (Избранное) через новую структуру PyMax
        me_id = client.me.contact.id if client.me else None
        
        error_msg = (
            f"⚠️ **Ошибка модуля TG Forwarder**\n"
            f"Не удалось переслать сообщение из чата `{message.chat_id}`.\n\n"
            f"**Текст ошибки:**\n`{str(e)}`"
        )
        
        if me_id:
            try:
                # Отправляем сообщение с ошибкой самому себе (в Избранное)
                # В большинстве подобных API метод называется send_message
                await client.send_message(me_id, error_msg)
            except Exception as send_err:
                print(f"[TG Forwarder] Критическая ошибка отправки в Избранное: {e} | {send_err}")
        else:
            print(f"[TG Forwarder] Ошибка: {e} (не удалось получить me_id)")

# Функция регистрации модуля (в зависимости от загрузчика Maxli может потребоваться)
def register(client: Client):
    """Подключает роутер к основному клиенту при загрузке модуля."""
    client.include_router(router)