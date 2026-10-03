import asyncio
import codecs
import posixpath
import re
import shlex
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import asyncssh

REMOTE_DIR = "vpncreator"              # папка в домашней директории пользователя на сервере
REMOTE_DIR_SH = '"$HOME/vpncreator"'   # то же самое для shell-команд

_ESC = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\\\)|\x1b[()][A-Za-z0-9]|\x1b[=>78DEHM]")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean(text: str) -> str:
    """Убирает цветные ANSI-коды и управляющие символы из вывода скриптов."""
    text = _ESC.sub("", text or "")
    # \r перерисовывает строку (прогресс-бары): оставляем только последний вариант строки
    text = "\n".join(line.rstrip("\r").rsplit("\r", 1)[-1] for line in text.split("\n"))
    return _CTRL.sub("", text)


@dataclass
class Creds:
    host: str
    port: int
    username: str
    password: str | None = None
    private_key: str | None = None


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    def tail(self, limit: int = 1500) -> str:
        text = (self.stdout.strip() + "\n" + self.stderr.strip()).strip()
        return text[-limit:]


class SSHError(Exception):
    pass


async def connect(c: Creds) -> asyncssh.SSHClientConnection:
    kwargs: dict = dict(
        host=c.host, port=c.port, username=c.username,
        known_hosts=None, connect_timeout=20, keepalive_interval=30,
    )
    if c.private_key:
        try:
            kwargs["client_keys"] = [asyncssh.import_private_key(c.private_key, c.password or None)]
        except (asyncssh.KeyImportError, ValueError) as exc:
            raise SSHError(f"Не удалось прочитать SSH-ключ: {exc}") from exc
        kwargs["password"] = None
    else:
        kwargs["password"] = c.password
        kwargs["client_keys"] = None
    try:
        return await asyncssh.connect(**kwargs)
    except asyncssh.PermissionDenied as exc:
        raise SSHError("Доступ запрещён: неверный логин, пароль или ключ") from exc
    except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
        raise SSHError(f"Не удалось подключиться: {exc or type(exc).__name__}") from exc


async def run(conn: asyncssh.SSHClientConnection, command: str, timeout: int) -> Result:
    """Выполняет команду в папке со скриптами."""
    full = (
        f"mkdir -p {REMOTE_DIR_SH} && cd {REMOTE_DIR_SH} && "
        f"export DEBIAN_FRONTEND=noninteractive && {command}"
    )
    try:
        res = await asyncio.wait_for(conn.run(f"bash -lc {shlex.quote(full)}", check=False), timeout)
    except asyncio.TimeoutError as exc:
        raise SSHError(f"Команда не завершилась за {timeout} сек.") from exc
    except asyncssh.Error as exc:
        raise SSHError(f"Ошибка SSH: {exc}") from exc
    return Result(res.exit_status if res.exit_status is not None else -1,
                  clean(res.stdout), clean(res.stderr))


async def upload(conn: asyncssh.SSHClientConnection, files: list[dict]) -> None:
    """Загружает файлы ({name, content}) в ~/vpncreator и делает их исполняемыми."""
    await conn.run(f"mkdir -p {REMOTE_DIR_SH}", check=False)
    try:
        async with conn.start_sftp_client() as sftp:
            for f in files:
                path = f"{REMOTE_DIR}/{f['name']}"
                async with sftp.open(path, "wb") as fh:
                    await fh.write(f["content"])
                await sftp.chmod(path, 0o755)
        return
    except (asyncssh.Error, OSError):
        pass  # на сервере может не быть SFTP — грузим через cat
    for f in files:
        path = f"{REMOTE_DIR_SH}/{shlex.quote(f['name'])}"
        res = await conn.run(f"cat > {path} && chmod +x {path}", input=f["content"],
                             encoding=None, check=False)
        if res.exit_status != 0:
            raise SSHError(f"Не удалось загрузить {f['name']}")


