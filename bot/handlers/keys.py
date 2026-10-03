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


def placeholders(server: dict, user_id: int, name: str, script: dict | None) -> dict[str, str]:
    file = f"{ssh.REMOTE_DIR_SH}/{script['filename']}" if script and script.get("filename") else ""
    return {
        "name": name,
        "user_id": str(user_id),
        "server_ip": server["host"],
        "dir": ssh.REMOTE_DIR_SH,
        "file": file,
        "script": file,
    }


def steps_of(raw: str, values: dict[str, str]) -> list[str]:
    return [ssh.fill(line, values) for line in ssh.parse_steps(raw)]


def session_output(output: str) -> str:
    """Вывод сессии без служебных строк приглашения bash."""
    return "\n".join(line for line in output.splitlines() if not line.startswith(ssh.READY)).strip()


def extract_key(output: str, regex: str) -> str:
    if regex:
        found = re.findall(regex, output, flags=re.M)
        found = [f if isinstance(f, str) else f[0] for f in found]
        return "\n".join(dict.fromkeys(x.strip() for x in found if x.strip()))
    return output.strip()


# ---------------- выбор сервера и скрипта ----------------

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


@router.callback_query(F.data.regexp(r"^k:srv:\d+$"))
async def cb_server(call: CallbackQuery, state: FSMContext, db_user: dict):
    server = await db.get_server(int(call.data.split(":")[2]), db_user["id"])
    if not server:
        return await call.answer("Сервер не найден", show_alert=True)
    scripts = await db.list_scripts(only_enabled=True)
    if not scripts:
        return await render(
            call,
            f"{e('warn')} <b>Скрипты ещё не добавлены</b>\n\nАдминистратор должен добавить VPN-скрипт в админ-панели.",
            kb(back()),
        )
    if len(scripts) == 1:
        return await ask_name(call, state, server, scripts[0])
    rows = [btn(s["name"], f"k:sc:{server['id']}:{s['id']}", emoji="rocket", style=BLUE) for s in scripts]
    await render(call, f"{e('key_new')} <b>Создание ключа</b> · {e('servers')} {escape(server['name'])}\n\n"
                       f"{e('point_down')} Какой VPN нужен?", kb(*rows, back("k:new")))


@router.callback_query(F.data.regexp(r"^k:sc:\d+:\d+$"))
async def cb_script(call: CallbackQuery, state: FSMContext, db_user: dict):
    _, _, sid, scid = call.data.split(":")
    server = await db.get_server(int(sid), db_user["id"])
    script = await db.get_script(int(scid))
    if not server or not script or not script["enabled"]:
        return await call.answer("Сервер или скрипт не найден", show_alert=True)
    await ask_name(call, state, server, script)


def name_prompt(server: dict, script_name: str, error: str = "") -> str:
    err = f"\n\n{e('error')} {error}" if error else ""
    return (f"{e('key_new')} <b>Создание ключа</b> · {escape(script_name)} · {e('servers')} {escape(server['name'])}\n\n"
            f"{e('tag')} Отправьте <b>название ключа</b> (латиница, цифры, <code>-</code> и <code>_</code>, "
            f"до 32 символов) или нажмите «Автоматически».{err}")


NAME_KB = kb(btn("Автоматически", "k:auto", emoji="auto", style=GREEN), cancel().inline_keyboard[0])


async def ask_name(call: CallbackQuery, state: FSMContext, server: dict, script: dict):
    await state.set_state(CreateKey.name)
    await state.update_data(server_id=server["id"], script_id=script["id"])
    msg = await render(call, name_prompt(server, script["name"]), NAME_KB)
    await state.update_data(prompt_id=msg.message_id)


@router.callback_query(CreateKey.name, F.data == "k:auto")
async def cb_auto(call: CallbackQuery, state: FSMContext, db_user: dict):
    await call.answer()
    await start_creation(call.message, state, db_user, f"u{db_user['id']}_{secrets.token_hex(3)}")


