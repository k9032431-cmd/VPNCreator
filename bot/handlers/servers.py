import ipaddress
import re
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import ssh
from ..common import AddServer, HasAccess, db
from ..crypto import decrypt, encrypt
from ..emoji import e
from ..ui import BLUE, GREEN, RED, back, btn, cancel, kb, render

router = Router(name="servers")
router.message.filter(HasAccess())
router.callback_query.filter(HasAccess())

HOST_RE = re.compile(r"^(?=.{1,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$", re.I)


def creds(server: dict) -> ssh.Creds:
    secret = decrypt(server["secret"])
    if server["auth_type"] == "key":
        key, _, passphrase = secret.partition("\n\x00PASS\x00\n")
        return ssh.Creds(server["host"], server["port"], server["username"],
                         password=passphrase or None, private_key=key)
    return ssh.Creds(server["host"], server["port"], server["username"], password=secret)


def valid_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(HOST_RE.match(host))


# ---------------- список и карточка ----------------

@router.callback_query(F.data == "s:list")
async def cb_list(call: CallbackQuery, state: FSMContext, db_user: dict):
    await state.clear()
    servers = await db.list_servers(db_user["id"])
    if not servers:
        text = (f"{e('servers')} <b>Мои серверы</b>\n\n"
                f"У вас ещё нет серверов.\nНажмите «Подключить VPS», чтобы добавить первый.")
    else:
        text = f"{e('servers')} <b>Мои серверы</b> — {len(servers)}\n\nВыберите сервер:"
    rows = [btn(f"{s['name']} · {s['host']}", f"s:view:{s['id']}", emoji="servers", style=BLUE)
            for s in servers]
    await render(call, text, kb(*rows, btn("Подключить VPS", "s:add", emoji="server_add", style=GREEN), back()))


async def server_card(server: dict) -> str:
    version = await db.config_version()
    installed = server["installed_version"] == version
    keys = await db._scalar("SELECT COUNT(*) FROM keys WHERE server_id=?", (server["id"],))
    auth = f"{e('sshkey')} SSH-ключ" if server["auth_type"] == "key" else f"{e('lock')} Пароль"
    status = f"{e('ok')} Установлено" if installed else f"{e('loading')} Установка при создании ключа"
    return (
        f"{e('servers')} <b>{escape(server['name'])}</b>\n\n"
        f"{e('globe')} Хост: <code>{escape(server['host'])}</code>\n"
        f"{e('plug')} Порт: <code>{server['port']}</code>\n"
        f"{e('user')} Логин: <code>{escape(server['username'])}</code>\n"
        f"{auth}\n"
        f"{e('keys')} Ключей: <b>{keys}</b>\n"
        f"{e('rocket')} Скрипт: {status}"
    )


def server_kb(sid: int):
    return kb(
        btn("Создать ключ на этом сервере", f"k:srv:{sid}", emoji="key_new", style=GREEN),
        [btn("Проверить", f"s:check:{sid}", emoji="search", style=BLUE),
         btn("Переустановить", f"s:reinst:{sid}", emoji="refresh", style=BLUE)],
        btn("Удалить сервер", f"s:del:{sid}", emoji="trash", style=RED),
        back("s:list"),
    )


@router.callback_query(F.data.startswith("s:view:"))
async def cb_view(call: CallbackQuery, db_user: dict):
    server = await db.get_server(int(call.data.split(":")[2]), db_user["id"])
    if not server:
        return await call.answer("Сервер не найден", show_alert=True)
    await render(call, await server_card(server), server_kb(server["id"]))


@router.callback_query(F.data.startswith("s:check:"))
async def cb_check(call: CallbackQuery, db_user: dict):
    server = await db.get_server(int(call.data.split(":")[2]), db_user["id"])
    if not server:
        return await call.answer("Сервер не найден", show_alert=True)
    await call.answer("Проверяю подключение…")
    try:
        async with await ssh.connect(creds(server)) as conn:
            res = await ssh.run(conn, "uname -sr; (. /etc/os-release && echo $PRETTY_NAME) 2>/dev/null; uptime -p", 30)
        result = f"{e('ok')} <b>Подключение успешно</b>\n<pre>{escape(res.tail(800))}</pre>"
    except ssh.SSHError as exc:
        result = f"{e('error')} <b>Ошибка:</b> {escape(str(exc))}"
    await render(call, await server_card(server) + "\n\n" + result, server_kb(server["id"]))


@router.callback_query(F.data.startswith("s:reinst:"))
async def cb_reinstall(call: CallbackQuery, db_user: dict):
    server = await db.get_server(int(call.data.split(":")[2]), db_user["id"])
    if not server:
        return await call.answer("Сервер не найден", show_alert=True)
    await db.set_server_version(server["id"], 0)
    await call.answer("Скрипт будет установлен заново при создании следующего ключа", show_alert=True)
    server["installed_version"] = 0
    await render(call, await server_card(server), server_kb(server["id"]))


