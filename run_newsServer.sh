#!/usr/bin/env bash
# NewsServer 실행 스크립트 (service_manager 표준)
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"
cd "$DIR"

LOG_DIR="$DIR/data/logs"
PID_DIR="$DIR/.pids"
PID_FILE="$PID_DIR/server.pid"
STDOUT_LOG="$LOG_DIR/stdout.log"

mkdir -p "$LOG_DIR" "$PID_DIR"
say() { echo "[NewsServer] $*"; }

# ─── .env ────────────────────────────────────────────────────────────────────
if [[ ! -f ".env" ]]; then
    cp .env.example .env
    say ".env 없음 — .env.example 에서 복사했습니다. 공시 소스를 쓰려면 자격 증명을 설정하세요."
fi
_HOST="$(grep '^SERVER_HOST=' .env 2>/dev/null | cut -d= -f2-)"; _HOST="${_HOST:-127.0.0.1}"
_PORT="$(grep '^SERVER_PORT=' .env 2>/dev/null | cut -d= -f2-)"; _PORT="${_PORT:-5200}"

# ─── 가상환경 ────────────────────────────────────────────────────────────────
if [[ ! -d ".venv" ]]; then
    say "가상환경 생성 중..."
    PY="$(command -v python3.12 || command -v python3)"
    "$PY" -m venv .venv
fi
source .venv/bin/activate
# pyproject.toml 이 바뀌었을 때만 재설치
if [[ ! -f .venv/.installed || pyproject.toml -nt .venv/.installed ]]; then
    say "패키지 설치 중..."
    pip install -q -e "." && touch .venv/.installed
fi

# ─── 기존 프로세스 종료 ──────────────────────────────────────────────────────
"$DIR/stop_newsServer.sh" >/dev/null 2>&1 || true

# ─── 시작 ────────────────────────────────────────────────────────────────────
say "시작 중 (${_HOST}:${_PORT})..."
uvicorn --factory newsserver.main:app_factory \
    --host "$_HOST" --port "$_PORT" --no-access-log >> "$STDOUT_LOG" 2>&1 &
SERVER_PID=$!
disown $SERVER_PID
echo $SERVER_PID > "$PID_FILE"

# 기동 확인 (최대 20초)
for _ in $(seq 1 20); do
    if curl -sf "http://127.0.0.1:${_PORT}/health" >/dev/null 2>&1; then
        say "시작됨 (PID $SERVER_PID, port $_PORT)"
        say "로그: tail -f $LOG_DIR/server.log"
        exit 0
    fi
    kill -0 "$SERVER_PID" 2>/dev/null || break
    sleep 1
done
say "기동 실패 — $STDOUT_LOG 확인"
tail -20 "$STDOUT_LOG"
exit 1
