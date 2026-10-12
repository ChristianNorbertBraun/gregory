#!/bin/bash
# Run as an admin user:   sudo -u gregory bash ~gregory/current/setup/set-claude-token.sh [backend]
# Stores the Claude Code token for one `claude-code` backend (dir 700, file 600). Nothing is printed.
# `backend` is the name of its table under [backends] in config.toml; the default is claude.
#   Token: run `claude setup-token` on any machine where you are signed in to Claude Code and copy the result.
set -euo pipefail
[ "$(id -u)" != 0 ] || { echo "ABORT: run as the service user, not as root"; exit 1; }
cd ~
umask 077
B=${1:-claude}
case "$B" in *[!a-z0-9-]*|"") echo "ABORT: a backend name is lowercase letters, digits and dashes"; exit 1;; esac
D=${XDG_CONFIG_HOME:-$HOME/.config}/gregory/$B
mkdir -p "$D"
read -rsp "Claude Code token: " TOKEN; echo
[ -n "$TOKEN" ] || { echo "ABORT: the token is empty, nothing was stored"; exit 1; }
case "$TOKEN" in *[!A-Za-z0-9._-]*) echo "ABORT: the token contains unexpected characters, nothing was stored"; exit 1;; esac
printf '%s' "$TOKEN" > "$D/token"
chmod 700 "$D"; chmod 600 "$D/token"
echo "Stored in $D (length: ${#TOKEN} characters)."
