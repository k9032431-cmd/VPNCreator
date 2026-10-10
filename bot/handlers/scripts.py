"""Админ-панель → Скрипты: мастер добавления VPN-скрипта и его редактирование."""

import base64
import re
from html import escape
from urllib.parse import urlparse

import aiohttp
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..common import IsAdmin, ScriptForm, db, fmt_size
from ..emoji import e
from ..ssh import ENTER_WORDS, parse_steps
from ..ui import BLUE, GREEN, RED, back, btn, kb, render

router = Router(name="scripts")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_FILE = 20 * 1024 * 1024
WIZARD = ["name", "file", "install_steps", "check_cmd", "key_steps", "result"]
STEP_FIELDS = {
    "install_steps": ("terminal", "Установка",
                      "Выполняется <b>один раз на сервер</b> — при первом ключе."),
    "key_steps": ("key_new", "Создание ключа",
                  "Выполняется <b>каждый раз</b>, когда пользователь создаёт ключ. "
                  "Название ключа — <code>{name}</code>."),
    "delete_steps": ("trash", "Удаление ключа",
                     "Необязательно. Выполняется, когда пользователь удаляет ключ."),
    "ip_steps": ("globe", "Смена IP",
                 "Необязательно. Выполняется, когда в боте меняют IP сервера (кнопка «Сменить IP»), — "
                 "чтобы новые ключи выдавались с новым IP. Подстановки: <code>{old_ip}</code>, "
                 "<code>{new_ip}</code>, <code>{old_ip_re}</code> (старый IP для sed, с экранированными точками).\n"
                 "Пример для OpenVPN:\n<pre>sed -i 's/{old_ip_re}/{new_ip}/g' "
                 "/etc/openvpn/client-template.txt /root/*.ovpn</pre>"),
}
TITLES = {"name": "Название", "file": "Файл скрипта", "check_cmd": "Проверка установки", "result": "Результат"}

PLACEHOLDERS = (
    f"{e('tip')} <b>Подстановки:</b> <code>{{name}}</code> — название ключа, "
    "<code>{file}</code> — путь к файлу скрипта, <code>{server_ip}</code> — IP сервера, "
    "<code>{user_id}</code> — ID пользователя."
)
STEPS_HELP = (
    "Отправляйте <b>всё, что вы напечатали бы в терминале</b> — команды и ответы скрипту, "
    "каждое с новой строки. Можно одним сообщением через Enter или по одному.\n"
    "• <code>enter</code> — просто нажать Enter (оставить значение по умолчанию)\n"
    "• бот вводит следующую строку, когда скрипт ждёт ответа\n"
    "• команды выполняются в папке <code>~/vpncreator</code>, куда загружен скрипт"
)


def pretty_steps(steps: list[str], limit: int = 2500) -> str:
    if not steps:
        return "<i>пока пусто</i>"
    out, size = [], 0
    for i, s in enumerate(steps, 1):
        if s.strip().lower() in ENTER_WORDS:
            line = f"<code>{i:>2}.</code> {e('enter')} Enter"
        else:
            line = f"<code>{i:>2}.</code> <code>{escape(s)}</code>"
        size += len(line)
        if size > limit:
            out.append(f"… ещё {len(steps) - i + 1}")
            break
        out.append(line)
    return "\n".join(out)


# ================================================================ список и карточка

@router.callback_query(F.data == "sc:list")
async def cb_list(call: CallbackQuery, state: FSMContext):
    await state.clear()
    scripts = await db.list_scripts()
    rows = [btn(f"{s['name']}" + ("" if s["enabled"] else " (выкл.)"), f"sc:v:{s['id']}",
                emoji="file", style=BLUE if s["enabled"] else None) for s in scripts]
    text = (f"{e('file')} <b>Скрипты VPN</b>\n\n"
            + ("Скриптов пока нет. Добавьте первый — бот проведёт по шагам." if not scripts else
               "Пользователь выбирает скрипт при создании ключа (если включён только один — выбирается сам)."))
    await render(call, text, kb(*rows, btn("Добавить скрипт", "sc:new", emoji="server_add", style=GREEN),
                                back("a:menu")))


