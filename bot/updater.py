"""Обновление бота по кнопке из админ-панели.

Бот работает без root, поэтому сам себя не обновляет: он кладёт файл-заявку
data/update.request, systemd-юнит vpncreator-update.path (ставится install.sh)
запускает `install.sh _auto_update` от root, тот обновляет код, перезапускает бота
и пишет итог в data/update.result. Бот замечает итог и сообщает админу.
"""

import asyncio
import json
import logging
import os
import re
import time
from html import escape
from urllib.parse import quote

import aiohttp
from aiogram import Bot

from .common import db
from .config import config
from .emoji import e
from .ui import BLUE, GREEN, btn, kb

log = logging.getLogger(__name__)

DATA = os.path.dirname(os.path.abspath(config.db_path))
VERSION = os.path.join(DATA, "version.json")
REQUEST = os.path.join(DATA, "update.request")
RUNNING = os.path.join(DATA, "update.running")
RESULT = os.path.join(DATA, "update.result")
LOG = os.path.join(DATA, "update.log")
UNIT = "/etc/systemd/system/vpncreator-update.path"
CHECK_EVERY = 6 * 3600


def local_version() -> dict | None:
    try:
        with open(VERSION, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def supported() -> bool:
    return os.path.exists(UNIT) and local_version() is not None


def in_progress() -> bool:
    return os.path.exists(REQUEST) or os.path.exists(RUNNING)


def _repo_path(url: str) -> str | None:
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url or "")
    return f"{m[1]}/{m[2]}" if m else None


async def check() -> dict:
    """{'latest': sha, 'behind': N, 'commits': [(sha, title), ...]} — что нового на GitHub.

    behind = -1: версия новее есть, но список изменений недоступен (лимит GitHub API и т.п.).
    """
    ver = local_version()
    if not ver:
        raise RuntimeError("Нет data/version.json — выполните на сервере `vpncreator update`")
    try:
        return await _check_api(ver)
    except Exception as api_error:  # noqa: BLE001
        try:
            latest = await _ls_remote(ver)
        except Exception:  # noqa: BLE001
            raise api_error from None
        return {"latest": latest, "behind": 0 if latest == ver["sha"] else -1, "commits": []}


async def _ls_remote(ver: dict) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", "ls-remote", ver["repo"], ver.get("branch") or "HEAD",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await asyncio.wait_for(proc.communicate(), 30)
    if proc.returncode != 0 or not out.split():
        raise RuntimeError("git ls-remote не сработал")
    return out.split()[0].decode()


async def _check_api(ver: dict) -> dict:
    repo = _repo_path(ver.get("repo", ""))
    if not repo:
        raise RuntimeError("Репозиторий не на GitHub — проверка недоступна")
    url = (f"https://api.github.com/repos/{repo}/compare/"
           f"{ver['sha']}...{quote(ver.get('branch') or 'HEAD', safe='/')}")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.get(url, headers={"Accept": "application/vnd.github+json"}) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise RuntimeError(f"GitHub ответил {resp.status}: {data.get('message', '')}")
    commits = [(c["sha"], c["commit"]["message"].splitlines()[0]) for c in data.get("commits", [])]
    latest = commits[-1][0] if commits else ver["sha"]
    return {"latest": latest, "behind": data.get("ahead_by", 0), "commits": commits[::-1]}


def request(chat_id: int, commits: list) -> None:
    os.makedirs(DATA, exist_ok=True)
    with open(REQUEST + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"chat_id": chat_id, "commits": commits, "at": int(time.time())}, f)
    os.replace(REQUEST + ".tmp", REQUEST)  # атомарно, чтобы systemd не увидел пустой файл


def commits_text(commits: list, limit: int = 10) -> str:
    lines = [f"• <code>{sha[:7]}</code> {escape(title)}" for sha, title in commits[:limit]]
    if len(commits) > limit:
        lines.append(f"… и ещё {len(commits) - limit}")
    return "\n".join(lines)


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


async def _report(bot: Bot) -> None:
    """Итог обновления (файл update.result) → сообщение админу, который нажал кнопку."""
    result = _read(RESULT).split()
    if not result:
        return
    try:
        req = json.loads(_read(RUNNING) or "{}")
    except ValueError:
        req = {}
    status, old, new = (result + ["?", "?"])[:3]
    if status == "ok":
        text = (f"{e('ok')} <b>Бот обновлён!</b>\n\n<code>{escape(old)}</code> → <code>{escape(new)}</code>"
                + (f"\n\n<b>Что нового:</b>\n{commits_text(req.get('commits', []))}" if req.get("commits") else ""))
    else:
        tail = re.sub(r"\x1b\[[0-9;]*m", "", _read(LOG))[-1500:]
        text = (f"{e('error')} <b>Обновление не удалось</b>\n\nБот продолжает работать на версии "
                f"<code>{escape(new)}</code>.\n\n<pre>{escape(tail)}</pre>")
    chats = [req["chat_id"]] if req.get("chat_id") else list(set(await db.admin_ids()) | config.admins)
    for chat_id in chats:
        try:
            await bot.send_message(chat_id, text, reply_markup=kb(btn("Открыть меню", "m:home", emoji="home",
                                                                      style=BLUE)))
        except Exception:  # noqa: BLE001
            log.exception("update report failed")
    for path in (RESULT, RUNNING):
        try:
            os.remove(path)
        except OSError:
            pass


async def _notify_new_version(bot: Bot) -> None:
    """Раз в 6 часов: если на GitHub появилась новая версия — сообщить админам (один раз)."""
    if not supported() or in_progress():
        return
    info = await check()
    if not info["behind"] or await db.get("update_notified") == info["latest"]:
        return
    await db.set("update_notified", info["latest"])
    text = f"{e('refresh')} <b>Доступно обновление бота</b>"
    if info["commits"]:
        text += f" — изменений: {info['behind']}\n\n{commits_text(info['commits'])}"
    markup = kb(btn("Обновить сейчас", "a:upd:go", emoji="ok", style=GREEN),
                btn("Подробнее", "a:upd", emoji="info", style=BLUE))
    for chat_id in set(await db.admin_ids()) | config.admins:
        try:
            await bot.send_message(chat_id, text, reply_markup=markup)
        except Exception:  # noqa: BLE001
            pass


async def watcher(bot: Bot) -> None:
    last_check = time.monotonic() - CHECK_EVERY + 60  # первая проверка через минуту после старта
    while True:
        try:
            if os.path.exists(RESULT):
                await _report(bot)
            if time.monotonic() - last_check > CHECK_EVERY:
                last_check = time.monotonic()
                await _notify_new_version(bot)
        except Exception:  # noqa: BLE001
            log.exception("updater watcher")
        await asyncio.sleep(5)
