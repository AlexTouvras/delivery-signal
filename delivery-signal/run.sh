#!/usr/bin/env bash
# Same command as ./run. Kept so an older path still works.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$here/run" "$@"
