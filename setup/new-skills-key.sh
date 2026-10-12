#!/bin/bash
# Run as an admin user:   sudo -u gregory bash ~gregory/current/setup/new-skills-key.sh
# Creates the key with which Gregory fetches the skills repository and prints its PUBLIC half.
# Add that line to the repository on GitHub: Settings -> Deploy keys -> Add deploy key, WITHOUT write access.
set -euo pipefail
[ "$(id -u)" != 0 ] || { echo "ABORT: run as the service user, not as root"; exit 1; }
umask 077
D=${XDG_CONFIG_HOME:-$HOME/.config}/gregory
mkdir -p "$D"
[ -e "$D/skills-key" ] || ssh-keygen -q -t ed25519 -N "" -C "gregory skills (read-only)" -f "$D/skills-key"
chmod 600 "$D/skills-key"
echo "Public key (not a secret):"
cat "$D/skills-key.pub"
