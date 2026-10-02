"""Кнопки, клавиатуры и отрисовка экранов."""

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .emoji import icon_id, plain

# Цвета кнопок (Bot API 9.4): primary — синяя, success — зелёная, danger — красная
BLUE, GREEN, RED = "primary", "success", "danger"


def btn(text: str, cb: str | None = None, *, emoji: str | None = None, style: str | None = None,
        url: str | None = None, copy: str | None = None) -> InlineKeyboardButton:
    icon = icon_id(emoji) if emoji else None
    label = f"{plain(emoji)} {text}" if emoji and not icon else text
    return InlineKeyboardButton(
        text=label,
        callback_data=cb,
        url=url,
        copy_text=CopyTextButton(text=copy) if copy else None,
        style=style,
        icon_custom_emoji_id=icon,
    )


def kb(*rows: list[InlineKeyboardButton] | InlineKeyboardButton | None) -> InlineKeyboardMarkup:
    out = []
    for row in rows:
        if row is None:
            continue
        row = row if isinstance(row, list) else [row]
        row = [b for b in row if b is not None]
        if row:
            out.append(row)
    return InlineKeyboardMarkup(inline_keyboard=out)


def back(cb: str = "m:home", text: str = "Назад") -> InlineKeyboardButton:
    return btn(text, cb, emoji="back", style=RED)


def home() -> InlineKeyboardButton:
    return btn("Главное меню", "m:home", emoji="home", style=BLUE)


def cancel() -> InlineKeyboardMarkup:
    return kb(btn("Отмена", "m:cancel", emoji="cancel", style=RED))


async def render(target: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None,
                 *, photo: str | None = None) -> Message:
    """Показывает экран: редактирует текущее сообщение бота или отправляет новое."""
    if isinstance(target, CallbackQuery):
        try:
            await target.answer()
        except TelegramBadRequest:
            pass
        msg = target.message
        if isinstance(msg, Message):
            try:
                if photo and msg.photo:
                    return await msg.edit_caption(caption=text, reply_markup=markup)
                if not photo and not msg.photo:
                    return await msg.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
            except TelegramBadRequest as exc:
                if "not modified" in str(exc):
                    return msg
            try:
                await msg.delete()
            except TelegramBadRequest:
                pass
            chat_id = msg.chat.id
        else:
            chat_id = target.from_user.id
        bot = target.bot
    else:
        chat_id = target.chat.id
        bot = target.bot
    if photo:
        return await bot.send_photo(chat_id, photo, caption=text, reply_markup=markup)
    return await bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)