async def script_card(script_id: int) -> tuple[str, object] | None:
    s = await db.get_script(script_id)
    if not s:
        return None
    install = parse_steps(s["install_steps"])
    keysteps = parse_steps(s["key_steps"])
    delsteps = parse_steps(s["delete_steps"])
    ipsteps = parse_steps(s["ip_steps"])
    if s["filename"]:
        file = f"<code>{escape(s['filename'])}</code> · {fmt_size(len(s['content'] or b''))}"
        if s["source_url"]:
            file += f"\n      {e('globe')} {escape(s['source_url'])}"
    else:
        file = "<i>без файла</i>"
    if s["result_type"] == "file":
        result = f"файл <code>{escape(s['result_path'] or '—')}</code>"
    else:
        result = "вывод команд" + (f", фильтр <code>{escape(s['key_regex'])}</code>" if s["key_regex"] else "")
    servers = await db._scalar("SELECT COUNT(*) FROM server_scripts WHERE script_id=? AND version=?",
                               (script_id, s["version"]))
    text = (
        f"{e('file')} <b>{escape(s['name'])}</b>  {e('ok') if s['enabled'] else e('ban') + ' выключен'}\n\n"
        f"{e('file')} Файл: {file}\n"
        f"{e('terminal')} Установка: <b>{len(install)}</b> шаг.\n"
        f"{e('search')} Проверка: "
        f"{('<code>' + escape(s['check_cmd']) + '</code>') if s['check_cmd'] else e('warn') + ' не задана'}\n"
        f"{e('key_new')} Создание ключа: <b>{len(keysteps)}</b> шаг.\n"
        f"{e('upload')} Ключ: {result}\n"
        f"{e('trash')} Удаление ключа: {len(delsteps) or '—'}\n"
        f"{e('globe')} Смена IP: {len(ipsteps) or '—'}\n"
        f"{e('servers')} Установлен на серверах: <b>{servers}</b> · версия {s['version']}"
    )
    sid = s["id"]
    markup = kb(
        [btn("Название", f"sc:e:{sid}:name", emoji="edit", style=BLUE),
         btn("Файл", f"sc:e:{sid}:file", emoji="file", style=BLUE)],
        [btn("Установка", f"sc:e:{sid}:install_steps", emoji="terminal", style=BLUE),
         btn("Проверка", f"sc:e:{sid}:check_cmd", emoji="search", style=BLUE)],
        [btn("Создание ключа", f"sc:e:{sid}:key_steps", emoji="key_new", style=BLUE),
         btn("Результат", f"sc:e:{sid}:result", emoji="upload", style=BLUE)],
        [btn("Удаление ключа", f"sc:e:{sid}:delete_steps", emoji="trash", style=BLUE),
         btn("Смена IP", f"sc:e:{sid}:ip_steps", emoji="globe", style=BLUE)],
        [btn("Выключить" if s["enabled"] else "Включить", f"sc:tog:{sid}",
             emoji="ban" if s["enabled"] else "ok", style=RED if s["enabled"] else GREEN),
         btn("Переустановить везде", f"sc:bump:{sid}", emoji="refresh", style=BLUE)],
        btn("Удалить скрипт", f"sc:del:{sid}", emoji="trash", style=RED),
        back("sc:list"),
    )
    return text, markup


async def show_card(target: Message | CallbackQuery, script_id: int):
    card = await script_card(script_id)
    if not card:
        if isinstance(target, CallbackQuery):
            return await target.answer("Скрипт не найден", show_alert=True)
        return await target.answer("Скрипт не найден")
    await render(target, *card)


