#!/usr/bin/env bash
# Local dev convenience for the Samus Compose stack.
#
# Usage:
#   ./Samus/docker/boot.sh           # build base + workcells, compose up, validate
#   ./Samus/docker/boot.sh --up      # compose up only (skip rebuild)
#   ./Samus/docker/boot.sh --check   # curl healthchecks for each service
#   ./Samus/docker/boot.sh --down    # compose down (named so cleanup is one keystroke)
set -euo pipefail

# Resolve repo root so the script works regardless of cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/Samus/docker/compose/docker-compose.samus.yml"
ENV_FILE="${REPO_ROOT}/Samus/docker/compose/.env"

cd "${REPO_ROOT}"

# All service names, in startup order.
SERVICES=(
    samus-gateway
    samus-prospecting
    samus-leadgen
    samus-scaffold
    samus-fulfillment
    samus-memory
)

# Host-side port map for --check. Only gateway is published to the host.
declare -A SERVICE_HOST_PORTS=(
    [samus-gateway]=8100
)

compose() {
    if [[ -f "${ENV_FILE}" ]]; then
        docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" "$@"
    else
        docker compose -f "${COMPOSE_FILE}" "$@"
    fi
}

cmd_build() {
    echo "[boot] building samus-base + workcell images"
    compose build
}

cmd_up() {
    echo "[boot] bringing the stack up (detached)"
    compose up -d
}

cmd_down() {
    echo "[boot] stopping stack"
    compose down
}

cmd_check() {
    echo "[boot] healthcheck sweep"
    local failed=0
    for svc in "${!SERVICE_HOST_PORTS[@]}"; do
        local port="${SERVICE_HOST_PORTS[${svc}]}"
        local url="http://127.0.0.1:${port}/health"
        printf '  %-22s %s ... ' "${svc}" "${url}"
        if curl --fail --silent --show-error --max-time 5 "${url}" >/dev/null; then
            echo "ok"
        else
            echo "FAIL"
            failed=$((failed + 1))
        fi
    done
    # Inside the samus-internal network the workcells are reachable by name;
    # we exec curl from the gateway container to probe each of them.
    for svc in "${SERVICES[@]}"; do
        if [[ "${svc}" == "samus-gateway" ]]; then
            continue
        fi
        printf '  %-22s via gateway exec ... ' "${svc}"
        if compose exec -T samus-gateway curl --fail --silent --show-error \
                --max-time 5 "http://${svc}:8080/health" >/dev/null; then
            echo "ok"
        else
            echo "FAIL"
            failed=$((failed + 1))
        fi
    done
    if [[ "${failed}" -gt 0 ]]; then
        echo "[boot] ${failed} healthcheck(s) failed"
        exit 1
    fi
    echo "[boot] all healthchecks passed"
}

main() {
    case "${1:-}" in
        "")
            cmd_build
            cmd_up
            cmd_check
            ;;
        --up)
            cmd_up
            ;;
        --down)
            cmd_down
            ;;
        --check)
            cmd_check
            ;;
        -h|--help)
            grep -E '^#( |$)' "${BASH_SOURCE[0]}" | sed -E 's/^# ?//'
            ;;
        *)
            echo "boot.sh: unknown flag '${1}'. Try --help." >&2
            exit 2
            ;;
    esac
}

main "$@"
