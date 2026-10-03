import time
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import updater
from ..common import ROLE_TITLES, Admin, IsAdmin, db
from ..config import config
from ..emoji import e
from ..ui import BLUE, GREEN, RED, back, btn, kb, render

router = Router(name="admin")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

PAGE = 8


def user_title(u: dict) -> str:
    name = u.get("full_name") or "Без имени"
    return name + (f" (@{u['username']})" if u.get("username") else "")


@router.callback_query(F.data == "a:menu")
async def cb_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    pending = await db.count_users(("pending",))
    scripts = len(await db.list_scripts())
    await render(
        call,
        f"{e('admin')} <b>Админ-панель</b>\n\n"
        f"{e('users')} Пользователей с доступом: <b>{await db.count_users(('user', 'admin'))}</b>\n"
        f"{e('bell')} Заявок: <b>{pending}</b>\n"
        f"{e('servers')} Серверов: <b>{await db.count_servers()}</b>\n"
        f"{e('keys')} Ключей: <b>{await db.count_keys()}</b>\n"
        f"{e('file')} Скриптов: <b>{scripts}</b>",
        kb(
            [btn("Пользователи", "a:users:0", emoji="users", style=BLUE),
             btn(f"Заявки ({pending})", "a:pending:0", emoji="bell", style=GREEN if pending else BLUE)],
            [btn("Выдать доступ по ID", "a:grantid", emoji="id", style=BLUE),
             btn("Заблокированные", "a:banned:0", emoji="ban", style=BLUE)],
            [btn(f"Скрипты VPN ({scripts})", "sc:list", emoji="file", style=GREEN),
             btn("Баннер", "set:banner", emoji="image", style=BLUE)],
            btn("Обновить бота", "a:upd", emoji="refresh", style=GREEN),
            back(),
        ),
    )


LISTS = {
    "users": (("user", "admin"), "Пользователи с доступом", "users"),
    "pending": (("pending",), "Заявки на доступ", "bell"),
    "banned": (("banned",), "Заблокированные", "ban"),
}


