"""Смена IP сервера без переустановки: бот проверяет новый IP, правит конфигурацию VPN
на сервере (шаги «Смена IP» у скрипта) и обновляет IP во всех ключах этого сервера."""

from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import ssh
from ..common import ChangeIP, HasAccess, db
from ..config import config
from ..emoji import e
from ..ui import BLUE, GREEN, back, btn, kb, render
from .keys import _busy, placeholders, send_key_files, steps_of
from .servers import creds, valid_host

router = Router(name="ipchange")
router.message.filter(HasAccess())
router.callback_query.filter(HasAccess())


def ask_text(server: dict, error: str = "") -> str:
    err = f"\n\n{e('error')} {escape(error)}" if error else ""
    return (f"{e('globe')} <b>Смена IP</b> · {e('servers')} {escape(server['name'])}\n\n"
            f"Сейчас: <code>{escape(server['host'])}</code>\n\n"
            f"Отправьте <b>новый IP</b> сервера. Логин и пароль остаются прежними.\n\n"
            f"{e('tip')} Бот проверит подключение, обновит IP в настройках VPN на сервере "
            f"и во всех ключах этого сервера — переустанавливать ничего не нужно.{err}")


@router.callback_query(F.data.regexp(r"^s:ip:\d+$"))
async def cb_change_ip(call: CallbackQuery, state: FSMContext, db_user: dict):
    server = await db.get_server(int(call.data.split(":")[2]), db_user["id"])
    if not server:
        return await call.answer("Сервер не найден", show_alert=True)
    await state.set_state(ChangeIP.host)
    await state.update_data(server_id=server["id"])
    msg = await render(call, ask_text(server), kb(back(f"s:view:{server['id']}")))
    await state.update_data(prompt_id=msg.message_id)


async def _edit(message: Message, prompt_id: int | None, text: str, markup=None) -> Message:
    if prompt_id:
        try:
            res = await message.bot.edit_message_text(text, chat_id=message.chat.id, message_id=prompt_id,
                                                      reply_markup=markup)
            if isinstance(res, Message):
                return res
        except TelegramBadRequest:
            pass
    return await message.answer(text, reply_markup=markup)


@router.message(ChangeIP.host, F.text)
async def in_new_ip(message: Message, state: FSMContext, db_user: dict):
    new = message.text.strip().lower()
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    data = await state.get_data()
    server = await db.get_server(data.get("server_id", 0), db_user["id"])
    if not server:
        await state.clear()
        return await message.answer(f"{e('error')} Сервер не найден.")
    back_kb = kb(back(f"s:view:{server['id']}"))
    if not valid_host(new):
        return await _edit(message, data.get("prompt_id"), ask_text(server, "Некорректный адрес."), back_kb)
    if new == server["host"]:
        return await _edit(message, data.get("prompt_id"), ask_text(server, "Это текущий IP сервера."), back_kb)
    if db_user["id"] in _busy:
        return await _edit(message, data.get("prompt_id"),
                           ask_text(server, "Дождитесь завершения предыдущей операции."), back_kb)

    old = server["host"]
    header = (f"{e('globe')} <b>Смена IP</b> · {e('servers')} {escape(server['name'])}\n"
              f"<code>{escape(old)}</code> → <code>{escape(new)}</code>\n\n")
    log: list[str] = []

    async def show(line: str, replace: bool = False, markup=None) -> Message:
        if replace and log:
            log[-1] = line
        else:
            log.append(line)
        return await _edit(message, data.get("prompt_id"), header + "\n".join(log), markup)

    _busy.add(db_user["id"])
    try:
        msg = await show(f"{e('loading')} Подключаюсь к <code>{escape(new)}</code>…")
        await state.update_data(prompt_id=msg.message_id)
        data["prompt_id"] = msg.message_id
        moved = {**server, "host": new}
        try:
            conn = await ssh.connect(creds(moved))
        except ssh.SSHError as exc:
            # остаёмся в режиме ввода — можно сразу отправить другой IP
            return await show(f"{e('error')} {escape(str(exc))}\n\nОтправьте другой IP или вернитесь назад.",
                              replace=True, markup=back_kb)
        async with conn:
            try:
                await conn.check_root()
            except ssh.SSHError as exc:
                return await show(f"{e('error')} {escape(str(exc))}", replace=True, markup=back_kb)
            await state.clear()
            await db.set_server_host(server["id"], new)
            await show(f"{e('ok')} Подключение по новому IP работает, IP сохранён", replace=True)

            # настройки VPN на сервере
            values_extra = {"old_ip": old, "new_ip": new, "old_ip_re": old.replace(".", r"\.")}
            for script in await db.installed_scripts(server["id"]):
                name = escape(script["name"])
                steps = steps_of(script["ip_steps"], {**placeholders(moved, db_user["id"], "", script),
                                                      **values_extra})
                if not steps:
                    await show(f"{e('warn')} {name}: у скрипта не заданы шаги «Смена IP» — новые ключи "
                               f"могут выдаваться со старым IP (админ-панель → Скрипты → Смена IP)")
                    continue
                await show(f"{e('loading')} {name}: обновляю настройки на сервере…")
                try:
                    res = await conn.session(steps, config.key_cmd_timeout)
                except ssh.SSHError as exc:
                    res = ssh.SessionResult(False, "", str(exc))
                if res.ok:
                    await show(f"{e('ok')} {name}: настройки на сервере обновлены", replace=True)
                else:
                    await show(f"{e('error')} {name}: {escape(res.error[:200])}", replace=True)

        # ключи в боте
        updated = 0
        for key in await db.server_keys(server["id"]):
            if old in key["value"]:
                await db.set_key_value(key["id"], key["value"].replace(old, new))
                updated += 1
        await show(f"{e('keys')} Обновлено ключей: <b>{updated}</b>"
                   + ("\n\nСтарые файлы ключей на устройствах больше не подключатся — "
                      "отправьте пользователям обновлённые." if updated else ""),
                   markup=kb(btn(f"Получить обновлённые ключи ({updated})", f"s:ipsend:{server['id']}",
                                 emoji="upload", style=GREEN) if updated else None,
                             btn("К серверу", f"s:view:{server['id']}", emoji="servers", style=BLUE)))
    finally:
        _busy.discard(db_user["id"])


@router.callback_query(F.data.regexp(r"^s:ipsend:\d+$"))
async def cb_send_updated(call: CallbackQuery, db_user: dict):
    server = await db.get_server(int(call.data.split(":")[2]), db_user["id"])
    if not server:
        return await call.answer("Сервер не найден", show_alert=True)
    keys = [k for k in await db.server_keys(server["id"]) if k["owner_id"] == db_user["id"]]
    if not keys:
        return await call.answer("Ключей нет", show_alert=True)
    await call.answer(f"Отправляю ключи: {len(keys)}")
    await send_key_files(call.message, keys)
