#!/usr/bin/env bash
#
# Telegram ↔ MAX Bridge — convenience launcher
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

usage() {
    cat <<'EOF'
Telegram ↔ MAX Bridge

Usage: ./bridge.sh <command> [args]

Local commands:
  start                   Run the bridge locally
  setup                   Full setup wizard (credentials + users + bridges)
  setup credentials       Set up Telegram API credentials only
  setup bridges           Configure users and chat bridges
  auth                    Authenticate accounts (reads config.yaml)
  test [pytest args]      Run E2E tests (bridge must be running)

Docker commands:
  docker up               Build image and start in background
  docker down             Stop containers
  docker restart          Restart containers (after config changes)
  docker logs             Follow container logs
  docker status           Show container status
  docker build            Rebuild image without restarting

Examples:
  ./bridge.sh setup               # first-time setup
  ./bridge.sh start               # run locally
  ./bridge.sh docker up           # run in Docker
  ./bridge.sh docker logs         # watch logs
  ./bridge.sh docker restart      # apply config changes
  ./bridge.sh test                # run all E2E tests
  ./bridge.sh test -k T01         # run a specific test case
  ./bridge.sh test -m formatting  # run tests by marker
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

cmd_test() {
    exec python -m pytest tests/e2e/ -v "$@"
}

cmd_docker() {
    local subcmd="${1:-}"
    shift 2>/dev/null || true

    case "$subcmd" in
        up)
            docker compose up -d --build "$@"
            ;;
        down)
            docker compose down "$@"
            ;;
        restart)
            docker compose restart "$@"
            ;;
        logs)
            docker compose logs -f "$@"
            ;;
        status)
            docker compose ps "$@"
            ;;
        build)
            docker compose build "$@"
            ;;
        "")
            echo "Usage: ./bridge.sh docker <up|down|restart|logs|status|build>"
            exit 1
            ;;
        *)
            docker compose "$subcmd" "$@"
            ;;
    esac
}

# ── Main ──────────────────────────────────────────────────────────────────────

command="${1:-}"
shift 2>/dev/null || true

case "$command" in
    start)          cmd_start "$@" ;;
    setup)          cmd_setup "$@" ;;
    auth)           cmd_auth "$@" ;;
    test)           cmd_test "$@" ;;
    docker)         cmd_docker "$@" ;;
    help|-h|--help) usage ;;
    "")             usage; exit 1 ;;
    *)
        echo "Unknown command: $command"
        echo "Run './bridge.sh help' for usage."
        exit 1
        ;;
esac
