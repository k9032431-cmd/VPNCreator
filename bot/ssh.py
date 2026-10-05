import asyncio
import codecs
import posixpath
import re
import shlex
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import asyncssh

REMOTE_DIR = "vpncreator"   # папка в домашней директории пользователя SSH (туда грузятся скрипты)
DIR_SH = '"$VPNC_DIR"'       # она же в командах на сервере (переменная задаётся ботом)
# Перед каждой командой: папка считается от имени пользователя SSH (до sudo)
_PREP = 'VPNC_DIR="$HOME/vpncreator"; mkdir -p "$VPNC_DIR" && cd "$VPNC_DIR" || exit 97; '
# Под sudo убираем SUDO_*, чтобы скрипты вели себя как при входе под root
# (иначе, например, OpenVPN-скрипт кладёт .ovpn в /home/<user>, а не в /root)
_ROOT_ENV = "env -u SUDO_USER -u SUDO_UID -u SUDO_GID -u SUDO_COMMAND HOME=/root"
SUDO_PROMPT = "VPNC_SUDO_PASSWORD:"

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
    password: str | None = None        # пароль SSH или пароль от SSH-ключа
    private_key: str | None = None
    sudo_password: str | None = None   # пароль для sudo (если вход не под root)

    @property
    def is_root(self) -> bool:
        return self.username == "root"


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


async def connect(c: Creds) -> "Remote":
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
        return Remote(await asyncssh.connect(**kwargs), c)
    except asyncssh.PermissionDenied as exc:
        raise SSHError("Доступ запрещён: неверный логин, пароль или ключ") from exc
    except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
        raise SSHError(f"Не удалось подключиться: {exc or type(exc).__name__}") from exc


_SUDO_ERRORS = [
    (re.compile(r"not in the sudoers|not allowed to run sudo|may not run sudo", re.I),
     "Пользователю «{user}» запрещён sudo — нужны права администратора или вход под root"),
    (re.compile(r"incorrect password|Sorry, try again|no password was provided", re.I),
     "Неверный пароль для sudo"),
    (re.compile(r"password is required|terminal is required", re.I),
     "sudo требует пароль, а вход настроен по SSH-ключу. Подключите сервер по паролю или под root"),
    (re.compile(r"sudo: (command )?not found|sudo: No such file", re.I),
     "На сервере нет sudo — подключайтесь под root"),
]


