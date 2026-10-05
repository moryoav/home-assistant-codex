#!/bin/sh
# Run inside the worker image: download the documentation with the packaged
# Git, then read it from the Codex sandbox the way a task's shell commands do.
set -eu

python3 - <<'EOF'
import sys

sys.path.insert(0, "/")
import server

info = server.download_ha_docs(server.HA_DOCS_ROOT)
print("Downloaded documentation commit", info["commit"])
EOF

mkdir -p /config /data/codex-home
export CODEX_HOME=/data/codex-home HOME=/data
cd /config
for mode in read-only workspace-write; do
    pages=$(codex sandbox --config "sandbox_mode=\"$mode\"" --config check_for_update_on_startup=false -- \
        /bin/sh -c 'ls /data/ha-docs/source/_integrations | wc -l')
    echo "$mode sandbox: $pages integration pages readable"
    test "$pages" -gt 500
done
