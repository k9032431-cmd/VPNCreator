import re
import time
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..common import ROLE_TITLES, HasAccess, IsAdmin, Settings, db, fmt_size
from ..emoji import e
from ..ui import BLUE, GREEN, RED, back, btn, kb, render

router = Router(name="settings")
router.message.filter(HasAccess())
router.callback_query.filter(HasAccess())

admin = Router(name="settings_admin")
admin.message.filter(IsAdmin())
admin.callback_query.filter(IsAdmin())
router.include_router(admin)

FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

PLACEHOLDERS = (
    f"{e('tip')} <b>Подстановки в командах:</b>\n"
    "<code>{name}</code> — название ключа\n"
    "<code>{script}</code> — путь к первому загруженному скрипту\n"
    "<code>{dir}</code> — папка со скриптами на сервере (~/vpncreator)\n"
    "<code>{server_ip}</code> — IP сервера\n"
    "<code>{user_id}</code> — Telegram ID пользователя\n\n"
    "Все команды выполняются из папки ~/vpncreator."
)


def short(text: str, limit: int = 600) -> str:
    text = text.strip()
    return escape(text if len(text) <= limit else text[:limit] + "\n…")


# ---------------- обычный пользователь: профиль ----------------

@router.callback_query(F.data == "set:menu")
async def cb_menu(call: CallbackQuery, state: FSMContext, db_user: dict):
    await state.clear()
    if db_user["role"] == "admin":
        return await show_admin_settings(call)
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


# ---------------- админ: настройка скрипта ----------------

async def show_admin_settings(target: CallbackQuery | Message):
    files = await db.list_files()
    steps = [line for line in (await db.get("install_cmds")).splitlines()
             if line.strip() and not line.strip().startswith("#")]
    key_cmd = (await db.get("key_cmd")).strip()
    mark = lambda ok: e("ok") if ok else e("cancel")  # noqa: E731
    text = (
        f"{e('settings')} <b>Настройки скрипта</b>\n\n"
        f"Здесь один раз настраивается, что бот делает на сервере.\n"
        f"При создании ключа бот: загружает скрипты → выполняет команды установки "
        f"(только один раз на сервер) → выполняет команду создания ключа.\n\n"
        f"{mark(files)} {e('file')} Скрипты: <b>{len(files)}</b>\n"
        f"{mark(steps)} {e('terminal')} Команды установки: <b>{len(steps)}</b>\n"
        f"{mark(key_cmd)} {e('key_new')} Команда создания ключа\n"
        f"{e('trash')} Команда удаления: {'задана' if (await db.get('delete_cmd')).strip() else 'нет'}\n"
        f"{e('filter')} Фильтр ключа: {'задан' if (await db.get('key_regex')).strip() else 'весь вывод'}\n"
        f"{e('refresh')} Версия конфигурации: <b>{await db.config_version()}</b>"
    )
    markup = kb(
        [btn("Скрипты", "set:files", emoji="file", style=BLUE),
         btn("Установка", "set:install", emoji="terminal", style=BLUE)],
        [btn("Создание ключа", "set:keycmd", emoji="key_new", style=BLUE),
         btn("Удаление ключа", "set:delcmd", emoji="trash", style=BLUE)],
        [btn("Фильтр ключа", "set:regex", emoji="filter", style=BLUE),
         btn("Баннер", "set:banner", emoji="image", style=BLUE)],
        btn("Переустановить на всех серверах", "set:bump", emoji="refresh", style=GREEN),
        back(),
    )
    await render(target, text, markup)


