#!/bin/bash
# Watchdog: keeps user-ssh-manager enabled and running, even after manual stop.
set -u

UNIT="user-ssh-manager.service"

if ! systemctl is-enabled --quiet "$UNIT" 2>/dev/null; then
    systemctl enable "$UNIT" >/dev/null 2>&1
    logger -t user-ssh-manager-watchdog "Re-enabled $UNIT"
fi

if ! systemctl is-active --quiet "$UNIT" 2>/dev/null; then
    logger -t user-ssh-manager-watchdog "Restarting $UNIT"
    systemctl start "$UNIT" >/dev/null 2>&1
fi