@router.message(CreateKey.name, F.text)
async def in_name(message: Message, state: FSMContext, db_user: dict):
    name = message.text.strip()
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    data = await state.get_data()
    error = ""
    if not NAME_RE.match(name):
        error = "Только латиница, цифры, - и _ (до 32 символов)."
    elif await db.key_name_exists(data["server_id"], name):
        error = "Ключ с таким названием уже есть на этом сервере."
    if error:
        server = await db.get_server(data["server_id"], db_user["id"])
        script = await db.get_script(data["script_id"])
        text = name_prompt(server, script["name"] if script else "", error)
        try:
            await message.bot.edit_message_text(text, chat_id=message.chat.id, message_id=data["prompt_id"],
                                                reply_markup=NAME_KB)
        except (TelegramBadRequest, KeyError):
            await message.answer(text, reply_markup=NAME_KB)
        return
    await start_creation(message, state, db_user, name)


# ---------------- создание ключа ----------------

async def start_creation(message: Message, state: FSMContext, db_user: dict, name: str):
    data = await state.get_data()
    await state.clear()
    uid = db_user["id"]
    bot, chat_id = message.bot, message.chat.id

    status_msg = None
    if data.get("prompt_id"):
        try:
            status_msg = await bot.edit_message_text(f"{e('loading')} Подготовка…", chat_id=chat_id,
                                                     message_id=data["prompt_id"])
        except TelegramBadRequest:
            pass
    if not isinstance(status_msg, Message):
        status_msg = await bot.send_message(chat_id, f"{e('loading')} Подготовка…")

    server = await db.get_server(data.get("server_id", 0), uid)
    script = await db.get_script(data.get("script_id", 0))
    if not server or not script:
        return await status_msg.edit_text(f"{e('error')} Сервер или скрипт не найден.", reply_markup=kb(home()))
    if uid in _busy:
        return await status_msg.edit_text(f"{e('warn')} Дождитесь завершения предыдущей операции.",
                                          reply_markup=kb(home()))
    _busy.add(uid)
    try:
        await Creation(status_msg, server, script, uid, name).run()
    finally:
        _busy.discard(uid)


