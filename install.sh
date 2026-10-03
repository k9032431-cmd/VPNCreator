#!/usr/bin/env bash
# VPN Creator — установка Telegram-бота одной командой.
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/k9032431-cmd/VPNCreator/HEAD/install.sh)
#
# Без вопросов (всё через переменные):
#   BOT_TOKEN=123:ABC ADMIN_IDS=111111111 bash <(curl -fsSL .../install.sh)
#
# После установки управление ботом: команда `vpncreator` (status, logs, restart, update, config, uninstall).

set -Eeuo pipefail

REPO_URL="${REPO_URL:-https://github.com/k9032431-cmd/VPNCreator.git}"
BRANCH="${BRANCH:-}"                       # пусто = ветка по умолчанию в репозитории
# Если запущено как установленная команда `vpncreator` — берём папку, где лежит скрипт
_self="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || true)"
if [[ -z "${INSTALL_DIR:-}" && -f "$_self" && -d "$(dirname "$_self")/bot" ]]; then
  INSTALL_DIR="$(dirname "$_self")"
fi
INSTALL_DIR="${INSTALL_DIR:-/opt/VPNCreator}"
SERVICE="vpncreator"
SERVICE_USER="vpncreator"
CLI="/usr/local/bin/vpncreator"
MIN_PY="3.10"

if [[ -t 1 ]]; then
  G=$'\e[32m'; R=$'\e[31m'; Y=$'\e[33m'; B=$'\e[34m'; W=$'\e[1m'; N=$'\e[0m'
else
  G=""; R=""; Y=""; B=""; W=""; N=""
fi
step() { echo -e "\n${B}${W}==>${N} ${W}$*${N}"; }
ok()   { echo -e "  ${G}✔${N} $*"; }
warn() { echo -e "  ${Y}!${N} $*"; }
die()  { echo -e "\n${R}✘ $*${N}" >&2; exit 1; }
trap 'die "Ошибка в строке $LINENO. Установка прервана."' ERR

# Вопросы читаем из терминала, даже если скрипт пришёл через curl | bash
ask() {  # ask "Вопрос" [по умолчанию]
  local prompt="$1" def="${2:-}" ans=""
  [[ -r /dev/tty ]] || die "Нет терминала для ввода. Передайте BOT_TOKEN и ADMIN_IDS переменными."
  if [[ -n "$def" ]]; then prompt="$prompt [${def}]"; fi
  read -r -p "  ${W}?${N} ${prompt}: " ans < /dev/tty || true
  echo "${ans:-$def}"
}

need_root() { [[ $EUID -eq 0 ]] || die "Запустите от root (sudo -i, затем команду ещё раз)."; }

env_get() { [[ -f "$INSTALL_DIR/.env" ]] && grep -E "^$1=" "$INSTALL_DIR/.env" | tail -1 | cut -d= -f2- || true; }

# ---------------------------------------------------------------- шаги установки

