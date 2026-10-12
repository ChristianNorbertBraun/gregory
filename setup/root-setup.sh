#!/bin/bash
# Run as an admin user: sudo bash setup/root-setup.sh <agent-user> <client> [<client> ...]
#   e.g. sudo bash setup/root-setup.sh hermes merlin
# <agent-user> is the unix user the chat agents run as; each <client> is the name of one of them.
# Creates the unprivileged user `gregory` that runs the service and owns the model token (no sudo, no password,
# no SSH, home 700, linger) and the two shared folders:
#   /srv/agent-skills        written by gregory, readable by the agents: the published skills
#   /srv/handoff/<client>    written by the agents, readable by gregory: files handed over for one request,
#                            emptied automatically after 12 hours
set -euo pipefail
U=gregory
A=${1:?usage: root-setup.sh <agent-user> <client> [<client> ...]}; shift
[ $# -ge 1 ] || { echo "ABORT: name at least one client"; exit 1; }
[ "$(id -u)" = 0 ] || { echo "ABORT: run with sudo"; exit 1; }
id "$A" >/dev/null 2>&1 || { echo "ABORT: there is no user $A"; exit 1; }
for C in "$@"; do case "$C" in *[!a-z0-9-]*|"") echo "ABORT: a client name is lowercase letters, digits and dashes"; exit 1;; esac; done

if ! id "$U" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "Gregory expert service" "$U"
  passwd -l "$U" >/dev/null
fi
chmod 700 "/home/$U"
loginctl enable-linger "$U"

install -d -m 750 -o "$U" -g "$A" /srv/agent-skills
install -d -m 755 -o root -g root /srv/handoff
: > /etc/tmpfiles.d/gregory-handoff.conf
for C in "$@"; do
  install -d -m 750 -o "$A" -g "$U" "/srv/handoff/$C"
  echo "d /srv/handoff/$C 0750 $A $U 12h" >> /etc/tmpfiles.d/gregory-handoff.conf
done
systemd-tmpfiles --create /etc/tmpfiles.d/gregory-handoff.conf

# TEMPORARY, lets the admin (and an assistant working as the admin) act as this user. Does NOT grant root.
# Remove when the setup is finished: sudo rm /etc/sudoers.d/92-admin-as-gregory
T=$(mktemp)
echo "${SUDO_USER:?run with sudo} ALL=($U) NOPASSWD: ALL" > "$T"
visudo -cf "$T"
install -m 440 -o root -g root "$T" "/etc/sudoers.d/92-admin-as-$U"
rm -f "$T"

echo "=== user:";   id "$U"
echo "=== home:";   ls -ld "/home/$U"
echo "=== linger:"; loginctl show-user "$U" -p Linger || true
echo "=== folders:"; ls -ld /srv/agent-skills /srv/handoff /srv/handoff/*
if sudo -u "$U" sudo -n true 2>/dev/null; then echo "FAIL: $U has sudo"; exit 1; else echo "ok: $U has no sudo"; fi
echo "=== ssh AllowUsers (must not list $U):"; sshd -T | grep -i '^allowusers' || true