class Remote:
    """SSH-подключение к серверу. Если вход не под root — всё выполняется через sudo."""

    def __init__(self, conn: asyncssh.SSHClientConnection, creds: Creds):
        self.conn = conn
        self.creds = creds

    async def __aenter__(self) -> "Remote":
        return self

    async def __aexit__(self, *exc) -> None:
        self.conn.close()
        try:
            await asyncio.wait_for(self.conn.wait_closed(), 10)
        except (asyncio.TimeoutError, asyncssh.Error, OSError):
            pass

    @property
    def is_root(self) -> bool:
        return self.creds.is_root

    def _wrap(self, tail: str, prompt: str = "") -> str:
        """Команда целиком: перейти в папку скриптов и выполнить tail от root."""
        if self.is_root:
            return f'{_PREP}exec env VPNC_DIR="$VPNC_DIR" {tail}'
        flags = "-S" if self.creds.sudo_password else "-n"
        return f'{_PREP}exec sudo {flags} -p {shlex.quote(prompt)} {_ROOT_ENV} VPNC_DIR="$VPNC_DIR" {tail}'

    def _sudo_input(self) -> str | None:
        if self.is_root or not self.creds.sudo_password:
            return None
        return self.creds.sudo_password + "\n"

    def sudo_error(self, text: str) -> str | None:
        if self.is_root:
            return None
        for pattern, message in _SUDO_ERRORS:
            if pattern.search(text or ""):
                return message.format(user=self.creds.username)
        return None

    async def run(self, command: str, timeout: int) -> Result:
        """Выполняет команду (от root) в папке со скриптами и возвращает вывод."""
        inner = f"exec </dev/null; export DEBIAN_FRONTEND=noninteractive; {command}"
        try:
            res = await asyncio.wait_for(
                self.conn.run(self._wrap(f"bash -lc {shlex.quote(inner)}"), input=self._sudo_input(),
                              check=False), timeout)
        except asyncio.TimeoutError as exc:
            raise SSHError(f"Команда не завершилась за {timeout} сек.") from exc
        except asyncssh.Error as exc:
            raise SSHError(f"Ошибка SSH: {exc}") from exc
        result = Result(res.exit_status if res.exit_status is not None else -1,
                        clean(res.stdout), clean(res.stderr))
        if not result.ok and (err := self.sudo_error(result.stderr)):
            raise SSHError(err)
        return result

    async def check_root(self) -> None:
        """Проверяет, что команды выполняются от root (напрямую или через sudo)."""
        res = await self.run("id -u", 30)
        if res.stdout.strip() != "0":
            raise SSHError(f"Не удалось получить права root: {res.tail(300) or 'sudo не сработал'}")

    async def upload(self, files: list[dict]) -> None:
        """Загружает файлы ({name, content}) в ~/vpncreator пользователя SSH и делает их исполняемыми."""
        await self.conn.run('mkdir -p "$HOME/vpncreator"', check=False)
        try:
            async with self.conn.start_sftp_client() as sftp:
                for f in files:
                    path = f"{REMOTE_DIR}/{f['name']}"
                    async with sftp.open(path, "wb") as fh:
                        await fh.write(f["content"])
                    await sftp.chmod(path, 0o755)
            return
        except (asyncssh.Error, OSError):
            pass  # на сервере может не быть SFTP — грузим через cat
        for f in files:
            path = '"$HOME/vpncreator/"' + shlex.quote(f["name"])
            res = await self.conn.run(f"cat > {path} && chmod +x {path}", input=f["content"],
                                      encoding=None, check=False)
            if res.exit_status != 0:
                raise SSHError(f"Не удалось загрузить {f['name']}")

    async def read_file(self, path: str, limit: int = 20 * 1024 * 1024) -> bytes:
        """Скачивает файл с сервера. Путь абсолютный или ~/… (домашняя папка root)."""
        rel = path[2:] if path.startswith("~/") else path
        if self.is_root:
            try:
                async with self.conn.start_sftp_client() as sftp:
                    if (await sftp.stat(rel)).size > limit:
                        raise SSHError("Файл слишком большой")
                    async with sftp.open(rel, "rb") as fh:
                        return await fh.read()
            except asyncssh.SFTPNoSuchFile as exc:
                raise SSHError(f"Файл не найден: {path}") from exc
            except (asyncssh.Error, OSError):
                pass
        sh_path = shlex.quote(rel) if rel.startswith("/") else '"$HOME"/' + shlex.quote(rel)
        inner = f"exec </dev/null; cat -- {sh_path}"
        sudo_in = self._sudo_input()
        res = await self.conn.run(self._wrap(f"bash -c {shlex.quote(inner)}"),
                                  input=sudo_in.encode() if sudo_in else None, encoding=None, check=False)
        if res.exit_status != 0:
            stderr = (res.stderr or b"").decode(errors="replace")
            raise SSHError(self.sudo_error(stderr) or f"Файл не найден: {path}")
        if len(res.stdout) > limit:
            raise SSHError("Файл слишком большой")
        return res.stdout

    async def session(self, lines: list[str], timeout: int,
                      on_step: Callable[[int, str], Awaitable[None]] | None = None,
                      idle: float = 1.5, stuck: float = 45.0) -> "SessionResult":
        return await _session(self, lines, timeout, on_step, idle, stuck)


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


async def _session(remote: Remote, lines: list[str], timeout: int,
                   on_step: Callable[[int, str], Awaitable[None]] | None = None,
                   idle: float = 1.5, stuck: float = 45.0) -> SessionResult:
    """Открывает настоящий терминал (PTY) с bash и «печатает» строки по одной.

    Каждая строка отправляется, когда терминал ждёт ввода: вывод затих, и последняя
    строка экрана похожа на вопрос (без перевода строки, есть буквы). Так можно отвечать
    на вопросы интерактивных скриптов (read -p, «Press any key» и т.п.).
    Если любая команда завершилась с ошибкой — сессия прерывается (set -e).
    """
    cmd = remote._wrap(
        f"env PS1={shlex.quote(READY + ' ')} PS2='' HISTFILE=/dev/null "
        f"DEBIAN_FRONTEND=noninteractive TERM=xterm bash --norc --noprofile -i",
        prompt=SUDO_PROMPT,
    )
    try:
        proc = await remote.conn.create_process(cmd, term_type="xterm", term_size=(200, 50), encoding=None)
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
        if tail is not None and tail.startswith(SUDO_PROMPT):
            # sudo спрашивает пароль — вводим пароль от сервера
            proc.stdin.write(remote.creds.sudo_password.encode() + b"\r")
            state["last"] = time.monotonic()
            started = len(text())
            tail = await wait_input(max_wait=30)
            if tail is not None and SUDO_PROMPT in text()[started:]:
                return result(False, "Неверный пароль для sudo")
        if tail is None:
            out = clean(text())
            return result(False, remote.sudo_error(out) or "Терминал закрылся сразу после запуска")
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


def basename(path: str) -> str:
    return posixpath.basename(path.rstrip("/")) or "key.txt"