install_packages() {
  step "Системные пакеты"
  command -v apt-get >/dev/null || die "Поддерживаются Ubuntu/Debian (нужен apt-get)."
  local pkgs=() p
  for p in git curl ca-certificates python3 python3-venv python3-pip openssl; do
    dpkg -s "$p" >/dev/null 2>&1 || pkgs+=("$p")
  done
  if ((${#pkgs[@]})); then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq "${pkgs[@]}" >/dev/null
    ok "Установлено: ${pkgs[*]}"
  else
    ok "Всё нужное уже есть"
  fi
}

fetch_code() {
  step "Код бота → $INSTALL_DIR"
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    git -C "$INSTALL_DIR" fetch -q --depth 1 origin ${BRANCH:+"$BRANCH"}
    git -C "$INSTALL_DIR" reset -q --hard FETCH_HEAD
    ok "Обновлено до $(git -C "$INSTALL_DIR" rev-parse --short HEAD)"
  else
    [[ -e "$INSTALL_DIR" && -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]] && \
      die "$INSTALL_DIR уже существует и не пустой. Удалите его или задайте INSTALL_DIR=..."
    git clone -q --depth 1 ${BRANCH:+--branch "$BRANCH"} "$REPO_URL" "$INSTALL_DIR"
    ok "Скачано $(git -C "$INSTALL_DIR" rev-parse --short HEAD)"
  fi
}

py_ok() { "$1" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; }

setup_venv() {
  step "Python-окружение"
  local venv="$INSTALL_DIR/.venv"
  if [[ -x "$venv/bin/python" ]] && ! py_ok "$venv/bin/python"; then rm -rf "$venv"; fi
  if [[ ! -x "$venv/bin/python" ]]; then
    if py_ok python3; then
      python3 -m venv "$venv"
    else
      # Старая система (например Ubuntu 20.04) — берём свежий Python через uv
      warn "Системный Python $(python3 -V 2>&1 | cut -d' ' -f2) старше $MIN_PY — ставлю Python 3.12 через uv"
      curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null
      UV_PYTHON_INSTALL_DIR=/opt/uv-python /usr/local/bin/uv venv -q --seed --python 3.12 "$venv"
    fi
  fi
  "$venv/bin/python" -m pip install -q --upgrade pip
  "$venv/bin/python" -m pip install -q -r "$INSTALL_DIR/requirements.txt"
  ok "$("$venv/bin/python" -V), зависимости установлены"
}

check_token() {  # 0 — валиден, 1 — неверный, 2 — не удалось проверить
  local resp
  resp=$(curl -s --max-time 10 "https://api.telegram.org/bot$1/getMe" || true)
  if [[ "$resp" == *'"ok":true'* ]]; then
    BOT_USERNAME=$(sed -n 's/.*"username":"\([^"]*\)".*/\1/p' <<<"$resp")
    return 0
  fi
  [[ "$resp" == *'"ok":false'* ]] && return 1
  return 2
}

configure() {
  step "Настройка (.env)"
  local env="$INSTALL_DIR/.env" token admins secret title
  token="${BOT_TOKEN:-$(env_get BOT_TOKEN)}"
  admins="${ADMIN_IDS:-$(env_get ADMIN_IDS)}"
  secret="$(env_get SECRET_KEY)"
  title="${BOT_TITLE:-$(env_get BOT_TITLE)}"

  BOT_USERNAME=""
  while :; do
    if [[ -z "$token" ]]; then
      echo "  Токен бота выдаёт @BotFather (команда /newbot)."
      token=$(ask "Токен бота")
    fi
    if [[ ! "$token" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]]; then
      warn "Это не похоже на токен (формат 123456789:AA...)"; token=""
      [[ -n "${BOT_TOKEN:-}" ]] && die "Неверный BOT_TOKEN"; continue
    fi
    local rc=0; check_token "$token" || rc=$?
    if ((rc == 0)); then ok "Бот найден: @$BOT_USERNAME"; break; fi
    if ((rc == 2)); then warn "Не удалось связаться с Telegram — проверю позже"; break; fi
    warn "Telegram отклонил токен"; token=""
    [[ -n "${BOT_TOKEN:-}" ]] && die "Неверный BOT_TOKEN"
  done

  while [[ ! "$admins" =~ ^[0-9]+([,[:space:]]+[0-9]+)*$ ]]; do
    echo "  Ваш Telegram ID можно узнать у @userinfobot."
    admins=$(ask "Telegram ID админа (несколько — через запятую)")
  done
  admins=$(tr -s ' ,' ',' <<<"$admins" | sed 's/^,//; s/,$//')

  if [[ -z "$secret" ]]; then
    secret=$(openssl rand -hex 32)
    ok "Сгенерирован SECRET_KEY (им шифруются пароли серверов)"
  else
    ok "SECRET_KEY сохранён прежний"
  fi
  [[ -z "$title" ]] && title="VPN Creator"

  umask 077
  cat > "$env" <<EOF
BOT_TOKEN=$token
ADMIN_IDS=$admins
SECRET_KEY=$secret
DB_PATH=data/bot.db
BOT_TITLE=$title
EOF
  umask 022
  ok "Сохранено в $env"
}

setup_service() {
  step "Сервис systemd"
  id "$SERVICE_USER" >/dev/null 2>&1 || \
    useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
  mkdir -p "$INSTALL_DIR/data"
  chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/data"
  chown root:"$SERVICE_USER" "$INSTALL_DIR/.env"
  chmod 640 "$INSTALL_DIR/.env"

  cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=VPN Creator Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python -m bot
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=$INSTALL_DIR/data
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  # Обновление по кнопке из админ-панели: бот (без root) кладёт файл-заявку,
  # systemd видит его и запускает обновление от root.
  cat > "/etc/systemd/system/$SERVICE-update.path" <<EOF
[Unit]
Description=VPN Creator: update request from the bot

[Path]
PathExists=$INSTALL_DIR/data/update.request
Unit=$SERVICE-update.service

[Install]
WantedBy=multi-user.target
EOF
  cat > "/etc/systemd/system/$SERVICE-update.service" <<EOF
[Unit]
Description=VPN Creator: self-update

[Service]
Type=oneshot
ExecStart=/bin/bash $INSTALL_DIR/install.sh _auto_update
TimeoutStartSec=1800
EOF
  write_version
  systemctl daemon-reload
  systemctl enable -q "$SERVICE" "$SERVICE-update.path"
  systemctl restart "$SERVICE-update.path"
  systemctl restart "$SERVICE"
  ok "Сервис $SERVICE запущен и добавлен в автозагрузку"
  ok "Обновление по кнопке в админ-панели включено"

  ln -sf "$INSTALL_DIR/install.sh" "$CLI"
  chmod +x "$INSTALL_DIR/install.sh"
  ok "Команда управления: ${W}vpncreator${N}"
}

write_version() {
  local f="$INSTALL_DIR/data/version.json"
  cat > "$f" <<EOF
{"sha": "$(git -C "$INSTALL_DIR" rev-parse HEAD)",
 "branch": "$(git -C "$INSTALL_DIR" rev-parse --abbrev-ref HEAD)",
 "repo": "$(git -C "$INSTALL_DIR" config --get remote.origin.url)",
 "date": "$(git -C "$INSTALL_DIR" log -1 --format=%cI)"}
EOF
  chown "$SERVICE_USER:$SERVICE_USER" "$f"
  chmod 644 "$f"
}

verify() {
  step "Проверка"
  local _
  for _ in 1 2 3 4 5 6; do
    sleep 2
    if journalctl -u "$SERVICE" -n 50 --no-pager 2>/dev/null | grep -q "Start polling"; then
      ok "Бот работает"
      return 0
    fi
    systemctl is-active -q "$SERVICE" || break
  done
  if systemctl is-active -q "$SERVICE"; then
    warn "Сервис запущен, но подтверждения пока нет. Посмотрите логи: vpncreator logs"
  else
    journalctl -u "$SERVICE" -n 20 --no-pager || true
    die "Бот не запустился (логи выше). Исправьте настройки: vpncreator config"
  fi
}

finish() {
  local link=""
  [[ -n "${BOT_USERNAME:-}" ]] && link=" → https://t.me/$BOT_USERNAME"
  cat <<EOF

${G}${W}✔ Готово!${N}
  Откройте бота в Telegram и нажмите /start${link}

  ${W}Управление:${N}
    vpncreator status     — статус
    vpncreator logs       — логи в реальном времени
    vpncreator restart    — перезапуск
    vpncreator update     — обновить бота из GitHub
    vpncreator config     — изменить токен / админов
    vpncreator backup     — резервная копия базы
    vpncreator uninstall  — удалить

EOF
}

# ---------------------------------------------------------------- команды

cmd_install() {
  need_root
  echo -e "${W}VPN Creator — установка${N}"
  install_packages
  fetch_code
  setup_venv
  configure
  setup_service
  verify
  finish
}

cmd_update() {
  need_root
  [[ -d "$INSTALL_DIR/.git" ]] || die "Бот не установлен в $INSTALL_DIR"
  fetch_code
  setup_venv
  # перезапуск через новую версию скрипта (она могла поменять юнит)
  exec bash "$INSTALL_DIR/install.sh" _post_update
}

cmd_post_update() {
  BOT_TOKEN="" ADMIN_IDS=""
  configure >/dev/null
  setup_service
  verify
  ok "Обновление завершено"
}

cmd_auto_update() {
  # Запускается systemd по заявке бота (см. vpncreator-update.path)
  local data="$INSTALL_DIR/data" old new rc=0
  [[ -f "$data/update.request" ]] || exit 0
  mv -f "$data/update.request" "$data/update.running"
  old=$(git -C "$INSTALL_DIR" rev-parse --short HEAD 2>/dev/null || echo "?")
  bash "$INSTALL_DIR/install.sh" update > "$data/update.log" 2>&1 || rc=$?
  new=$(git -C "$INSTALL_DIR" rev-parse --short HEAD 2>/dev/null || echo "?")
  if ((rc == 0)); then
    echo "ok $old $new" > "$data/update.result"
  else
    echo "fail $old $new" > "$data/update.result"
  fi
  chown "$SERVICE_USER:$SERVICE_USER" "$data/update.result" "$data/update.log" "$data/update.running" 2>/dev/null || true
  chmod 644 "$data/update.result" "$data/update.log"
}

cmd_config() {
  need_root
  [[ -f "$INSTALL_DIR/.env" ]] || die "Бот не установлен"
  echo -e "${W}Текущие настройки${N} (Enter — оставить как есть)"
  local cur_admins cur_title t a n
  cur_admins=$(env_get ADMIN_IDS); cur_title=$(env_get BOT_TITLE)
  t=$(ask "Новый токен бота (Enter — не менять)")
  a=$(ask "ID админов" "$cur_admins")
  n=$(ask "Название бота" "$cur_title")
  BOT_TOKEN="$t" ADMIN_IDS="$a" BOT_TITLE="$n" configure
  chown root:"$SERVICE_USER" "$INSTALL_DIR/.env"; chmod 640 "$INSTALL_DIR/.env"
  systemctl restart "$SERVICE"
  verify
}

cmd_backup() {
  need_root
  local db="$INSTALL_DIR/data/bot.db" out
  [[ -f "$db" ]] || die "База не найдена: $db"
  out="/root/vpncreator-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
  tar -czf "$out" -C "$INSTALL_DIR" data .env
  ok "Копия сохранена: $out"
  warn "В архиве есть .env с SECRET_KEY — храните его в надёжном месте"
}

cmd_uninstall() {
  need_root
  local yn
  yn=$(ask "Удалить бота, базу и все данные? (yes/no)" "no")
  [[ "$yn" == "yes" ]] || { echo "Отменено."; exit 0; }
  systemctl disable --now "$SERVICE" "$SERVICE-update.path" 2>/dev/null || true
  rm -f "/etc/systemd/system/$SERVICE.service" "/etc/systemd/system/$SERVICE-update.path" \
        "/etc/systemd/system/$SERVICE-update.service" "$CLI"
  systemctl daemon-reload
  rm -rf "$INSTALL_DIR"
  userdel "$SERVICE_USER" 2>/dev/null || true
  ok "Бот удалён"
}

usage() {
  cat <<EOF
${W}vpncreator${N} — управление ботом VPN Creator

  install    установить / переустановить
  update     обновить из GitHub
  status     статус сервиса
  logs       логи в реальном времени (Ctrl+C — выход)
  restart    перезапустить
  stop       остановить
  start      запустить
  config     изменить токен / админов / название
  backup     резервная копия базы
  uninstall  удалить бота
EOF
}

case "${1:-install}" in
  install)      cmd_install ;;
  update)       cmd_update ;;
  _post_update) cmd_post_update ;;
  _auto_update) cmd_auto_update ;;
  status)       systemctl status "$SERVICE" --no-pager ;;
  logs)         journalctl -u "$SERVICE" -f -n 100 ;;
  restart)      need_root; systemctl restart "$SERVICE"; ok "Перезапущен" ;;
  stop)         need_root; systemctl stop "$SERVICE"; ok "Остановлен" ;;
  start)        need_root; systemctl start "$SERVICE"; ok "Запущен" ;;
  config)       cmd_config ;;
  backup)       cmd_backup ;;
  uninstall)    cmd_uninstall ;;
  -h|--help|help) usage ;;
  *)            usage; exit 1 ;;
esac
