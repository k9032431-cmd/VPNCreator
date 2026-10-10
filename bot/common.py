from aiogram import BaseMiddleware
from aiogram.filters import Filter
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, TelegramObject

from .config import config
from .db import Database

db = Database(config.db_path)

ROLE_TITLES = {
    "admin": "Админ",
    "user": "Есть доступ",
    "pending": "Ожидает одобрения",
    "new": "Нет доступа",
    "banned": "Заблокирован",
}


class UserMiddleware(BaseMiddleware):
    """Сохраняет пользователя в БД и передаёт его в хендлеры как db_user."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user and not user.is_bot:
            data["db_user"] = await db.sync_user(
                user.id, user.username, user.full_name, user.id in config.admins)
        return await handler(event, data)


class HasAccess(Filter):
    async def __call__(self, event: Message | CallbackQuery, db_user: dict | None = None) -> bool:
        return bool(db_user) and db_user["role"] in ("user", "admin")


class IsAdmin(Filter):
    async def __call__(self, event: Message | CallbackQuery, db_user: dict | None = None) -> bool:
        return bool(db_user) and db_user["role"] == "admin"


class AddServer(StatesGroup):
    name = State()
    host = State()
    port = State()
    username = State()
    auth = State()
    secret = State()
    passphrase = State()


class ChangeIP(StatesGroup):
    host = State()


class CreateKey(StatesGroup):
    name = State()
    bulk = State()


class Settings(StatesGroup):
    banner = State()


class ScriptForm(StatesGroup):
    name = State()
    file = State()
    steps = State()
    check = State()
    result_type = State()
    result_path = State()
    result_regex = State()


class Admin(StatesGroup):
    grant_id = State()


def fmt_size(n: int) -> str:
    return f"{n} Б" if n < 1024 else f"{n / 1024:.1f} КБ"
