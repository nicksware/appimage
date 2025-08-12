#!/bin/sh
set -euo pipefail
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/../../../AppRun" "$@"
