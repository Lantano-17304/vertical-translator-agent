#!/usr/bin/env bash
# 一键启动（Linux / macOS / Git Bash）
#   ./start.sh
#   ./start.sh --skip-install
#   ./start.sh --no-browser
#   ./start.sh --stop

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
VENV="$BACKEND/.venv"
ENV_FILE="$ROOT/.env"
ENV_EXAMPLE="$ROOT/.env.example"

info() { printf '\033[36m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
err()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }

port_listening() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | grep -q ":$1 "
  elif command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"$1" -sTCP:LISTEN -P -n >/dev/null 2>&1
  else
    return 1
  fi
}

stop_ports() {
  for port in 8000 3000; do
    if command -v lsof >/dev/null 2>&1; then
      mapfile -t pids < <(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
      for pid in "${pids[@]:-}"; do
        [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
      done
    fi
  done
  info "已尝试停止 8000 / 3000 端口上的进程。"
}

SKIP_INSTALL=0
NO_BROWSER=0
DO_STOP=0
for arg in "$@"; do
  case "$arg" in
    --skip-install|-SkipInstall) SKIP_INSTALL=1 ;;
    --no-browser|-NoBrowser) NO_BROWSER=1 ;;
    --stop|-Stop) DO_STOP=1 ;;
  esac
done

if [[ "$DO_STOP" -eq 1 ]]; then
  stop_ports
  exit 0
fi

echo ""
echo "  垂直领域智能翻译 Agent — 本地启动"
echo "  项目目录: $ROOT"
echo ""

if [[ ! -f "$ENV_FILE" && -f "$ENV_EXAMPLE" ]]; then
  cp "$ENV_EXAMPLE" "$ENV_FILE"
  warn "已从 .env.example 生成 .env，请填写 OPENAI_API_KEY 等配置。"
fi

if ! command -v python3 >/dev/null 2>&1; then
  err "未找到 python3，请先安装 Python 3.10+"
  exit 1
fi
if ! command -v node >/dev/null 2>&1; then
  err "未找到 node，请先安装 Node.js"
  exit 1
fi

if port_listening 8000 || port_listening 3000; then
  warn "端口 8000 或 3000 已被占用。可先运行: ./start.sh --stop"
  read -r -p "仍要尝试启动? (y/N) " ans
  [[ "${ans,,}" == "y" ]] || exit 1
fi

if [[ "$SKIP_INSTALL" -eq 0 ]]; then
  info "[1/3] Python 虚拟环境..."
  [[ -d "$VENV" ]] || python3 -m venv "$VENV"
  info "[2/3] pip install..."
  "$VENV/bin/pip" install -r "$BACKEND/requirements.txt"
  info "[3/3] npm install..."
  (cd "$FRONTEND" && npm install)
  echo ""
else
  [[ -x "$VENV/bin/python" ]] || { err "缺少 $VENV，请先运行 ./start.sh"; exit 1; }
fi

cleanup() {
  info "正在停止服务..."
  [[ -n "${BACKEND_PID:-}" ]] && kill "$BACKEND_PID" 2>/dev/null || true
  [[ -n "${FRONTEND_PID:-}" ]] && kill "$FRONTEND_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

info "启动后端 (8000)..."
(
  cd "$BACKEND"
  exec "$VENV/bin/python" -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
) &
BACKEND_PID=$!

sleep 2
info "启动前端 (3000)..."
(
  cd "$FRONTEND"
  exec npm run dev
) &
FRONTEND_PID=$!

info "等待后端就绪..."
ready=0
for _ in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:8000/" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done

echo ""
if [[ "$ready" -eq 1 ]]; then
  echo "  启动完成"
  echo "  页面:  http://localhost:3000"
  echo "  后端:  http://127.0.0.1:8000"
  echo "  停止:  在本终端按 Ctrl+C"
  echo ""
  [[ "$NO_BROWSER" -eq 1 ]] || {
    if command -v xdg-open >/dev/null 2>&1; then xdg-open "http://localhost:3000"
    elif command -v open >/dev/null 2>&1; then open "http://localhost:3000"
    fi
  }
else
  warn "后端未在 60 秒内响应，请检查上方日志。"
fi

wait