def fill(template: str, values: dict[str, str]) -> str:
    """Подставляет {плейсхолдеры} без str.format, чтобы не ломать ${VAR} в bash."""
    for k, v in values.items():
        template = template.replace("{" + k + "}", v)
    return template


# ---------------------------------------------------------------- терминальная сессия

READY = "VPNC_READY$"
ENTER_WORDS = {"enter", "{enter}", "<enter>", "⏎", "↵"}
_ANY_KEY = re.compile(r"any key|любую клавишу|istalan|basyň", re.I)
_LETTER = re.compile(r"[^\W\d_]")


@dataclass
class SessionResult:
    ok: bool
    output: str
    error: str = ""

    def tail(self, limit: int = 1500) -> str:
        return self.output.strip()[-limit:]


def parse_steps(text: str) -> list[str]:
    """Каждая строка — то, что человек напечатал бы в терминале и нажал Enter."""
    return [line.rstrip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


async def session(conn: asyncssh.SSHClientConnection, lines: list[str], timeout: int,
                  on_step: Callable[[int, str], Awaitable[None]] | None = None,
                  idle: float = 1.5, stuck: float = 45.0) -> SessionResult:
    """Открывает настоящий терминал (PTY) с bash и «печатает» строки по одной.

    Каждая строка отправляется, когда терминал ждёт ввода: вывод затих, и последняя
    строка экрана похожа на вопрос (без перевода строки, есть буквы). Так можно отвечать
    на вопросы интерактивных скриптов (read -p, «Press any key» и т.п.).
    Если любая команда завершилась с ошибкой — сессия прерывается (set -e).
    """
    cmd = (
        f"mkdir -p {REMOTE_DIR_SH}; cd {REMOTE_DIR_SH} && "
        f"exec env PS1={shlex.quote(READY + ' ')} PS2='' HISTFILE=/dev/null "
        f"DEBIAN_FRONTEND=noninteractive TERM=xterm bash --norc --noprofile -i"
    )
    try:
        proc = await conn.create_process(cmd, term_type="xterm", term_size=(200, 50), encoding=None)
    except asyncssh.Error as exc:
        raise SSHError(f"Не удалось открыть терминал: {exc}") from exc

    raw: list[str] = []
    state = {"last": time.monotonic(), "closed": False}
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    async def reader():
        try:
            while True:
                data = await proc.stdout.read(65536)
                if not data:
                    break
                raw.append(decoder.decode(data))
                state["last"] = time.monotonic()
        except (asyncssh.Error, OSError):
            pass
        finally:
            state["closed"] = True

    task = asyncio.create_task(reader())
    deadline = time.monotonic() + timeout

    def text() -> str:
        return "".join(raw)

    def screen_tail() -> str:
        t = _ESC.sub("", text()[-2000:])
        if t.endswith("\n"):
            return ""
        return _CTRL.sub("", t.rsplit("\n", 1)[-1].rsplit("\r", 1)[-1]).strip()

    async def wait_input(max_wait: float | None = None) -> str | None:
        """Ждёт, пока терминал запросит ввод. None — процесс завершился."""
        started = time.monotonic()
        while True:
            if state["closed"]:
                return None
            now = time.monotonic()
            if now > deadline:
                raise asyncio.TimeoutError
            if max_wait is not None and now - started > max_wait:
                raise _Stuck(screen_tail())
            if now - state["last"] >= idle:
                tail = screen_tail()
                if tail and _LETTER.search(tail):
                    return tail
            await asyncio.sleep(0.2)

    def result(ok: bool, error: str = "") -> SessionResult:
        return SessionResult(ok, clean(text()), error)

    try:
        tail = await wait_input(max_wait=30)
        if tail is None:
            return result(False, "Терминал закрылся сразу после запуска")
        proc.stdin.write(b"set -e; bind 'set enable-bracketed-paste off' 2>/dev/null; clear\r")

        asked: dict = {}  # последний вопрос, на который ответили, — чтобы заметить повтор

        def repeated(tail: str) -> str | None:
            """Скрипт не принял ответ и спросил то же самое снова → останавливаемся,
            иначе следующие ответы уйдут не на те вопросы (например, в меню скрипта)."""
            if not asked or tail.startswith(READY) or tail != asked["tail"]:
                return None
            if "\n" not in text()[asked["pos"]:]:
                return None  # после ответа ничего не выводилось — это не повтор вопроса
            return (f"Скрипт не принял ответ «{asked['line']}» на вопрос «{tail}» и спросил снова. "
                    f"Остановлено, чтобы не ввести лишнего. Возможно, скрипт уже установлен "
                    f"(задайте «Проверку установки») или вопросы идут в другом порядке")

        for i, line in enumerate(lines, 1):
            tail = await wait_input()
            if tail is None:
                return result(False, f"Сессия завершилась на шаге {i}: «{line}» — предыдущая команда упала")
            if err := repeated(tail):
                return result(False, err)
            if on_step:
                await on_step(i, line)
            asked = {"tail": tail, "line": line, "pos": len(text())}
            if line.strip().lower() in ENTER_WORDS:
                proc.stdin.write(b"\r")
            elif _ANY_KEY.search(tail):
                proc.stdin.write(line.strip()[:1].encode() or b"\r")
            else:
                # Ctrl+U стирает подставленное значение по умолчанию (read -e -i ...)
                proc.stdin.write(b"\x15" + line.encode() + b"\r")
            state["last"] = time.monotonic()

        # Все строки введены — ждём возврата в shell
        while True:
            try:
                tail = await wait_input(max_wait=stuck)
            except _Stuck as exc:
                return result(False, f"Скрипт ждёт ввода, а шаги закончились: «{exc.tail}»")
            if tail is None:
                return result(False, "Сессия завершилась с ошибкой")
            if tail.startswith(READY):
                break
            if err := repeated(tail):
                return result(False, err)
            # вопрос висит дольше stuck — сообщаем
            started = time.monotonic()
            while not state["closed"] and screen_tail() == tail and time.monotonic() - started < stuck:
                await asyncio.sleep(0.5)
            if not state["closed"] and screen_tail() == tail:
                return result(False, f"Скрипт ждёт ввода, а шаги закончились: «{tail}»")
        proc.stdin.write(b"exit\r")
        try:
            await asyncio.wait_for(task, 10)
        except asyncio.TimeoutError:
            pass
        return result(True)
    except asyncio.TimeoutError:
        return result(False, f"Не уложились в {timeout} сек.")
    finally:
        if not state["closed"]:
            proc.close()
        task.cancel()


class _Stuck(Exception):
    def __init__(self, tail: str):
        super().__init__(tail)
        self.tail = tail


async def read_file(conn: asyncssh.SSHClientConnection, path: str, limit: int = 20 * 1024 * 1024) -> bytes:
    """Скачивает файл с сервера (путь абсолютный или от домашней папки, ~/ поддерживается)."""
    rel = path[2:] if path.startswith("~/") else path
    try:
        async with conn.start_sftp_client() as sftp:
            if (await sftp.stat(rel)).size > limit:
                raise SSHError("Файл слишком большой")
            async with sftp.open(rel, "rb") as fh:
                return await fh.read()
    except asyncssh.SFTPNoSuchFile as exc:
        raise SSHError(f"Файл не найден: {path}") from exc
    except (asyncssh.Error, OSError):
        pass
    sh_path = ('"$HOME"/' + shlex.quote(rel)) if not rel.startswith("/") else shlex.quote(rel)
    res = await conn.run(f"cat -- {sh_path}", encoding=None, check=False)
    if res.exit_status != 0:
        raise SSHError(f"Файл не найден: {path}")
    return res.stdout


def basename(path: str) -> str:
    return posixpath.basename(path.rstrip("/")) or "key.txt"