@router.callback_query(F.data.regexp(r"^a:(users|pending|banned):\d+$"))
async def cb_users(call: CallbackQuery):
    _, kind, page = call.data.split(":")
    page = int(page)
    roles, title, emoji = LISTS[kind]
    total = await db.count_users(roles)
    users = await db.list_users(roles, page * PAGE, PAGE)
    pages = max(1, (total + PAGE - 1) // PAGE)
    rows = [btn(user_title(u), f"a:u:{u['id']}", emoji="star" if u["role"] == "admin" else "user", style=BLUE)
            for u in users]
    nav = [
        btn("Назад", f"a:{kind}:{page - 1}", emoji="back") if page > 0 else None,
        btn(f"{page + 1}/{pages}", f"a:{kind}:{page}") if pages > 1 else None,
        btn("Далее", f"a:{kind}:{page + 1}", emoji="next") if page + 1 < pages else None,
    ]
    text = f"{e(emoji)} <b>{title}</b> — {total}" + ("" if total else "\n\nПока пусто.")
    await render(call, text, kb(*rows, nav, back("a:menu")))


async def user_card(call: CallbackQuery, uid: int):
    u = await db.get_user(uid)
    if not u:
        return await call.answer("Пользователь не найден", show_alert=True)
    date = time.strftime("%d.%m.%Y", time.localtime(u["created_at"]))
    text = (
        f"{e('user')} <b>{escape(user_title(u))}</b>\n\n"
        f"{e('id')} ID: <code>{u['id']}</code>\n"
        f"{e('star')} Статус: {ROLE_TITLES.get(u['role'], u['role'])}\n"
        f"{e('servers')} Серверов: <b>{await db.count_servers(uid)}</b>\n"
        f"{e('keys')} Ключей: <b>{await db.count_keys(uid)}</b>\n"
        f"{e('calendar')} Регистрация: {date}"
    )
    env_admin = uid in config.admins
    role = u["role"]
    rows = []
    if env_admin:
        text += f"\n\n{e('info')} Главный админ (из .env) — изменить нельзя."
    else:
        if role in ("new", "pending", "banned"):
            rows.append(btn("Выдать доступ", f"a:grant:{uid}", emoji="ok", style=GREEN))
        if role == "user":
            rows.append(btn("Сделать админом", f"a:mkadmin:{uid}", emoji="star", style=GREEN))
        if role == "admin":
            rows.append(btn("Снять админа", f"a:grant:{uid}", emoji="star", style=BLUE))
        if role == "pending":
            rows.append(btn("Отклонить заявку", f"a:deny:{uid}", emoji="cancel", style=RED))
        if role != "banned":
            rows.append(btn("Забрать доступ / заблокировать", f"a:ban:{uid}", emoji="ban", style=RED))
    await render(call, text, kb(*rows, back("a:users:0")))


@router.callback_query(F.data.startswith("a:u:"))
async def cb_user(call: CallbackQuery):
    await user_card(call, int(call.data.split(":")[2]))


async def _notify(call_or_msg, uid: int, text: str, markup=None):
    try:
        await call_or_msg.bot.send_message(uid, text, reply_markup=markup)
    except Exception:
        pass


ACTIONS = {
    "grant": ("user", "Доступ выдан"),
    "mkadmin": ("admin", "Назначен админом"),
    "deny": ("new", "Заявка отклонена"),
    "ban": ("banned", "Доступ закрыт"),
}


@router.callback_query(F.data.regexp(r"^a:(grant|mkadmin|deny|ban):\d+$"))
async def cb_action(call: CallbackQuery):
    _, action, uid = call.data.split(":")
    uid = int(uid)
    if uid in config.admins:
        return await call.answer("Главного админа изменить нельзя", show_alert=True)
    u = await db.get_user(uid)
    if not u:
        return await call.answer("Пользователь не найден", show_alert=True)
    role, note = ACTIONS[action]
    if u["role"] == role:
        await call.answer("Уже применено")
        return await user_card(call, uid)
    await db.set_role(uid, role)
    await call.answer(note)
    if role == "user" and u["role"] in ("new", "pending", "banned"):
        await _notify(call, uid, f"{e('ok')} <b>Доступ открыт!</b>\n\nНажмите кнопку ниже, чтобы начать.",
                      kb(btn("Открыть меню", "m:home", emoji="home", style=GREEN)))
    elif role == "admin":
        await _notify(call, uid, f"{e('admin')} Вам выданы права администратора.",
                      kb(btn("Открыть меню", "m:home", emoji="home", style=GREEN)))
    elif role == "new":
        await _notify(call, uid, f"{e('cancel')} Ваша заявка отклонена.")
    elif role == "banned":
        await _notify(call, uid, f"{e('ban')} Доступ к боту закрыт администратором.")
    await user_card(call, uid)


@router.callback_query(F.data == "a:grantid")
async def cb_grant_id(call: CallbackQuery, state: FSMContext):
    await state.set_state(Admin.grant_id)
    await render(
        call,
        f"{e('id')} <b>Выдать доступ по ID</b>\n\n"
        f"Отправьте Telegram ID пользователя (можно несколько через пробел или запятую).\n\n"
        f"{e('tip')} <i>Свой ID человек может узнать у @userinfobot или в этом боте после /start.</i>",
        kb(back("a:menu")),
    )


@router.message(Admin.grant_id, F.text)
async def in_grant_id(message: Message, state: FSMContext):
    ids = [x for x in message.text.replace(",", " ").split() if x.isdigit()]
    if not ids:
        return await message.answer(f"{e('error')} Не нашёл ни одного ID. Отправьте числа.")
    for raw in ids:
        uid = int(raw)
        if uid in config.admins:
            continue
        await db.ensure_user(uid)
        u = await db.get_user(uid)
        if u["role"] != "admin":
            await db.set_role(uid, "user")
            await _notify(message, uid, f"{e('ok')} <b>Доступ открыт!</b>\n\nНажмите /start, чтобы начать.")
    await state.clear()
    await message.answer(f"{e('ok')} Доступ выдан: {', '.join(f'<code>{i}</code>' for i in ids)}",
                         reply_markup=kb(btn("Админ-панель", "a:menu", emoji="admin", style=BLUE)))


# ---------------- узнать ID премиум-эмодзи ----------------

@router.message(StateFilter(None), F.entities[...].type == "custom_emoji")
async def emoji_ids(message: Message):
    lines = []
    for ent in message.entities:
        if ent.type == "custom_emoji":
            char = ent.extract_from(message.text)
            lines.append(f"{char} — <code>{ent.custom_emoji_id}</code>")
    await message.answer(f"{e('id')} <b>ID премиум-эмодзи:</b>\n\n" + "\n".join(dict.fromkeys(lines)))


@router.message(Command("emoji"))
async def cmd_emoji(message: Message):
    await message.answer(f"{e('tip')} Отправьте мне сообщение с премиум-эмодзи — я пришлю их ID.\n"
                         f"Затем впишите ID в файл <code>bot/emoji.py</code>.")


# ---------------- обновление бота ----------------

@router.callback_query(F.data == "a:upd")
async def cb_update(call: CallbackQuery):
    title = f"{e('refresh')} <b>Обновление бота</b>\n\n"
    if not updater.supported():
        return await render(
            call,
            title + f"{e('warn')} Обновление по кнопке ещё не включено на этом сервере.\n\n"
            f"Один раз выполните на сервере бота:\n<pre>vpncreator update</pre>\n"
            f"После этого кнопка будет обновлять бота сама.",
            kb(back("a:menu")))
    if updater.in_progress():
        return await render(call, title + f"{e('loading')} Обновление уже идёт — бот пришлёт результат.",
                            kb(back("a:menu")))
    ver = updater.local_version()
    current = f"{e('info')} Текущая версия: <code>{ver['sha'][:7]}</code> от {ver.get('date', '')[:10]}\n\n"
    await render(call, title + current + f"{e('loading')} Проверяю GitHub…")
    try:
        info = await updater.check()
    except Exception as exc:  # noqa: BLE001
        return await render(call, title + current + f"{e('error')} Не удалось проверить: {escape(str(exc))}",
                            kb(btn("Проверить снова", "a:upd", emoji="refresh", style=BLUE),
                               btn("Обновить всё равно", "a:upd:go", emoji="ok", style=GREEN), back("a:menu")))
    if not info["behind"]:
        return await render(call, title + current + f"{e('ok')} У вас последняя версия.",
                            kb(btn("Проверить снова", "a:upd", emoji="refresh", style=BLUE), back("a:menu")))
    changes = (f"{e('rocket')} <b>Доступно изменений: {info['behind']}</b>\n{updater.commits_text(info['commits'])}"
               if info["behind"] > 0 else
               f"{e('rocket')} <b>Доступна новая версия</b> <code>{info['latest'][:7]}</code>")
    await render(
        call,
        title + current + f"{changes}\n\n"
        f"{e('tip')} Бот перезапустится примерно на минуту. Пользователи, серверы, ключи и скрипты сохранятся.",
        kb(btn("Обновить сейчас", "a:upd:go", emoji="ok", style=GREEN), back("a:menu")))


@router.callback_query(F.data == "a:upd:go")
async def cb_update_go(call: CallbackQuery):
    from .keys import _busy
    if not updater.supported():
        return await cb_update(call)
    if updater.in_progress():
        return await call.answer("Обновление уже идёт", show_alert=True)
    if _busy:
        return await call.answer(f"Сейчас создаются ключи ({len(_busy)}). Подождите пару минут и повторите.",
                                 show_alert=True)
    try:
        commits = (await updater.check())["commits"]
    except Exception:  # noqa: BLE001
        commits = []
    updater.request(call.from_user.id, commits)
    await render(call, f"{e('loading')} <b>Обновление запущено</b>\n\n"
                       f"Скачиваю новую версию и перезапускаю бота — обычно 1–3 минуты.\n"
                       f"Когда всё будет готово, пришлю сообщение.")
