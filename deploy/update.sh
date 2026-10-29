#!/bin/sh
# Pull the latest code, test it, install it, and restart the service.
#
# Run by ditta-ia-adapter-update.timer (as root), and before every service
# start with --no-restart. New code is tested from a clean export of the
# fetched commit before the checkout moves, so a broken push never reaches
# the running service: the checkout and the installed copy stay on the last
# good commit, and that commit is retried on the next run only after a new
# push.
#
#   update.sh [--no-restart]
#
# Settings, all optional, read from the environment:
#   APP_DIR       the git checkout (default: this script's parent directory)
#   APP_USER      user that owns the checkout and runs the service
#                 (default: the checkout's owner)
#   BRANCH        branch to follow (default: main)
#   SERVICE       systemd unit to restart (default: ditta-ia-adapter.service)

set -eu

APP_DIR=${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}
APP_USER=${APP_USER:-$(stat -c %U "$APP_DIR")}
BRANCH=${BRANCH:-main}
SERVICE=${SERVICE:-ditta-ia-adapter.service}
VENV="$APP_DIR/.venv"
FAILED_MARK="$APP_DIR/.update-failed"
RESTART=yes
[ "${1:-}" = "--no-restart" ] && RESTART=no

log() { echo "update: $*"; }

# Git, pip and the tests run as the service user, never as root.
as_app() {
    if [ "$(id -un)" = "$APP_USER" ]; then
        "$@"
    else
        runuser -u "$APP_USER" -- "$@"
    fi
}

cd "$APP_DIR"
as_app git fetch --quiet origin "$BRANCH"
current=$(as_app git rev-parse HEAD)
latest=$(as_app git rev-parse "origin/$BRANCH")

if [ "$current" = "$latest" ] && [ -x "$VENV/bin/ditta-ia-adapter" ]; then
    exit 0
fi
if [ -f "$FAILED_MARK" ] && [ "$(cat "$FAILED_MARK")" = "$latest" ]; then
    log "skipping $latest: it failed its tests before"
    exit 0
fi

[ -x "$VENV/bin/python" ] || as_app python3 -m venv "$VENV"

# Test the fetched commit in a throwaway export, with its own dependencies.
scratch=$(as_app mktemp -d)
trap 'rm -rf "$scratch"' EXIT
as_app sh -c "git archive '$latest' | tar -x -C '$scratch'"
as_app "$VENV/bin/python" -m pip install --quiet "$scratch[test]"
if ! as_app sh -c "cd '$scratch' && '$VENV/bin/python' -m pytest -q -p no:cacheprovider"; then
    log "tests failed for $latest; staying on $current"
    echo "$latest" | as_app tee "$FAILED_MARK" >/dev/null
    # Put the last good code back in the venv.
    as_app "$VENV/bin/python" -m pip install --quiet "$APP_DIR"
    exit 1
fi

as_app git merge --quiet --ff-only "$latest"
as_app "$VENV/bin/python" -m pip install --quiet "$APP_DIR"
rm -f "$FAILED_MARK"
if [ "$current" = "$latest" ]; then log "installed $latest"; else log "updated $current -> $latest"; fi

if [ "$RESTART" = yes ]; then
    systemctl restart "$SERVICE"
    log "restarted $SERVICE"
fi
