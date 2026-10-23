#!/usr/bin/env bash
#
# start.sh - one-shot installer for user-ssh-manager.
#
# Installs the daemon, the watchdog, and the systemd units, generates the
# root-only config file (including an optional password for _dbus), then
# starts everything and verifies it.
#
# Usage:
#   sudo ./start.sh
#   sudo DBUS_PASSWORD='your-password' ./start.sh
#   sudo ./start.sh --reset-config
#   sudo ./start.sh --uninstall
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/usr/local/bin"
CONFIG_FILE="/etc/user-ssh-manager.conf"
UNIT_DIR="/etc/systemd/system"

MAIN_SCRIPT="user-ssh-manager.py"
WATCHDOG_SCRIPT="user-ssh-manager-watchdog.sh"
SERVICE="user-ssh-manager.service"
WATCHDOG_SERVICE="user-ssh-manager-watchdog.service"
WATCHDOG_TIMER="user-ssh-manager-watchdog.timer"

RESET_CONFIG=false
UNINSTALL=false

log()  { printf '\033[1;34m[start]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ok]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
user-ssh-manager installer

Usage: sudo ./start.sh [option]

Options:
  (none)          Install or update everything (keeps an existing config).
  --reset-config  Rewrite /etc/user-ssh-manager.conf from env vars/prompts.
  --uninstall     Stop and remove the services and scripts. Users and the
                  config file are left untouched.
  -h, --help      Show this help.

Environment variables (used when the config is created/reset):
  DBUS_PASSWORD      Password for the _dbus account. If empty, you are
                     prompted; in non-interactive mode a random one is
                     generated and printed.
  OPENCODE_PASSWORD  Optional password for the opencode account (unset = key
                     authentication only).

Example:
  sudo DBUS_PASSWORD='S3cure-Passw0rd!' ./start.sh
EOF
}

require_root() {
    [[ "${EUID}" -eq 0 ]] || die "Run as root: sudo ./start.sh"
}

preflight() {
    command -v systemctl >/dev/null 2>&1 || die "systemd is required (systemctl not found)"
    [[ -d /run/systemd/system ]] || warn "systemd does not look active (container?) - service setup may fail"
    [[ -f "${REPO_DIR}/${MAIN_SCRIPT}" ]] || die "Missing ${REPO_DIR}/${MAIN_SCRIPT} - run this from the repo checkout"
}