@router.callback_query(F.data.regexp(r"^sc:v:\d+$"))
async def cb_view(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_card(call, int(call.data.split(":")[2]))


@router.callback_query(F.data.regexp(r"^sc:tog:\d+$"))
async def cb_toggle(call: CallbackQuery):
    s = await db.get_script(int(call.data.split(":")[2]))
    if s:
        await db.update_script(s["id"], enabled=0 if s["enabled"] else 1)
    await show_card(call, s["id"] if s else 0)


@router.callback_query(F.data.regexp(r"^sc:bump:\d+$"))
async def cb_bump(call: CallbackQuery):
    sid = int(call.data.split(":")[2])
    await db.bump_script(sid)
    await call.answer("При следующем ключе на каждом сервере установка запустится заново "
                      "(если «Проверка» не скажет, что всё уже установлено)", show_alert=True)
    await show_card(call, sid)


@router.callback_query(F.data.regexp(r"^sc:del:\d+$"))
async def cb_delete(call: CallbackQuery):
    s = await db.get_script(int(call.data.split(":")[2]))
    if not s:
        return await call.answer("Скрипт не найден", show_alert=True)
    await render(call, f"{e('warn')} Удалить скрипт <b>{escape(s['name'])}</b>?\n\n"
                       f"Созданные ключи останутся у пользователей.",
                 kb([btn("Да, удалить", f"sc:delok:{s['id']}", emoji="trash", style=RED),
                     btn("Нет", f"sc:v:{s['id']}", emoji="back", style=BLUE)]))


@router.callback_query(F.data.regexp(r"^sc:delok:\d+$"))
async def cb_delete_ok(call: CallbackQuery, state: FSMContext):
    await db.delete_script(int(call.data.split(":")[2]))
    await call.answer("Скрипт удалён")
    await cb_list(call, state)


# ================================================================ мастер / редактирование

def header(data: dict) -> str:
    if data.get("mode") == "wizard":
        n = WIZARD.index(data["field"] if data["field"] in WIZARD else "result") + 1
        name = data.get("draft", {}).get("name")
        title = f"{e('server_add')} <b>Новый скрипт</b>" + (f" · {escape(name)}" if name else "")
        return f"{title} · шаг {n}/{len(WIZARD)}\n\n"
    return f"{e('edit')} <b>{escape(data.get('script_name', ''))}</b>\n\n"


def nav(data: dict, *extra) -> object:
    cancel_cb = "sc:list" if data.get("mode") == "wizard" else f"sc:v:{data.get('script_id')}"
    return kb(*extra, btn("Отмена" if data.get("mode") == "wizard" else "Назад", cancel_cb,
                          emoji="cancel" if data.get("mode") == "wizard" else "back", style=RED))


async def ask(target: Message | CallbackQuery, state: FSMContext, field: str):
    """Показывает вопрос для поля field (общий для мастера и редактирования)."""
    data = await state.get_data()
    data["field"] = field
    cur = data.get("current", {})
    if field == "name":
        await state.set_state(ScriptForm.name)
        text = (f"{e('tag')} <b>Название скрипта</b>\n\nКак он будет называться у пользователей? "
                f"Например: <code>OpenVPN</code>, <code>WireGuard</code>, <code>VLESS</code>.")
        if cur.get("name"):
            text += f"\n\nСейчас: <b>{escape(cur['name'])}</b>"
        markup = nav(data)
    elif field == "file":
        await state.set_state(ScriptForm.file)
        text = (f"{e('file')} <b>Файл скрипта</b>\n\n"
                f"{e('upload')} Отправьте файл документом\n"
                f"{e('globe')} или ссылку на него (GitHub, raw-ссылка и т.п.)\n\n"
                f"Файл загрузится на сервер в <code>~/vpncreator/</code> и станет исполняемым.")
        if cur.get("filename"):
            text += f"\n\nСейчас: <code>{escape(cur['filename'])}</code>"
        markup = nav(data, btn("Без файла", "sc:f:nofile", emoji="cancel", style=BLUE))
    elif field in STEP_FIELDS:
        await state.set_state(ScriptForm.steps)
        emoji, title, about = STEP_FIELDS[field]
        steps = data.get("steps", [])
        text = (f"{e(emoji)} <b>{title}</b>\n{about}\n\n{STEPS_HELP}\n\n"
                f"<b>Шаги ({len(steps)}):</b>\n{pretty_steps(steps)}\n\n{PLACEHOLDERS}")
        buttons = [btn("Готово", "sc:st:done", emoji="ok", style=GREEN)]
        if steps:
            buttons += [btn("Убрать последний", "sc:st:undo", emoji="back", style=BLUE),
                        btn("Очистить", "sc:st:clear", emoji="trash", style=RED)]
        markup = nav(data, buttons[0], buttons[1:] or None)
    elif field == "check_cmd":
        await state.set_state(ScriptForm.check)
        text = (f"{e('search')} <b>Проверка установки</b> (необязательно)\n\n"
                f"Команда, которая успешна, если скрипт <b>уже установлен</b> на сервере. "
                f"Тогда бот не будет запускать установку повторно (важно для скриптов, "
                f"которые при втором запуске показывают меню).\n\n"
                f"{e('warn')} <b>Обязательно задайте, если скрипт при повторном запуске показывает меню</b> "
                f"(как OpenVPN): иначе на сервере, где VPN уже стоит, ответы установки попадут в меню.\n\n"
                f"Пример для OpenVPN:\n<pre>test -f /etc/openvpn/server.conf</pre>")
        if cur.get("check_cmd"):
            text += f"\nСейчас: <code>{escape(cur['check_cmd'])}</code>"
        markup = nav(data, btn("Пропустить" if data.get("mode") == "wizard" else "Убрать проверку",
                               "sc:c:skip", emoji="next", style=BLUE))
    else:  # result
        await state.set_state(ScriptForm.result_type)
        text = (f"{e('upload')} <b>Где взять ключ?</b>\n\n"
                f"{e('file')} <b>Файл на сервере</b> — скрипт создаёт файл (например <code>.ovpn</code>), "
                f"бот скачает его и отправит пользователю.\n"
                f"{e('terminal')} <b>Вывод команд</b> — ключ печатается в терминал "
                f"(например ссылка <code>vless://…</code>).")
        markup = nav(data, [btn("Файл на сервере", "sc:r:file", emoji="file", style=GREEN),
                            btn("Вывод команд", "sc:r:output", emoji="terminal", style=BLUE)])
    text = header(data) + text
    await state.update_data(field=field)
    if isinstance(target, CallbackQuery):
        msg = await render(target, text, markup)
    else:
        msg = None
        prompt_id = data.get("prompt_id")
        if prompt_id:
            try:
                msg = await target.bot.edit_message_text(text, chat_id=target.chat.id, message_id=prompt_id,
                                                         reply_markup=markup, disable_web_page_preview=True)
            except TelegramBadRequest as exc:
                if "not modified" in str(exc):
                    return
                msg = None
        if msg is None or msg is True:
            msg = await target.answer(text, reply_markup=markup, disable_web_page_preview=True)
    if msg is not None and msg is not True:
        await state.update_data(prompt_id=msg.message_id)


async def field_done(target: Message | CallbackQuery, state: FSMContext, **values):
    """Поле заполнено: в мастере — следующий шаг, при редактировании — сохранить."""
    data = await state.get_data()
    if data.get("mode") == "wizard":
        draft = {**data.get("draft", {}), **values}
        idx = WIZARD.index(data["field"]) if data["field"] in WIZARD else len(WIZARD) - 1
        await state.update_data(draft=draft, steps=[])
        if data["field"] == "result" or idx + 1 >= len(WIZARD):
            if "content" in draft and draft["content"] is not None:
                draft["content"] = base64.b64decode(draft["content"])
            script_id = await db.add_script(**draft)
            await state.clear()
            if isinstance(target, Message):
                await target.answer(f"{e('ok')} <b>Скрипт добавлен!</b>")
            return await show_card(target, script_id)
        return await ask(target, state, WIZARD[idx + 1])
    if "content" in values and values["content"] is not None:
        values["content"] = base64.b64decode(values["content"])
    await db.update_script(data["script_id"], **values)
    await state.clear()
    if isinstance(target, Message):
        await target.answer(f"{e('ok')} Сохранено.")
    await show_card(target, data["script_id"])


@router.callback_query(F.data == "sc:new")
async def cb_new(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(mode="wizard", draft={}, steps=[])
    await ask(call, state, "name")


@router.callback_query(F.data.regexp(r"^sc:e:\d+:\w+$"))
async def cb_edit(call: CallbackQuery, state: FSMContext):
    _, _, sid, field = call.data.split(":")
    s = await db.get_script(int(sid))
    if not s:
        return await call.answer("Скрипт не найден", show_alert=True)
    await state.clear()
    steps = parse_steps(s[field]) if field in STEP_FIELDS else []
    await state.update_data(mode="edit", script_id=s["id"], script_name=s["name"], steps=steps,
                            current={k: s[k] for k in ("name", "filename", "check_cmd")})
    await ask(call, state, field)


async def _delete(message: Message):
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


# ---------- название ----------

@router.message(ScriptForm.name, F.text)
async def in_name(message: Message, state: FSMContext):
    name = message.text.strip()[:40]
    await _delete(message)
    await field_done(message, state, name=name)


# ---------- файл ----------

def raw_github(url: str) -> str:
    m = re.match(r"^https?://github\.com/([^/]+)/([^/]+)/blob/(.+)$", url)
    return f"https://raw.githubusercontent.com/{m[1]}/{m[2]}/{m[3]}" if m else url


async def fetch(url: str) -> bytes:
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status != 200:
                raise ValueError(f"сервер ответил {resp.status}")
            data = await resp.content.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise ValueError("файл больше 20 МБ")
    return data


def safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:64].strip("._") or "script.sh"
    return name


@router.message(ScriptForm.file, F.document)
async def in_file_doc(message: Message, state: FSMContext):
    doc = message.document
    if doc.file_size > MAX_FILE:
        return await message.reply(f"{e('error')} Файл больше 20 МБ.")
    content = (await message.bot.download(doc)).read()
    await field_done(message, state, filename=safe_name(doc.file_name or "script.sh"),
                     content=base64.b64encode(content).decode(), source_url=None)


@router.message(ScriptForm.file, F.text)
async def in_file_url(message: Message, state: FSMContext):
    url = raw_github(message.text.strip())
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return await message.reply(f"{e('error')} Отправьте файл документом или ссылку http(s)://…")
    wait = await message.reply(f"{e('loading')} Скачиваю…")
    try:
        content = await fetch(url)
    except Exception as exc:  # noqa: BLE001 — показываем админу любую причину
        return await wait.edit_text(f"{e('error')} Не удалось скачать: {escape(str(exc))}")
    await _delete(wait)
    await field_done(message, state, filename=safe_name(parsed.path.rsplit("/", 1)[-1]),
                     content=base64.b64encode(content).decode(), source_url=url)


@router.callback_query(ScriptForm.file, F.data == "sc:f:nofile")
async def cb_nofile(call: CallbackQuery, state: FSMContext):
    await field_done(call, state, filename=None, content=None, source_url=None)


# ---------- шаги ----------

@router.message(ScriptForm.steps, F.text)
async def in_steps(message: Message, state: FSMContext):
    new = parse_steps(message.text)
    await _delete(message)
    data = await state.get_data()
    await state.update_data(steps=data.get("steps", []) + new)
    await ask(message, state, data["field"])


@router.callback_query(ScriptForm.steps, F.data.in_({"sc:st:undo", "sc:st:clear"}))
async def cb_steps_edit(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    steps = data.get("steps", [])
    steps = steps[:-1] if call.data == "sc:st:undo" else []
    await state.update_data(steps=steps)
    await ask(call, state, data["field"])


@router.callback_query(ScriptForm.steps, F.data == "sc:st:done")
async def cb_steps_done(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    steps = data.get("steps", [])
    if not steps and data["field"] == "key_steps":
        return await call.answer("Добавьте хотя бы один шаг для создания ключа", show_alert=True)
    await field_done(call, state, **{data["field"]: "\n".join(steps)})


# ---------- проверка ----------

@router.message(ScriptForm.check, F.text)
async def in_check(message: Message, state: FSMContext):
    cmd = message.text.strip()
    await _delete(message)
    await field_done(message, state, check_cmd=cmd)


@router.callback_query(ScriptForm.check, F.data == "sc:c:skip")
async def cb_check_skip(call: CallbackQuery, state: FSMContext):
    await field_done(call, state, check_cmd="")


# ---------- результат ----------

@router.callback_query(ScriptForm.result_type, F.data.in_({"sc:r:file", "sc:r:output"}))
async def cb_result_type(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if call.data == "sc:r:file":
        await state.set_state(ScriptForm.result_path)
        text = (f"{e('file')} <b>Путь к файлу ключа</b>\n\n"
                f"Где скрипт сохраняет файл? Используйте <code>{{name}}</code> вместо названия ключа.\n\n"
                f"Пример для OpenVPN:\n<pre>/root/{{name}}.ovpn</pre>")
        markup = nav(data)
    else:
        await state.set_state(ScriptForm.result_regex)
        text = (f"{e('filter')} <b>Фильтр ключа</b>\n\n"
                f"Регулярное выражение — из вывода возьмётся только совпадение. "
                f"Например для VLESS:\n<pre>vless://\\S+</pre>\n"
                f"Или нажмите «Весь вывод».")
        markup = nav(data, btn("Весь вывод", "sc:r:all", emoji="next", style=BLUE))
    await render(call, header(data) + text, markup)


@router.message(ScriptForm.result_path, F.text)
async def in_result_path(message: Message, state: FSMContext):
    path = message.text.strip()
    if "\n" in path or len(path) > 300:
        return await message.reply(f"{e('error')} Нужен один путь, например <code>/root/{{name}}.ovpn</code>")
    await _delete(message)
    await field_done(message, state, result_type="file", result_path=path, key_regex="")


@router.message(ScriptForm.result_regex, F.text)
async def in_result_regex(message: Message, state: FSMContext):
    value = message.text.strip()
    try:
        re.compile(value)
    except re.error as exc:
        return await message.reply(f"{e('error')} Некорректное выражение: {escape(str(exc))}")
    await _delete(message)
    await field_done(message, state, result_type="output", key_regex=value, result_path="")


@router.callback_query(ScriptForm.result_regex, F.data == "sc:r:all")
async def cb_result_all(call: CallbackQuery, state: FSMContext):
    await field_done(call, state, result_type="output", key_regex="", result_path="")


# ---------- неверный ввод ----------

@router.message(ScriptForm.file)
@router.message(ScriptForm.steps)
@router.message(ScriptForm.name)
async def in_wrong(message: Message):
    await message.reply(f"{e('warn')} Не тот формат. Следуйте подсказке выше или /cancel.")
