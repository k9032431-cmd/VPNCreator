import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, CallbackQuery, Message

from . import updater
from .common import UserMiddleware, db
from .config import config
from .emoji import e
from .handlers import admin, ipchange, keys, scripts, servers, settings, start

fallback = Router(name="fallback")


@fallback.callback_query()
async def unknown_callback(call: CallbackQuery, db_user: dict | None = None):
    if db_user and db_user["role"] not in ("user", "admin"):
        return await call.answer("Нет доступа. Нажмите /start", show_alert=True)
    await call.answer("Кнопка устарела — откройте /menu")


@fallback.message(F.chat.type == "private")
async def unknown_message(message: Message, db_user: dict | None = None):
    if db_user and db_user["role"] in ("user", "admin"):
        await message.answer(f"{e('info')} Не понял. Откройте меню — /menu")
    else:
        await message.answer(f"{e('lock')} Нет доступа. Нажмите /start")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await db.connect()
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(UserMiddleware())
    dp.callback_query.outer_middleware(UserMiddleware())
    dp.include_routers(start.router, admin.router, scripts.router, settings.router, servers.router,
                       ipchange.router, keys.router, fallback)
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="help", description="Помощь"),
        BotCommand(command="cancel", description="Отменить действие"),
    ])
    watcher = asyncio.create_task(updater.watcher(bot))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        watcher.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