install_dependencies() {
    local missing=()
    command -v python3 >/dev/null 2>&1 || missing+=(python3)
    command -v sshd >/dev/null 2>&1 || missing+=(openssh-server)

    if (( ${#missing[@]} == 0 )); then
        return 0
    fi

    if command -v apt-get >/dev/null 2>&1; then
        log "Installing dependencies: ${missing[*]}"
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
    else
        command -v python3 >/dev/null 2>&1 || die "python3 is required but not installed"
        warn "openssh-server is not installed; install it with your package manager"
    fi
}

install_files() {
    log "Installing scripts to ${INSTALL_DIR}"
    install -D -m 700 "${REPO_DIR}/${MAIN_SCRIPT}" "${INSTALL_DIR}/${MAIN_SCRIPT}"
    install -D -m 755 "${REPO_DIR}/${WATCHDOG_SCRIPT}" "${INSTALL_DIR}/${WATCHDOG_SCRIPT}"

    log "Installing systemd units to ${UNIT_DIR}"
    install -D -m 644 "${REPO_DIR}/${SERVICE}" "${UNIT_DIR}/${SERVICE}"
    install -D -m 644 "${REPO_DIR}/${WATCHDOG_SERVICE}" "${UNIT_DIR}/${WATCHDOG_SERVICE}"
    install -D -m 644 "${REPO_DIR}/${WATCHDOG_TIMER}" "${UNIT_DIR}/${WATCHDOG_TIMER}"
}

generate_password() {
    python3 -c 'import secrets, string; alphabet = string.ascii_letters + string.digits + "!@#%^*-_=+"; print("".join(secrets.choice(alphabet) for _ in range(24)))'
}

write_config() {
    local dbus_pw="${DBUS_PASSWORD:-}"
    local opencode_pw="${OPENCODE_PASSWORD:-}"
    local generated=0

    if [[ -z "${dbus_pw}" && -t 0 ]]; then
        read -rsp "Password for _dbus (leave empty to generate a random one): " dbus_pw
        echo
    fi

    if [[ "${dbus_pw}" == *$'\n'* || "${dbus_pw}" == *$'\r'* || "${opencode_pw}" == *$'\n'* || "${opencode_pw}" == *$'\r'* ]]; then
        die "Passwords must not contain newlines"
    fi

    if [[ -z "${dbus_pw}" ]]; then
        dbus_pw="$(generate_password)"
        generated=1
    fi

    DBUS_PASSWORD="${dbus_pw}" OPENCODE_PASSWORD="${opencode_pw}" \
        python3 - > "${CONFIG_FILE}" <<'PY'
import json
import os

config = {
    "users": {
        "opencode": {
            "sudo": True,
            "sudo_nopasswd": True,
            "shell": "/bin/bash",
            "ssh_key_type": "ed25519",
            "password": os.environ.get("OPENCODE_PASSWORD") or None,
        },
        "_dbus": {
            "sudo": True,
            "sudo_nopasswd": False,
            "shell": "/bin/bash",
            "ssh_key_type": "ed25519",
            "password": os.environ.get("DBUS_PASSWORD") or None,
        },
    }
}
print(json.dumps(config, indent=2))
PY

    chown root:root "${CONFIG_FILE}"
    chmod 600 "${CONFIG_FILE}"
    ok "Wrote ${CONFIG_FILE}"

    if (( generated )); then
        GENERATED_DBUS_PASSWORD="${dbus_pw}"
    fi
}

ensure_config() {
    if [[ "${RESET_CONFIG}" == true || ! -f "${CONFIG_FILE}" ]]; then
        write_config
    else
        log "Keeping existing ${CONFIG_FILE} (use --reset-config to rewrite)"
    fi
}

start_services() {
    systemctl daemon-reload

    # Make sure SSH itself is running and enabled.
    if systemctl list-unit-files ssh.service >/dev/null 2>&1; then
        systemctl enable --now ssh >/dev/null 2>&1 || true
    elif systemctl list-unit-files sshd.service >/dev/null 2>&1; then
        systemctl enable --now sshd >/dev/null 2>&1 || true
    fi

    systemctl enable "${SERVICE}" >/dev/null
    systemctl restart "${SERVICE}"
    systemctl enable --now "${WATCHDOG_TIMER}" >/dev/null

    ok "Services enabled and started"
}

verify() {
    local waited=0

    while (( waited < 20 )); do
        if systemctl is-active --quiet "${SERVICE}"; then
            break
        fi
        sleep 1
        waited=$((waited + 1))
    done

    if ! systemctl is-active --quiet "${SERVICE}"; then
        journalctl -u "${SERVICE}" -n 40 --no-pager || true
        die "${SERVICE} failed to start"
    fi

    # Give the daemon a moment to create users/keys on first run.
    for _ in $(seq 1 10); do
        if id opencode >/dev/null 2>&1 && id _dbus >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done

    echo
    ok "Installation complete"
    echo
    log "Users:"
    id opencode 2>/dev/null || warn "opencode not created yet (check logs)"
    id _dbus 2>/dev/null || warn "_dbus not created yet (check logs)"
    echo
    log "SSH:"
    if command -v sshd >/dev/null 2>&1; then
        sshd -T 2>/dev/null | grep -E '^(passwordauthentication|pubkeyauthentication|kbdinteractiveauthentication|allowusers)' || true
    fi
    echo
    log "Status:"
    systemctl --no-pager --lines=0 status "${SERVICE}" | sed -n '1,4p' || true
    systemctl --no-pager list-timers "${WATCHDOG_TIMER}" | sed -n '1,3p' || true
    echo
    log "Logs: journalctl -u ${SERVICE} -f"
    if [[ -n "${GENERATED_DBUS_PASSWORD:-}" ]]; then
        echo
        warn "Generated password for _dbus: ${GENERATED_DBUS_PASSWORD}"
        warn "Store it now; it is not written anywhere else."
    fi
}

uninstall() {
    log "Stopping and disabling services"
    systemctl disable --now "${WATCHDOG_TIMER}" >/dev/null 2>&1 || true
    systemctl disable --now "${SERVICE}" >/dev/null 2>&1 || true

    rm -f "${UNIT_DIR}/${SERVICE}" \
          "${UNIT_DIR}/${WATCHDOG_SERVICE}" \
          "${UNIT_DIR}/${WATCHDOG_TIMER}" \
          "${INSTALL_DIR}/${MAIN_SCRIPT}" \
          "${INSTALL_DIR}/${WATCHDOG_SCRIPT}"

    systemctl daemon-reload
    ok "Removed services and scripts"
    warn "Users opencode/_dbus, their SSH keys, sudoers entries and ${CONFIG_FILE} were left in place"
}

main() {
    case "${1:-}" in
        -h|--help) usage; exit 0 ;;
        --uninstall) UNINSTALL=true ;;
        --reset-config) RESET_CONFIG=true ;;
        "") ;;
        *) die "Unknown option: $1 (try --help)" ;;
    esac

    require_root

    if [[ "${UNINSTALL}" == true ]]; then
        uninstall
        exit 0
    fi

    preflight
    install_dependencies
    install_files
    ensure_config
    start_services
    verify
}

main "$@"