@router.callback_query(F.data.startswith("s:del:"))
async def cb_delete(call: CallbackQuery, db_user: dict):
    server = await db.get_server(int(call.data.split(":")[2]), db_user["id"])
    if not server:
        return await call.answer("Сервер не найден", show_alert=True)
    await render(
        call,
        f"{e('warn')} Удалить сервер <b>{escape(server['name'])}</b>?\n\n"
        f"Ключи этого сервера тоже исчезнут из бота (на самом сервере ничего не удаляется).",
        kb([btn("Да, удалить", f"s:delok:{server['id']}", emoji="trash", style=RED),
            btn("Нет", f"s:view:{server['id']}", emoji="back", style=BLUE)]),
    )


@router.callback_query(F.data.startswith("s:delok:"))
async def cb_delete_ok(call: CallbackQuery, state: FSMContext, db_user: dict):
    server = await db.get_server(int(call.data.split(":")[2]), db_user["id"])
    if server:
        await db.delete_server(server["id"])
    await call.answer("Сервер удалён")
    await cb_list(call, state, db_user)


# ---------------- добавление сервера ----------------

async def step(message: Message, state: FSMContext, text: str, markup=None) -> None:
    """Удаляет ввод пользователя и обновляет сообщение-подсказку бота."""
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    data = await state.get_data()
    prompt_id = data.get("prompt_id")
    if prompt_id:
        try:
            await message.bot.edit_message_text(text, chat_id=message.chat.id, message_id=prompt_id,
                                                reply_markup=markup)
            return
        except TelegramBadRequest:
            pass
    sent = await message.answer(text, reply_markup=markup)
    await state.update_data(prompt_id=sent.message_id)


def progress(n: int, title: str, hint: str, error: str = "") -> str:
    err = f"\n\n{e('error')} {error}" if error else ""
    return f"{e('server_add')} <b>Подключение VPS</b> · шаг {n}/5\n\n{title}\n\n{e('tip')} <i>{hint}</i>{err}"


ASK_HOST = ("Отправьте <b>IP-адрес</b> или домен сервера.", "Например: 185.22.33.44")


@router.callback_query(F.data == "s:add")
async def cb_add(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AddServer.host)
    msg = await render(call, progress(1, f"{e('globe')} " + ASK_HOST[0], ASK_HOST[1]), cancel())
    await state.update_data(prompt_id=msg.message_id)


@router.message(AddServer.host, F.text)
async def in_host(message: Message, state: FSMContext):
    host = message.text.strip().lower()
    if not valid_host(host):
        return await step(message, state, progress(1, f"{e('globe')} " + ASK_HOST[0], ASK_HOST[1],
                                                   "Некорректный адрес, попробуйте ещё раз."), cancel())
    await state.update_data(host=host)
    await state.set_state(AddServer.port)
    await step(message, state,
               progress(2, f"{e('plug')} Отправьте <b>SSH-порт</b>.", "Обычно это 22"),
               kb(btn("22 (стандартный)", "s:port22", emoji="plug", style=GREEN), cancel().inline_keyboard[0]))


async def _ask_user(target, state: FSMContext):
    await state.set_state(AddServer.username)
    text = progress(3, f"{e('user')} Отправьте <b>логин</b> SSH.", "Обычно это root")
    markup = kb(btn("root", "s:root", emoji="user", style=GREEN), cancel().inline_keyboard[0])
    if isinstance(target, CallbackQuery):
        await render(target, text, markup)
    else:
        await step(target, state, text, markup)


@router.callback_query(AddServer.port, F.data == "s:port22")
async def cb_port22(call: CallbackQuery, state: FSMContext):
    await state.update_data(port=22)
    await _ask_user(call, state)


