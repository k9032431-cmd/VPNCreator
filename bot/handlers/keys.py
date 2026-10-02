import re
import secrets
import time
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from .. import ssh
from ..common import CreateKey, HasAccess, db
from ..config import config
from ..emoji import e
from ..ui import BLUE, GREEN, RED, back, btn, cancel, home, kb, render
from .servers import creds

router = Router(name="keys")
router.message.filter(HasAccess())
router.callback_query.filter(HasAccess())

PAGE = 8
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_busy: set[int] = set()


def placeholders(server: dict, user_id: int, name: str, files: list[dict]) -> dict[str, str]:
    script = f"{ssh.REMOTE_DIR_SH}/{files[0]['name']}" if files else ""
    return {
        "name": name,
        "user_id": str(user_id),
        "server_ip": server["host"],
        "dir": ssh.REMOTE_DIR_SH,
        "script": script,
    }


def install_steps(raw: str) -> list[str]:
    return [line.strip() for line in raw.splitlines() if line.strip() and not line.strip().startswith("#")]


def extract_key(output: str, regex: str) -> str:
    if regex:
        found = re.findall(regex, output, flags=re.M)
        found = [f if isinstance(f, str) else f[0] for f in found]
        return "\n".join(dict.fromkeys(x.strip() for x in found if x.strip()))
    return output.strip()


# ---------------- создание ключа ----------------

@router.callback_query(F.data == "k:new")
async def cb_new(call: CallbackQuery, state: FSMContext, db_user: dict):
    await state.clear()
    servers = await db.list_servers(db_user["id"])
    if not servers:
        return await render(
            call,
            f"{e('key_new')} <b>Создание ключа</b>\n\nСначала подключите хотя бы один сервер.",
            kb(btn("Подключить VPS", "s:add", emoji="server_add", style=GREEN), back()),
        )
    rows = [btn(f"{s['name']} · {s['host']}", f"k:srv:{s['id']}", emoji="servers", style=BLUE) for s in servers]
    await render(call, f"{e('key_new')} <b>Создание ключа</b>\n\n{e('point_down')} На каком сервере создать ключ?",
                 kb(*rows, back()))


@router.callback_query(F.data.startswith("k:srv:"))
async def cb_server(call: CallbackQuery, state: FSMContext, db_user: dict):
    server = await db.get_server(int(call.data.split(":")[2]), db_user["id"])
    if not server:
        return await call.answer("Сервер не найден", show_alert=True)
    if not (await db.get("key_cmd")).strip():
        return await render(
            call,
            f"{e('warn')} <b>Скрипт ещё не настроен</b>\n\n"
            f"Администратор должен указать команду создания ключа в «Настройках».",
            kb(back()),
        )
    await state.set_state(CreateKey.name)
    await state.update_data(server_id=server["id"])
    msg = await render(
        call,
        f"{e('key_new')} <b>Создание ключа</b> · {e('servers')} {escape(server['name'])}\n\n"
        f"{e('tag')} Отправьте <b>название ключа</b> (латиница, цифры, <code>-</code> и <code>_</code>, до 32 символов)"
        f" или нажмите «Автоматически».",
        kb(btn("Автоматически", "k:auto", emoji="auto", style=GREEN), cancel().inline_keyboard[0]),
    )
    await state.update_data(prompt_id=msg.message_id)


@router.callback_query(CreateKey.name, F.data == "k:auto")
async def cb_auto(call: CallbackQuery, state: FSMContext, db_user: dict):
    name = f"u{db_user['id']}_{secrets.token_hex(3)}"
    await start_creation(call.message, state, db_user, name, edit=True)
    await call.answer()


@router.message(CreateKey.name, F.text)
async def in_name(message: Message, state: FSMContext, db_user: dict):
    name = message.text.strip()
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    data = await state.get_data()
    if not NAME_RE.match(name):
        return await _prompt_error(message, data, "Только латиница, цифры, - и _ (до 32 символов).")
    if await db.key_name_exists(data["server_id"], name):
        return await _prompt_error(message, data, "Ключ с таким названием уже есть на этом сервере.")
    await start_creation(message, state, db_user, name, edit=False)


