#!/usr/bin/env bash
# Container entrypoint for harvester-ops
set -eo pipefail

case "${1:-serve}" in
    serve)
        exec python3 /opt/harvester-ops/web/app.py
        ;;
    shutdown|startup|status)
        action="$1"; shift
        exec /usr/local/bin/harvester-${action}.sh "$@"
        ;;
    sh|bash)
        exec /usr/bin/env bash "$@"
        ;;
    *)
        echo "Usage: docker run harvester-ops {serve|shutdown|startup|status|bash}" >&2
        exit 1
        ;;
esac