class Creation:
    """Подключение → установка (один раз) → создание ключа → отправка пользователю."""

    def __init__(self, msg: Message, server: dict, script: dict, uid: int, name: str):
        self.msg, self.server, self.script, self.uid, self.name = msg, server, script, uid, name
        self.values = placeholders(server, uid, name, script)
        self.log: list[str] = []
        self.last_edit = 0.0
        self.header = (f"{e('key_new')} <b>Создание ключа</b> <code>{escape(name)}</code>\n"
                       f"{e('rocket')} {escape(script['name'])} · {e('servers')} {escape(server['name'])}\n\n")

    async def show(self, line: str | None = None, *, force: bool = False, replace: bool = False):
        if line is not None:
            if replace and self.log:
                self.log[-1] = line
            else:
                self.log.append(line)
        if not force and time.monotonic() - self.last_edit < 1.5:
            return
        self.last_edit = time.monotonic()
        try:
            await self.msg.edit_text(self.header + "\n".join(self.log[-12:]))
        except TelegramBadRequest:
            pass

    async def fail(self, error: str, output: str = ""):
        out = f"\n\n<b>Вывод:</b>\n<pre>{escape(output.strip()[-1500:])}</pre>" if output.strip() else ""
        text = self.header + "\n".join(self.log[-10:]) + f"\n\n{e('error')} <b>{escape(error)}</b>{out}"
        markup = kb(btn("Попробовать снова", f"k:sc:{self.server['id']}:{self.script['id']}",
                        emoji="refresh", style=GREEN), home())
        try:
            await self.msg.edit_text(text[-4000:], reply_markup=markup)
        except TelegramBadRequest:
            await self.msg.answer(f"{e('error')} <b>{escape(error)}</b>", reply_markup=markup)

    def stepper(self, title: str, steps: list[str]):
        async def on_step(i: int, line: str):
            shown = "Enter" if line.strip().lower() in ssh.ENTER_WORDS else line
            shown = escape(shown if len(shown) <= 50 else shown[:47] + "…")
            await self.show(f"{e('rocket')} {title}: шаг {i}/{len(steps)} · <code>{shown}</code>",
                            replace=i > 1)
        return on_step

    async def run(self):
        await self.show(f"{e('loading')} Подключение к серверу…", force=True)
        try:
            conn = await ssh.connect(creds(self.server))
        except ssh.SSHError as exc:
            return await self.fail(str(exc))
        async with conn:
            await self.show(f"{e('ok')} Подключено к <code>{escape(self.server['host'])}</code>",
                            force=True, replace=True)
            try:
                if not await self.install(conn):
                    return
                value, filename = await self.create(conn)
            except ssh.SSHError as exc:
                return await self.fail(str(exc))
            if value is None:
                return

        kid = await db.add_key(self.uid, self.server["id"], self.name, value, self.script["id"], filename)
        await self.show(f"{e('ok')} Ключ создан", force=True, replace=True)
        key = await db.get_key(kid, self.uid)
        await send_key(self.msg, key, created=True)

    async def check(self, conn) -> bool:
        if not self.script["check_cmd"].strip():
            return False
        res = await ssh.run(conn, ssh.fill(self.script["check_cmd"], self.values), 60)
        return res.ok

    async def install(self, conn) -> bool:
        s = self.script
        if await db.installed_version(self.server["id"], s["id"]) == s["version"]:
            return True
        if await self.check(conn):
            await self.show(f"{e('ok')} {escape(s['name'])} уже установлен на сервере", force=True)
            await db.set_installed(self.server["id"], s["id"], s["version"])
            return True
        if s["filename"] and s["content"]:
            await self.show(f"{e('upload')} Загрузка <code>{escape(s['filename'])}</code>…", force=True)
            await ssh.upload(conn, [{"name": s["filename"], "content": s["content"]}])
            await self.show(f"{e('ok')} Скрипт загружен", force=True, replace=True)
        steps = steps_of(s["install_steps"], self.values)
        if steps:
            await self.show(f"{e('rocket')} Установка: начинаю ({len(steps)} шаг.)", force=True)
            res = await ssh.session(conn, steps, config.install_step_timeout, self.stepper("Установка", steps))
            if not res.ok:
                await self.fail(f"Установка: {res.error}", res.tail())
                return False
            await self.show(f"{e('ok')} Установка завершена", force=True, replace=True)
            if s["check_cmd"].strip() and not await self.check(conn):
                await self.fail("Установка прошла, но «Проверка установки» не подтвердила результат",
                                session_output(res.output))
                return False
        await db.set_installed(self.server["id"], s["id"], s["version"])
        return True

    async def create(self, conn) -> tuple[str | None, str | None]:
        s = self.script
        steps = steps_of(s["key_steps"], self.values)
        if not steps:
            await self.fail("У скрипта не заданы шаги создания ключа")
            return None, None
        await self.show(f"{e('loading')} Создание ключа…", force=True)
        res = await ssh.session(conn, steps, config.key_cmd_timeout, self.stepper("Ключ", steps))
        if not res.ok:
            await self.fail(f"Создание ключа: {res.error}", res.tail())
            return None, None
        if s["result_type"] == "file":
            path = ssh.fill(s["result_path"], self.values)
            try:
                data = await ssh.read_file(conn, path)
            except ssh.SSHError as exc:
                await self.fail(str(exc), session_output(res.output))
                return None, None
            return data.decode("utf-8", errors="replace"), ssh.basename(path)
        key = extract_key(session_output(res.output), s["key_regex"])
        if not key:
            await self.fail("Скрипт не вернул ключ", res.tail())
            return None, None
        return key, None


# ---------------- просмотр ключей ----------------

def key_markup(key: dict, created: bool = False):
    value = key["value"]
    if key.get("filename"):
        main = btn("Скачать файл", f"k:file:{key['id']}", emoji="file", style=GREEN)
    elif len(value) <= 256:
        main = btn("Копировать ключ", emoji="copy", style=GREEN, copy=value)
    else:
        main = btn("Скачать файлом", f"k:file:{key['id']}", emoji="file", style=GREEN)
    again = (f"k:sc:{key['server_id']}:{key['script_id']}" if key.get("script_id")
             else f"k:srv:{key['server_id']}")
    return kb(
        main,
        [btn("Создать ещё", again, emoji="key_new", style=BLUE),
         btn("Удалить", f"k:del:{key['id']}", emoji="trash", style=RED)],
        [btn("Мои ключи", "k:list:0", emoji="keys", style=BLUE), home()] if created else back("k:list:0"),
    )


def key_text(key: dict, created: bool = False) -> str:
    title = f"{e('ok')} <b>Ключ готов!</b>" if created else f"{e('keys')} <b>Ключ</b>"
    value = key["value"]
    if key.get("filename"):
        body = f"{e('file')} Файл <code>{escape(key['filename'])}</code> — импортируйте его в VPN-приложение."
    elif len(value) <= 3000:
        body = f"<pre>{escape(value)}</pre>"
    else:
        body = f"{e('file')} Ключ длинный — нажмите «Скачать файлом»."
    date = time.strftime("%d.%m.%Y %H:%M", time.localtime(key["created_at"]))
    vpn = f"{e('rocket')} {escape(key['script_name'])}\n" if key.get("script_name") else ""
    return (f"{title}\n\n{e('tag')} <code>{escape(key['name'])}</code>\n{vpn}"
            f"{e('servers')} {escape(key.get('server_name') or '—')}\n{e('calendar')} {date}\n\n{body}")


