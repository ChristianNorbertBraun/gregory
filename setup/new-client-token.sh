#!/bin/bash
# Run as an admin user:   sudo -u gregory bash ~gregory/current/setup/new-client-token.sh <client>
# Creates the bearer token for one chat client (file token-<client>, mode 600). The token is NOT printed:
# copy the file's content into the client's configuration yourself. Restart gregory.service afterwards.
set -euo pipefail
[ "$(id -u)" != 0 ] || { echo "ABORT: run as the service user, not as root"; exit 1; }
C=${1:?usage: new-client-token.sh <client>}
case "$C" in *[!a-z0-9-]*) echo "ABORT: a client name is lowercase letters, digits and dashes"; exit 1;; esac
umask 077
D=${XDG_CONFIG_HOME:-$HOME/.config}/gregory
mkdir -p "$D"
[ ! -e "$D/token-$C" ] || { echo "ABORT: $D/token-$C exists; delete it first to make a new one"; exit 1; }
python3 -c "import secrets; print(secrets.token_urlsafe(48), end='')" > "$D/token-$C"
chmod 600 "$D/token-$C"
echo "Created $D/token-$C"
