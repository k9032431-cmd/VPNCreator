import time

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..common import ROLE_TITLES, HasAccess, IsAdmin, Settings, db
from ..emoji import e
from ..ui import BLUE, RED, back, btn, kb, render

router = Router(name="settings")
router.message.filter(HasAccess())
router.callback_query.filter(HasAccess())

admin = Router(name="settings_admin")
admin.message.filter(IsAdmin())
admin.callback_query.filter(IsAdmin())
router.include_router(admin)


# ---------------- профиль (для всех) ----------------

@router.callback_query(F.data == "set:menu")
async def cb_menu(call: CallbackQuery, state: FSMContext, db_user: dict):
    await state.clear()
    date = time.strftime("%d.%m.%Y", time.localtime(db_user["created_at"]))
    await render(
        call,
        f"{e('settings')} <b>Настройки</b>\n\n"
        f"{e('user')} <b>Профиль</b>\n"
        f"{e('id')} ID: <code>{db_user['id']}</code>\n"
        f"{e('star')} Статус: {ROLE_TITLES.get(db_user['role'], db_user['role'])}\n"
        f"{e('servers')} Серверов: <b>{await db.count_servers(db_user['id'])}</b>\n"
        f"{e('keys')} Ключей: <b>{await db.count_keys(db_user['id'])}</b>\n"
        f"{e('calendar')} С нами с {date}",
        kb(back()),
    )


# ---------------- баннер (админ-панель) ----------------

@admin.callback_query(F.data == "set:banner")
async def cb_banner(call: CallbackQuery, state: FSMContext):
    await state.set_state(Settings.banner)
    has = bool(await db.get("banner"))
    await render(
        call,
        f"{e('image')} <b>Баннер главного меню</b>\n\n"
        f"Отправьте картинку (как фото) — она будет показываться над главным меню.\n"
        f"Рекомендуемый размер: 1280×720.\n\nСейчас: {'установлен' if has else 'нет'}",
        kb(btn("Убрать баннер", "set:bannerdel", emoji="trash", style=RED) if has else None, back("a:menu")),
    )


@admin.message(Settings.banner, F.photo)
async def in_banner(message: Message, state: FSMContext):
    await db.set("banner", message.photo[-1].file_id)
    await state.clear()
    await message.answer(f"{e('ok')} Баннер установлен.",
                         reply_markup=kb(btn("Админ-панель", "a:menu", emoji="admin", style=BLUE)))


@admin.message(Settings.banner)
async def in_banner_wrong(message: Message):
    await message.reply(f"{e('warn')} Отправьте картинку как фото. /cancel — отмена.")


@admin.callback_query(F.data == "set:bannerdel")
async def cb_banner_del(call: CallbackQuery, state: FSMContext):
    await db.set("banner", "")
    await state.clear()
    await call.answer("Баннер убран")
    await cb_banner(call, state)
