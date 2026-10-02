from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..common import HasAccess, db
from ..config import config
from ..emoji import e
from ..ui import BLUE, GREEN, RED, back, btn, kb, render

router = Router(name="start")


def main_menu(is_admin: bool):
    return kb(
        btn("Создать ключ", "k:new", emoji="key_new", style=GREEN),
        [btn("Мои ключи", "k:list:0", emoji="keys", style=BLUE),
         btn("Мои серверы", "s:list", emoji="servers", style=BLUE)],
        [btn("Подключить VPS", "s:add", emoji="server_add", style=BLUE),
         btn("Настройки", "set:menu", emoji="settings", style=BLUE)],
        [btn("Помощь", "m:help", emoji="help", style=RED),
         btn("Админ-панель", "a:menu", emoji="admin", style=GREEN) if is_admin else None],
    )


async def menu_text(user_id: int) -> str:
    servers = await db.count_servers(user_id)
    keys = await db.count_keys(user_id)
    return (
        f"{e('wave')} <b>{escape(config.bot_title)}</b> — создавайте VPN-ключи "
        f"на своих серверах в пару кликов\n\n"
        f"{e('servers')} Серверов: <b>{servers}</b>   {e('keys')} Ключей: <b>{keys}</b>\n\n"
        f"{e('point_down')} Выберите раздел:"
    )


async def show_menu(target: Message | CallbackQuery, db_user: dict) -> None:
    banner = await db.get("banner") or None
    await render(target, await menu_text(db_user["id"]), main_menu(db_user["role"] == "admin"), photo=banner)


def no_access_screen(db_user: dict):
    if db_user["role"] == "pending":
        text = (f"{e('loading')} <b>Заявка отправлена</b>\n\n"
                f"Администратор рассмотрит её в ближайшее время — бот пришлёт уведомление.")
        return text, kb(btn("Проверить статус", "m:home", emoji="refresh", style=BLUE))
    if db_user["role"] == "banned":
        return f"{e('ban')} <b>Доступ закрыт</b>\n\nОбратитесь к администратору.", None
    text = (f"{e('lock')} <b>{escape(config.bot_title)}</b>\n\n"
            f"У вас пока нет доступа к боту.\nОтправьте заявку — администратор её одобрит.\n\n"
            f"{e('id')} Ваш ID: <code>{db_user['id']}</code>")
    return text, kb(btn("Запросить доступ", "m:request", emoji="bell", style=GREEN))


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext, db_user: dict):
    await state.clear()
    if db_user["role"] in ("user", "admin"):
        await show_menu(message, db_user)
    else:
        text, markup = no_access_screen(db_user)
        await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "m:home")
async def cb_home(call: CallbackQuery, state: FSMContext, db_user: dict):
    await state.clear()
    if db_user["role"] in ("user", "admin"):
        await show_menu(call, db_user)
    else:
        text, markup = no_access_screen(db_user)
        await render(call, text, markup)


@router.callback_query(F.data == "m:cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext, db_user: dict):
    await cb_home(call, state, db_user)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, db_user: dict):
    await cmd_start(message, state, db_user)


@router.callback_query(F.data == "m:request")
async def cb_request(call: CallbackQuery, db_user: dict):
    if db_user["role"] != "new":
        if db_user["role"] in ("user", "admin"):
            return await show_menu(call, db_user)
        return await render(call, *no_access_screen(db_user))
    await db.set_role(db_user["id"], "pending")
    db_user = {**db_user, "role": "pending"}
    await render(call, *no_access_screen(db_user))
    u = call.from_user
    name = escape(u.full_name) + (f" (@{escape(u.username)})" if u.username else "")
    for admin_id in set(await db.admin_ids()) | config.admins:
        try:
            await call.bot.send_message(
                admin_id,
                f"{e('bell')} <b>Новая заявка на доступ</b>\n\n"
                f"{e('user')} {name}\n{e('id')} <code>{u.id}</code>",
                reply_markup=kb([btn("Одобрить", f"a:grant:{u.id}", emoji="ok", style=GREEN),
                                 btn("Отклонить", f"a:deny:{u.id}", emoji="cancel", style=RED)]),
            )
        except Exception:
            pass


HELP = f"""{e('help')} <b>Помощь</b>

<b>Как это работает</b>
{e('server_add')} <b>1. Подключите VPS</b> — укажите IP, порт, логин и пароль (или SSH-ключ). Бот проверит подключение и сохранит сервер. Пароли хранятся в зашифрованном виде.

{e('key_new')} <b>2. Создайте ключ</b> — выберите сервер и название ключа. При первом запуске бот сам загрузит скрипт на сервер и выполнит установку, затем создаст ключ.

{e('keys')} <b>3. Мои ключи</b> — все созданные ключи: посмотреть, скопировать, удалить.

{e('refresh')} Установка выполняется на сервере один раз. Повторно — только если администратор обновил скрипт или команды.

{e('tip')} Отменить любое действие — /cancel"""


@router.callback_query(F.data == "m:help", HasAccess())
async def cb_help(call: CallbackQuery):
    await render(call, HELP, kb(back()))


@router.message(Command("help"), HasAccess())
async def cmd_help(message: Message):
    await message.answer(HELP, reply_markup=kb(back()))
