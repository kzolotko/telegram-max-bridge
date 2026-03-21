#!/usr/bin/env bash
#
# Telegram ↔ MAX Bridge — convenience launcher
#
# Usage:
#   ./bridge.sh start                  — run the bridge
#   ./bridge.sh setup [mode]           — interactive setup wizard
#   ./bridge.sh auth                   — authenticate accounts (from config)
#   ./bridge.sh docker <command>       — Docker operations
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

usage() {
    cat <<'EOF'
Telegram ↔ MAX Bridge

Usage: ./bridge.sh <command> [args]

Commands:
  start                   Run the bridge
  setup                   Full setup wizard (credentials + users + bridges)
  setup credentials       Set up Telegram API credentials only
  setup bridges           Configure users and chat bridges
  auth                    Authenticate accounts (reads config.yaml)
  docker build            Build Docker image
  docker up               Start in Docker (detached)
  docker down             Stop Docker containers
  docker logs             Follow Docker logs
  docker restart          Restart Docker containers

Examples:
  ./bridge.sh setup               # first-time setup
  ./bridge.sh start               # run locally
  ./bridge.sh docker up           # run in Docker
EOF
}

cmd_start() {
    exec python -u -m src "$@"
}

cmd_setup() {
    exec python -m src.setup "$@"
}

cmd_auth() {
    exec python -m src.auth "$@"
}

cmd_docker() {
    local subcmd="${1:-}"
    shift 2>/dev/null || true

    case "$subcmd" in
        build)
            docker compose build "$@"
            ;;
        up)
            docker compose up -d --build "$@"
            ;;
        down)
            docker compose down "$@"
            ;;
        logs)
            docker compose logs -f "$@"
            ;;
        restart)
            docker compose restart "$@"
            ;;
        "")
            echo "Usage: ./bridge.sh docker <build|up|down|logs|restart>"
            exit 1
            ;;
        *)
            # Pass through any other docker compose command
            docker compose "$subcmd" "$@"
            ;;
    esac
}

# ── Main ──────────────────────────────────────────────────────────────────────

command="${1:-}"
shift 2>/dev/null || true

case "$command" in
    start)    cmd_start "$@" ;;
    setup)    cmd_setup "$@" ;;
    auth)     cmd_auth "$@" ;;
    docker)   cmd_docker "$@" ;;
    help|-h|--help)  usage ;;
    "")       usage; exit 1 ;;
    *)
        echo "Unknown command: $command"
        echo "Run './bridge.sh help' for usage."
        exit 1
        ;;
esac