async def _prompt_error(message: Message, data: dict, error: str):
    text = (f"{e('key_new')} <b>Создание ключа</b>\n\n{e('tag')} Отправьте <b>название ключа</b> "
            f"или нажмите «Автоматически».\n\n{e('error')} {error}")
    markup = kb(btn("Автоматически", "k:auto", emoji="auto", style=GREEN), cancel().inline_keyboard[0])
    try:
        await message.bot.edit_message_text(text, chat_id=message.chat.id, message_id=data["prompt_id"],
                                            reply_markup=markup)
    except (TelegramBadRequest, KeyError):
        await message.answer(text, reply_markup=markup)


async def start_creation(message: Message, state: FSMContext, db_user: dict, name: str, edit: bool):
    data = await state.get_data()
    await state.clear()
    uid = db_user["id"]
    server = await db.get_server(data["server_id"], uid)
    bot, chat_id = message.bot, message.chat.id

    status_msg = None
    if data.get("prompt_id"):
        try:
            status_msg = await bot.edit_message_text(f"{e('loading')} Подготовка…", chat_id=chat_id,
                                                     message_id=data["prompt_id"])
        except TelegramBadRequest:
            pass
    if status_msg is None or status_msg is True:
        status_msg = await bot.send_message(chat_id, f"{e('loading')} Подготовка…")

    if not server:
        return await status_msg.edit_text(f"{e('error')} Сервер не найден.", reply_markup=kb(home()))
    if uid in _busy:
        return await status_msg.edit_text(f"{e('warn')} Дождитесь завершения предыдущей операции.",
                                          reply_markup=kb(home()))
    _busy.add(uid)
    try:
        await _create(status_msg, server, uid, name)
    finally:
        _busy.discard(uid)


async def _create(status_msg: Message, server: dict, uid: int, name: str):
    header = f"{e('key_new')} <b>Создание ключа</b> <code>{escape(name)}</code>\n{e('servers')} {escape(server['name'])}\n\n"
    log: list[str] = []
    last_edit = 0.0

    async def show(line: str | None = None, force: bool = False, replace_last: bool = False):
        nonlocal last_edit
        if line is not None:
            if replace_last and log:
                log[-1] = line
            else:
                log.append(line)
        if not force and time.monotonic() - last_edit < 1.0:
            return
        last_edit = time.monotonic()
        try:
            await status_msg.edit_text(header + "\n".join(log[-12:]))
        except TelegramBadRequest:
            pass

    async def fail(error: str, output: str = ""):
        out = f"\n\n<b>Вывод:</b>\n<pre>{escape(output[-1500:])}</pre>" if output.strip() else ""
        try:
            await status_msg.edit_text(
                header + "\n".join(log[-12:]) + f"\n\n{e('error')} <b>{escape(error)}</b>{out}",
                reply_markup=kb(btn("Попробовать снова", f"k:srv:{server['id']}", emoji="refresh", style=GREEN),
                                home()))
        except TelegramBadRequest:
            await status_msg.answer(f"{e('error')} <b>{escape(error)}</b>", reply_markup=kb(home()))

    files = await db.all_files()
    values = placeholders(server, uid, name, files)

    await show(f"{e('loading')} Подключение к серверу…", force=True)
    try:
        conn = await ssh.connect(creds(server))
    except ssh.SSHError as exc:
        return await fail(str(exc))

    async with conn:
        await show(f"{e('ok')} Подключено к <code>{escape(server['host'])}</code>", force=True, replace_last=True)

        version = await db.config_version()
        if server["installed_version"] != version:
            if files:
                await show(f"{e('upload')} Загрузка скриптов ({len(files)})…", force=True)
                try:
                    await ssh.upload(conn, files)
                except ssh.SSHError as exc:
                    return await fail(str(exc))
                await show(f"{e('ok')} Скрипты загружены", force=True, replace_last=True)

            steps = install_steps(await db.get("install_cmds"))
            for i, cmd in enumerate(steps, 1):
                cmd = ssh.fill(cmd, values)
                short = escape(cmd if len(cmd) <= 60 else cmd[:57] + "…")
                await show(f"{e('rocket')} Шаг {i}/{len(steps)}: <code>{short}</code>", force=True)
                try:
                    res = await ssh.run(conn, cmd, config.install_step_timeout)
                except ssh.SSHError as exc:
                    return await fail(f"Шаг {i}: {exc}")
                if not res.ok:
                    return await fail(f"Шаг {i} завершился с кодом {res.code}", res.tail())
                await show(f"{e('ok')} Шаг {i}/{len(steps)}: <code>{short}</code>", force=True, replace_last=True)
            await db.set_server_version(server["id"], version)

        await show(f"{e('loading')} Создание ключа…", force=True)
        cmd = ssh.fill(await db.get("key_cmd"), values)
        try:
            res = await ssh.run(conn, cmd, config.key_cmd_timeout)
        except ssh.SSHError as exc:
            return await fail(str(exc))
        if not res.ok:
            return await fail(f"Команда создания ключа завершилась с кодом {res.code}", res.tail())

    key = extract_key(res.stdout, await db.get("key_regex"))
    if not key:
        return await fail("Скрипт не вернул ключ", res.tail())

    kid = await db.add_key(uid, server["id"], name, key)
    await show(f"{e('ok')} Ключ создан", force=True, replace_last=True)
    await send_key(status_msg, await db.get_key(kid, uid), created=True)