def key_document(key: dict) -> BufferedInputFile:
    return BufferedInputFile(key["value"].encode(), key.get("filename") or f"{key['name']}.txt")


def key_caption(key: dict) -> str:
    text = f"{e('keys')} <code>{escape(key['name'])}</code>"
    if (key.get("filename") or "").lower().endswith(".ovpn"):
        text += (
            f"\n\n{e('tip')} <b>Как открыть в OpenVPN:</b>\n"
            "📱 <b>Android:</b> ⋮ у файла → «Поделиться» → <b>OpenVPN Connect</b>\n"
            "🍏 <b>iPhone:</b> нажмите на файл → «Поделиться» → <b>OpenVPN</b>\n"
            "Или в OpenVPN Connect: <b>Import Profile → Upload File</b> → папка "
            "<code>Download/Telegram</code>"
        )
    return text


async def send_key(msg: Message, key: dict, created: bool = False):
    if created and key.get("filename"):
        await msg.answer_document(key_document(key), caption=key_caption(key))
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
    rows = [btn(f"{k['name']} · {k['script_name'] or k['server_name'] or '—'}", f"k:view:{k['id']}",
                emoji="keys", style=BLUE) for k in keys]
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
    await call.message.answer_document(key_document(key), caption=key_caption(key))


@router.callback_query(F.data.startswith("k:del:"))
async def cb_delete(call: CallbackQuery, db_user: dict):
    key = await db.get_key(int(call.data.split(":")[2]), db_user["id"])
    if not key:
        return await call.answer("Ключ не найден", show_alert=True)
    script = await db.get_script(key["script_id"]) if key.get("script_id") else None
    has_steps = bool(script and script["delete_steps"].strip())
    note = "Ключ будет удалён и на сервере." if has_steps else "Ключ удалится только из бота."
    await render(call, f"{e('warn')} Удалить ключ <code>{escape(key['name'])}</code>?\n\n{note}",
                 kb([btn("Да, удалить", f"k:delok:{key['id']}", emoji="trash", style=RED),
                     btn("Нет", f"k:view:{key['id']}", emoji="back", style=BLUE)]))


@router.callback_query(F.data.startswith("k:delok:") | F.data.startswith("k:delforce:"))
async def cb_delete_ok(call: CallbackQuery, db_user: dict):
    force = call.data.startswith("k:delforce:")
    key = await db.get_key(int(call.data.split(":")[2]), db_user["id"])
    if not key:
        return await call.answer("Ключ не найден", show_alert=True)
    script = await db.get_script(key["script_id"]) if key.get("script_id") else None
    server = await db.get_server(key["server_id"])
    if script and script["delete_steps"].strip() and server and not force:
        if db_user["id"] in _busy:
            return await call.answer("Дождитесь завершения предыдущей операции", show_alert=True)
        await render(call, f"{e('loading')} Удаляю ключ <code>{escape(key['name'])}</code> на сервере…")
        error, output = "", ""
        _busy.add(db_user["id"])
        try:
            async with await ssh.connect(creds(server)) as conn:
                steps = steps_of(script["delete_steps"], placeholders(server, db_user["id"], key["name"], script))
                res = await ssh.session(conn, steps, config.key_cmd_timeout)
            if not res.ok:
                error, output = res.error, res.tail()
        except ssh.SSHError as exc:
            error = str(exc)
        finally:
            _busy.discard(db_user["id"])
        if error:
            out = f"\n<pre>{escape(output[-1200:])}</pre>" if output else ""
            return await render(
                call, f"{e('error')} <b>{escape(error)}</b>{out}\n\nУдалить ключ только из бота?",
                kb([btn("Удалить из бота", f"k:delforce:{key['id']}", emoji="trash", style=RED),
                    btn("Отмена", f"k:view:{key['id']}", emoji="back", style=BLUE)]))
    await db.delete_key(key["id"])
    await call.answer("Ключ удалён")
    await show_list(call, db_user, 0)
