import asyncio
from pathlib import Path

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

from common.config import get_admin_token, get_admin_access_pass
from common.logging_config import setup_logger
from common.db import migrate, admin_is_authorized, admin_authorize

ROOT = Path(__file__).resolve().parents[1]
log = setup_logger("admin_bot", ROOT / "logs" / "admin.log")

router = Router()

@router.message(CommandStart())
async def start_handler(msg: Message):
    if admin_is_authorized(msg.chat.id):
        await msg.answer("Вы уже авторизованы. Введите /help")
    else:
        await msg.answer("Админ-бот. Для доступа отправьте секретный пароль отдельным сообщением.")

@router.message(Command("help"))
async def help_handler(msg: Message):
    if not admin_is_authorized(msg.chat.id):
        await msg.answer("Сначала авторизуйтесь — отправьте секретный пароль.")
        return
    await msg.answer(
        "Команды админа (MVP):\n"
        "/help — помощь\n"
        "(Далее добавим: список врачей, новые направления, расчёты, экспорт и т.д.)"
    )

@router.message()
async def auth_or_ignore(msg: Message):
    # Если уже авторизован — пока игнорируем прочий ввод
    if admin_is_authorized(msg.chat.id):
        return

    # Если прилетел текст — проверяем секрет
    secret = get_admin_access_pass()
    if msg.text and msg.text.strip() == secret:
        admin_authorize(msg.chat.id)
        await msg.answer("Авторизация успешна. Введите /help")
        log.info(f"Admin chat authorized: {msg.chat.id}")
    else:
        # Игнорируем всё, кроме /start и правильного пароля
        pass

async def main():
    migrate()
    bot = Bot(get_admin_token())
    dp = Dispatcher()
    dp.include_router(router)

    log.info("Admin bot starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