@router.message(AddServer.port, F.text)
async def in_port(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or not 1 <= int(text) <= 65535:
        return await step(message, state, progress(2, f"{e('plug')} Отправьте <b>SSH-порт</b>.", "Обычно это 22",
                                                   "Порт — число от 1 до 65535."),
                          kb(btn("22 (стандартный)", "s:port22", emoji="plug", style=GREEN),
                             cancel().inline_keyboard[0]))
    await state.update_data(port=int(text))
    await _ask_user(message, state)


async def _ask_auth(target, state: FSMContext):
    await state.set_state(AddServer.auth)
    text = progress(4, f"{e('lock')} Как подключаться к серверу?", "Пароль — проще, SSH-ключ — надёжнее")
    markup = kb([btn("Пароль", "s:auth:password", emoji="lock", style=BLUE),
                 btn("SSH-ключ", "s:auth:key", emoji="sshkey", style=BLUE)], cancel().inline_keyboard[0])
    if isinstance(target, CallbackQuery):
        await render(target, text, markup)
    else:
        await step(target, state, text, markup)


@router.callback_query(AddServer.username, F.data == "s:root")
async def cb_root(call: CallbackQuery, state: FSMContext):
    await state.update_data(username="root")
    await _ask_auth(call, state)


@router.message(AddServer.username, F.text)
async def in_username(message: Message, state: FSMContext):
    username = message.text.strip()
    if not USER_RE.match(username):
        return await step(message, state, progress(3, f"{e('user')} Отправьте <b>логин</b> SSH.", "Обычно это root",
                                                   "Некорректный логин."),
                          kb(btn("root", "s:root", emoji="user", style=GREEN), cancel().inline_keyboard[0]))
    await state.update_data(username=username)
    await _ask_auth(message, state)


@router.callback_query(AddServer.auth, F.data.startswith("s:auth:"))
async def cb_auth(call: CallbackQuery, state: FSMContext):
    auth = call.data.split(":")[2]
    await state.update_data(auth_type=auth)
    await state.set_state(AddServer.secret)
    if auth == "password":
        text = progress(5, f"{e('lock')} Отправьте <b>пароль</b> от сервера.",
                        "Сообщение с паролем сразу удалится, пароль хранится в зашифрованном виде")
    else:
        text = progress(5, f"{e('sshkey')} Отправьте <b>приватный SSH-ключ</b> текстом или файлом.",
                        "Начинается с -----BEGIN … PRIVATE KEY-----. Сообщение сразу удалится")
    await render(call, text, cancel())


@router.message(AddServer.secret, F.text | F.document)
async def in_secret(message: Message, state: FSMContext):
    data = await state.get_data()
    if message.document:
        if data["auth_type"] != "key" or message.document.file_size > 64 * 1024:
            return await step(message, state, progress(5, "Отправьте пароль текстом.", "", "Нужен текст."), cancel())
        file = await message.bot.download(message.document)
        secret = file.read().decode(errors="ignore").strip()
    else:
        secret = message.text if data["auth_type"] == "password" else message.text.strip()
    if data["auth_type"] == "key" and "PRIVATE KEY" not in secret:
        return await step(message, state, progress(5, f"{e('sshkey')} Отправьте <b>приватный SSH-ключ</b>.",
                                                   "Начинается с -----BEGIN … PRIVATE KEY-----",
                                                   "Это не похоже на приватный ключ."), cancel())
    await state.update_data(secret=encrypt(secret))
    if data["auth_type"] == "key" and "ENCRYPTED" in secret:
        await state.set_state(AddServer.passphrase)
        return await step(message, state, progress(5, f"{e('lock')} Ключ защищён паролем — отправьте его.",
                                                   "Сообщение сразу удалится"), cancel())
    await _ask_name(message, state)


@router.message(AddServer.passphrase, F.text)
async def in_passphrase(message: Message, state: FSMContext):
    data = await state.get_data()
    key = decrypt(data["secret"])
    await state.update_data(secret=encrypt(key + "\n\x00PASS\x00\n" + message.text))
    await _ask_name(message, state)


async def _ask_name(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.set_state(AddServer.name)
    await step(message, state,
               f"{e('tag')} <b>Название сервера</b>\n\nКак назвать сервер? Например: <code>Germany-1</code>",
               kb(btn(f"Оставить {data['host']}", "s:name:ip", emoji="auto", style=GREEN),
                  cancel().inline_keyboard[0]))


@router.callback_query(AddServer.name, F.data == "s:name:ip")
async def cb_name_ip(call: CallbackQuery, state: FSMContext, db_user: dict):
    data = await state.get_data()
    await _save(call, state, db_user, data["host"])


@router.message(AddServer.name, F.text)
async def in_name(message: Message, state: FSMContext, db_user: dict):
    name = message.text.strip()[:40]
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    await _save(message, state, db_user, name)


async def _save(target: Message | CallbackQuery, state: FSMContext, db_user: dict, name: str):
    data = await state.get_data()
    chat_id = target.chat.id if isinstance(target, Message) else target.message.chat.id
    bot = target.bot
    text = f"{e('loading')} Подключаюсь к <code>{escape(data['host'])}:{data['port']}</code>…"
    if isinstance(target, CallbackQuery):
        msg = await render(target, text)
    else:
        try:
            msg = await bot.edit_message_text(text, chat_id=chat_id, message_id=data["prompt_id"])
        except (TelegramBadRequest, KeyError):
            msg = await bot.send_message(chat_id, text)

    server = {"host": data["host"], "port": data["port"], "username": data["username"],
              "auth_type": data["auth_type"], "secret": data["secret"]}
    try:
        async with await ssh.connect(creds(server)) as conn:
            await ssh.run(conn, "true", 30)
    except ssh.SSHError as exc:
        await state.set_state(AddServer.host)
        await state.update_data(prompt_id=msg.message_id)
        return await msg.edit_text(
            f"{e('error')} <b>Не удалось подключиться</b>\n\n{escape(str(exc))}\n\n"
            f"{e('globe')} Отправьте IP-адрес заново, чтобы повторить ввод данных.",
            reply_markup=cancel())

    await state.clear()
    sid = await db.add_server(db_user["id"], name, data["host"], data["port"], data["username"],
                              data["auth_type"], data["secret"])
    await msg.edit_text(
        f"{e('ok')} <b>Сервер подключён!</b>\n\n{e('servers')} {escape(name)} · <code>{escape(data['host'])}</code>\n\n"
        f"Теперь можно создать ключ на этом сервере.",
        reply_markup=kb(btn("Создать ключ", f"k:srv:{sid}", emoji="key_new", style=GREEN),
                        btn("Мои серверы", "s:list", emoji="servers", style=BLUE),
                        back(text="Главное меню")),
    )
