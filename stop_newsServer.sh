#!/usr/bin/env bash
# NewsServer 종료 스크립트 (service_manager 표준)
#   1) PID 파일 → SIGTERM → 최대 10초 대기 → SIGKILL
#   2) 포트 LISTEN 폴백 (PID 파일이 유실된 경우)
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"
cd "$DIR"

PID_FILE="$DIR/.pids/server.pid"
SERVER_PORT="$(grep '^SERVER_PORT=' .env 2>/dev/null | cut -d= -f2-)"
SERVER_PORT="${SERVER_PORT:-5200}"
say() { echo "[NewsServer] $*"; }
stopped=0

if [[ -f "$PID_FILE" ]]; then
    PID="$(cat "$PID_FILE" 2>/dev/null)"
    if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
        say "종료 중 (PID $PID)..."
        kill "$PID" 2>/dev/null || true
        for _ in $(seq 1 10); do
            kill -0 "$PID" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$PID" 2>/dev/null; then
            say "SIGTERM 무응답 — 강제 종료 (PID $PID)"
            kill -9 "$PID" 2>/dev/null || true
        fi
        stopped=1
    fi
    rm -f "$PID_FILE"
fi

# -sTCP:LISTEN 필수: 없으면 이 포트에 접속 중인 클라이언트 프로세스까지 잡힌다
PORT_PIDS="$(lsof -ti tcp:"$SERVER_PORT" -sTCP:LISTEN 2>/dev/null || true)"
if [[ -n "$PORT_PIDS" ]]; then
    say "포트 $SERVER_PORT 점유 프로세스 종료: $(echo $PORT_PIDS)"
    echo "$PORT_PIDS" | xargs kill 2>/dev/null || true
    sleep 2
    PORT_PIDS="$(lsof -ti tcp:"$SERVER_PORT" -sTCP:LISTEN 2>/dev/null || true)"
    [[ -n "$PORT_PIDS" ]] && echo "$PORT_PIDS" | xargs kill -9 2>/dev/null || true
    stopped=1
fi

if [[ "$stopped" = "1" ]]; then say "종료 완료"; else say "실행 중인 프로세스가 없습니다"; fi

LEFTOVER="$(lsof -ti tcp:"$SERVER_PORT" -sTCP:LISTEN 2>/dev/null || true)"
if [[ -n "$LEFTOVER" ]]; then
    say "경고 — 포트 $SERVER_PORT 가 아직 점유되어 있습니다: $(echo $LEFTOVER)"
    exit 1
fi