# ---------------- просмотр ключей ----------------

def key_markup(key: dict, created: bool = False):
    value = key["value"]
    return kb(
        btn("Копировать ключ", emoji="copy", style=GREEN, copy=value) if len(value) <= 256 else
        btn("Скачать файлом", f"k:file:{key['id']}", emoji="file", style=GREEN),
        [btn("Создать ещё", f"k:srv:{key['server_id']}", emoji="key_new", style=BLUE),
         btn("Удалить", f"k:del:{key['id']}", emoji="trash", style=RED)],
        [btn("Мои ключи", "k:list:0", emoji="keys", style=BLUE), home()] if created else back("k:list:0"),
    )


def key_text(key: dict, created: bool = False) -> str:
    title = f"{e('ok')} <b>Ключ готов!</b>" if created else f"{e('keys')} <b>Ключ</b>"
    value = key["value"]
    body = (f"<pre>{escape(value)}</pre>" if len(value) <= 3000
            else f"{e('file')} Ключ длинный — нажмите «Скачать файлом».")
    date = time.strftime("%d.%m.%Y %H:%M", time.localtime(key["created_at"]))
    return (f"{title}\n\n{e('tag')} <code>{escape(key['name'])}</code>\n"
            f"{e('servers')} {escape(key.get('server_name') or '—')}\n{e('calendar')} {date}\n\n{body}")


async def send_key(msg: Message, key: dict, created: bool = False):
    try:
        await msg.edit_text(key_text(key, created), reply_markup=key_markup(key, created))
    except TelegramBadRequest:
        await msg.answer(key_text(key, created), reply_markup=key_markup(key, created))


@router.callback_query(F.data.startswith("k:list:"))
async def cb_list(call: CallbackQuery, state: FSMContext, db_user: dict):
    await state.clear()
    await show_list(call, db_user, int(call.data.split(":")[2]))


