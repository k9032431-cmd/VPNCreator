import asyncio
import re
import shlex
from dataclasses import dataclass

import asyncssh

REMOTE_DIR = "vpncreator"              # папка в домашней директории пользователя на сервере
REMOTE_DIR_SH = '"$HOME/vpncreator"'   # то же самое для shell-команд

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\r")


def clean(text: str) -> str:
    """Убирает цветные ANSI-коды и \\r из вывода скриптов."""
    return _ANSI.sub("", text or "")


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
    """Загружает файлы скриптов в ~/vpncreator и делает их исполняемыми."""
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