@admin.callback_query(F.data == "set:admin")
async def cb_admin_settings(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_admin_settings(call)


@admin.callback_query(F.data == "set:bump")
async def cb_bump(call: CallbackQuery):
    v = await db.bump_version()
    await call.answer(f"Версия {v}: на каждом сервере установка выполнится заново при следующем ключе",
                      show_alert=True)
    await show_admin_settings(call)


async def _after_input(message: Message, state: FSMContext, note: str):
    await state.clear()
    await message.answer(f"{e('ok')} {note}",
                         reply_markup=kb(btn("К настройкам", "set:admin", emoji="settings", style=BLUE)))


# ----- скрипты -----

@admin.callback_query(F.data == "set:files")
async def cb_files(call: CallbackQuery, state: FSMContext):
    await state.set_state(Settings.file)
    files = await db.list_files()
    listing = "\n".join(f"{e('file')} <code>{escape(f['name'])}</code> · {fmt_size(f['size'])}" for f in files) \
        or "Файлов пока нет."
    rows = [btn(f"Удалить {f['name']}", f"set:fdel:{f['id']}", emoji="trash", style=RED) for f in files]
    await render(
        call,
        f"{e('file')} <b>Скрипты</b>\n\n{listing}\n\n"
        f"{e('upload')} Отправьте файл скрипта документом — он загрузится в <code>~/vpncreator/</code> "
        f"на сервере и станет исполняемым. Файл с тем же именем заменится.",
        kb(*rows, back("set:admin")),
    )


@admin.message(Settings.file, F.document)
async def in_file(message: Message, state: FSMContext):
    doc = message.document
    name = doc.file_name or "script.sh"
    if not FILENAME_RE.match(name):
        return await message.answer(f"{e('error')} Имя файла: только латиница, цифры, точка, - и _.")
    if doc.file_size > 20 * 1024 * 1024:
        return await message.answer(f"{e('error')} Файл больше 20 МБ.")
    content = (await message.bot.download(doc)).read()
    await db.put_file(name, content)
    v = await db.bump_version()
    await _after_input(message, state,
                       f"Скрипт <code>{escape(name)}</code> сохранён ({fmt_size(len(content))}). "
                       f"Версия конфигурации: {v}.")


@admin.callback_query(F.data.startswith("set:fdel:"))
async def cb_file_delete(call: CallbackQuery, state: FSMContext):
    f = await db.get_file(int(call.data.split(":")[2]))
    if f:
        await db.delete_file(f["id"])
        await db.bump_version()
        await call.answer(f"{f['name']} удалён")
    await cb_files(call, state)


# ----- текстовые настройки -----

TEXT_SETTINGS = {
    "install": dict(
        key="install_cmds", state=Settings.install, emoji="terminal", title="Команды установки", bump=True,
        about=("Каждая строка — отдельный шаг. Выполняются по порядку <b>один раз на сервер</b> "
               "(повторно — только после изменения скриптов/команд). Строки с # пропускаются.\n\n"
               "Пример:\n<pre>apt-get update -y\nbash {script} install</pre>"),
    ),
    "keycmd": dict(
        key="key_cmd", state=Settings.key_cmd, emoji="key_new", title="Команда создания ключа", bump=False,
        about=("Выполняется <b>каждый раз</b> при создании ключа. Всё, что команда выведет, "
               "станет ключом (или используйте «Фильтр ключа»).\n\n"
               "Пример:\n<pre>bash {script} add {name}</pre>"),
    ),
    "delcmd": dict(
        key="delete_cmd", state=Settings.delete_cmd, emoji="trash", title="Команда удаления ключа", bump=False,
        about=("Необязательно. Выполняется при удалении ключа из бота.\n\n"
               "Пример:\n<pre>bash {script} del {name}</pre>"),
    ),
    "regex": dict(
        key="key_regex", state=Settings.regex, emoji="filter", title="Фильтр ключа", bump=False,
        about=("Необязательно. Регулярное выражение — из вывода берутся только совпадения. "
               "Удобно, если скрипт печатает лишний текст.\n\n"
               "Пример для VLESS:\n<pre>vless://\\S+</pre>"),
    ),
}
STATE_TO_SETTING = {v["state"].state: k for k, v in TEXT_SETTINGS.items()}


@admin.callback_query(F.data.in_({f"set:{k}" for k in TEXT_SETTINGS}))
async def cb_text_setting(call: CallbackQuery, state: FSMContext):
    code = call.data.split(":")[1]
    s = TEXT_SETTINGS[code]
    await state.set_state(s["state"])
    current = (await db.get(s["key"])).strip()
    cur = f"<pre>{short(current)}</pre>" if current else "<i>не задано</i>"
    await render(
        call,
        f"{e(s['emoji'])} <b>{s['title']}</b>\n\n{s['about']}\n\n<b>Сейчас:</b>\n{cur}\n\n"
        f"{e('edit')} Отправьте новое значение сообщением.\n\n{PLACEHOLDERS}",
        kb(btn("Очистить", f"set:clear:{code}", emoji="trash", style=RED) if current else None,
           back("set:admin")),
    )


@admin.callback_query(F.data.startswith("set:clear:"))
async def cb_clear(call: CallbackQuery, state: FSMContext):
    s = TEXT_SETTINGS[call.data.split(":")[2]]
    await db.set(s["key"], "")
    if s["bump"]:
        await db.bump_version()
    await state.clear()
    await call.answer("Очищено")
    await show_admin_settings(call)


@admin.message(Settings.install, F.text)
@admin.message(Settings.key_cmd, F.text)
@admin.message(Settings.delete_cmd, F.text)
@admin.message(Settings.regex, F.text)
async def in_text_setting(message: Message, state: FSMContext):
    s = TEXT_SETTINGS[STATE_TO_SETTING[await state.get_state()]]
    value = message.text.strip()
    if s["key"] == "key_regex":
        try:
            re.compile(value)
        except re.error as exc:
            return await message.answer(f"{e('error')} Некорректное выражение: {escape(str(exc))}")
    await db.set(s["key"], value)
    note = f"«{s['title']}» сохранено."
    if s["bump"]:
        note += f" Версия конфигурации: {await db.bump_version()} — серверы переустановятся при следующем ключе."
    await _after_input(message, state, note)


# ----- баннер -----

@admin.callback_query(F.data == "set:banner")
async def cb_banner(call: CallbackQuery, state: FSMContext):
    await state.set_state(Settings.banner)
    has = bool(await db.get("banner"))
    await render(
        call,
        f"{e('image')} <b>Баннер главного меню</b>\n\n"
        f"Отправьте картинку (как фото) — она будет показываться над главным меню.\n"
        f"Рекомендуемый размер: 1280×720.\n\nСейчас: {'установлен' if has else 'нет'}",
        kb(btn("Убрать баннер", "set:bannerdel", emoji="trash", style=RED) if has else None, back("set:admin")),
    )


@admin.message(Settings.banner, F.photo)
async def in_banner(message: Message, state: FSMContext):
    await db.set("banner", message.photo[-1].file_id)
    await _after_input(message, state, "Баннер установлен.")


@admin.callback_query(F.data == "set:bannerdel")
async def cb_banner_del(call: CallbackQuery, state: FSMContext):
    await db.set("banner", "")
    await state.clear()
    await call.answer("Баннер убран")
    await show_admin_settings(call)


@admin.message(Settings.file)
@admin.message(Settings.banner)
async def in_wrong(message: Message):
    try:
        await message.reply(f"{e('warn')} Ожидаю файл (скрипт — документом, баннер — фото). /cancel — отмена.")
    except TelegramBadRequest:
        pass