async def show_list(call: CallbackQuery, db_user: dict, page: int):
    total = await db.count_keys(db_user["id"])
    keys = await db.list_keys(db_user["id"], page * PAGE, PAGE)
    if not total:
        return await render(call, f"{e('keys')} <b>Мои ключи</b>\n\nКлючей пока нет.",
                            kb(btn("Создать ключ", "k:new", emoji="key_new", style=GREEN), back()))
    pages = (total + PAGE - 1) // PAGE
    rows = [btn(f"{k['name']} · {k['server_name'] or '—'}", f"k:view:{k['id']}", emoji="keys", style=BLUE)
            for k in keys]
    nav = [
        btn("Назад", f"k:list:{page - 1}", emoji="back") if page > 0 else None,
        btn(f"{page + 1}/{pages}", f"k:list:{page}") if pages > 1 else None,
        btn("Далее", f"k:list:{page + 1}", emoji="next") if page + 1 < pages else None,
    ]
    await render(call, f"{e('keys')} <b>Мои ключи</b> — {total}\n\nВыберите ключ:",
                 kb(*rows, nav, btn("Создать ключ", "k:new", emoji="key_new", style=GREEN), back()))


@router.callback_query(F.data.startswith("k:view:"))
async def cb_view(call: CallbackQuery, db_user: dict):
    key = await db.get_key(int(call.data.split(":")[2]), db_user["id"])
    if not key:
        return await call.answer("Ключ не найден", show_alert=True)
    await render(call, key_text(key), key_markup(key))


@router.callback_query(F.data.startswith("k:file:"))
async def cb_file(call: CallbackQuery, db_user: dict):
    key = await db.get_key(int(call.data.split(":")[2]), db_user["id"])
    if not key:
        return await call.answer("Ключ не найден", show_alert=True)
    await call.answer()
    await call.message.answer_document(BufferedInputFile(key["value"].encode(), f"{key['name']}.txt"),
                                       caption=f"{e('keys')} <code>{escape(key['name'])}</code>")


@router.callback_query(F.data.startswith("k:del:"))
async def cb_delete(call: CallbackQuery, db_user: dict):
    key = await db.get_key(int(call.data.split(":")[2]), db_user["id"])
    if not key:
        return await call.answer("Ключ не найден", show_alert=True)
    has_cmd = bool((await db.get("delete_cmd")).strip())
    note = "Ключ будет удалён и на сервере." if has_cmd else "Ключ удалится только из бота."
    await render(call, f"{e('warn')} Удалить ключ <code>{escape(key['name'])}</code>?\n\n{note}",
                 kb([btn("Да, удалить", f"k:delok:{key['id']}", emoji="trash", style=RED),
                     btn("Нет", f"k:view:{key['id']}", emoji="back", style=BLUE)]))


@router.callback_query(F.data.startswith("k:delok:") | F.data.startswith("k:delforce:"))
async def cb_delete_ok(call: CallbackQuery, state: FSMContext, db_user: dict):
    force = call.data.startswith("k:delforce:")
    key = await db.get_key(int(call.data.split(":")[2]), db_user["id"])
    if not key:
        return await call.answer("Ключ не найден", show_alert=True)
    delete_cmd = (await db.get("delete_cmd")).strip()
    server = await db.get_server(key["server_id"])
    if delete_cmd and server and not force:
        await render(call, f"{e('loading')} Удаляю ключ <code>{escape(key['name'])}</code> на сервере…")
        error, output = "", ""
        try:
            async with await ssh.connect(creds(server)) as conn:
                values = placeholders(server, db_user["id"], key["name"], await db.all_files())
                res = await ssh.run(conn, ssh.fill(delete_cmd, values), config.key_cmd_timeout)
            if not res.ok:
                error, output = f"Команда удаления завершилась с кодом {res.code}", res.tail()
        except ssh.SSHError as exc:
            error = str(exc)
        if error:
            out = f"\n<pre>{escape(output[-1200:])}</pre>" if output else ""
            return await render(
                call, f"{e('error')} <b>{escape(error)}</b>{out}\n\nУдалить ключ только из бота?",
                kb([btn("Удалить из бота", f"k:delforce:{key['id']}", emoji="trash", style=RED),
                    btn("Отмена", f"k:view:{key['id']}", emoji="back", style=BLUE)]))
    await db.delete_key(key["id"])
    await call.answer("Ключ удалён")
    await show_list(call, db_user, 0)
