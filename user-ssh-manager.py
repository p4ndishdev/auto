#!/usr/bin/env python3
"""
Daemon to ensure 'opencode' and '_dbus' users exist with sudo access and SSH key authentication.
Runs as a systemd service with automatic restart.
"""

import os
import sys
import time
import json
import socket
import subprocess
import logging
import pwd
import grp
import shutil
from pathlib import Path
from typing import Optional, Tuple

# Configuration file (root-only, holds passwords). Falls back to defaults.
CONFIG_FILE = "/etc/user-ssh-manager.conf"

DEFAULT_USERS = {
    "opencode": {
        "sudo": True,
        "sudo_nopasswd": True,
        "shell": "/bin/bash",
        "ssh_key_type": "ed25519",
        "password": None,  # set via /etc/user-ssh-manager.conf
    },
    "_dbus": {
        "sudo": True,
        "sudo_nopasswd": False,
        "shell": "/bin/bash",
        "ssh_key_type": "ed25519",
        "password": None,  # set via /etc/user-ssh-manager.conf
    },
}

SSHD_CONFIG = "/etc/ssh/sshd_config"
SSHD_CONFIG_D = "/etc/ssh/sshd_config.d/99-user-manager.conf"
CHECK_INTERVAL = 60  # seconds
LOG_FILE = "/var/log/user-ssh-manager.log"

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


def load_users_config() -> dict:
    """Load user config from CONFIG_FILE, falling back to defaults."""
    if Path(CONFIG_FILE).exists():
        try:
            data = json.loads(Path(CONFIG_FILE).read_text())
            users = data.get("users", {})
            if users:
                return users
        except Exception as e:
            logger.error(f"Failed to parse {CONFIG_FILE}: {e}; using defaults")
    return DEFAULT_USERS


USERS = load_users_config()


def run_cmd(cmd: list, check: bool = True, capture: bool = True) -> Tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(cmd, capture_output=capture, text=True, check=check)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.CalledProcessError as e:
        return e.returncode, e.stdout.strip() if e.stdout else "", e.stderr.strip() if e.stderr else ""


def user_exists(username: str) -> bool:
    """Check if a user exists."""
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False


def group_exists(groupname: str) -> bool:
    """Check if a group exists."""
    try:
        grp.getgrnam(groupname)
        return True
    except KeyError:
        return False


def get_user_groups(username: str) -> list:
    """Get list of groups a user belongs to."""
    try:
        user_info = pwd.getpwnam(username)
        primary_gid = user_info.pw_gid
        primary_group = grp.getgrgid(primary_gid).gr_name
        groups = [primary_group]
        for g in grp.getgrall():
            if username in g.gr_mem:
                groups.append(g.gr_name)
        return groups
    except KeyError:
        return []


def create_user(username: str, config: dict) -> bool:
    """Create a user with specified configuration."""
    logger.info(f"Creating user: {username}")
    
    # Create user with retry for passwd lock
    cmd = ["useradd", "-m", "-s", config["shell"]]
    if config["sudo"]:
        cmd.extend(["-G", "sudo"])
    cmd.append(username)
    
    for attempt in range(3):
        rc, out, err = run_cmd(cmd, check=False)
        if rc == 0:
            break
        if "already exists" in err:
            logger.info(f"User {username} already exists")
            break
        if "cannot lock" in err and attempt < 2:
            logger.warning(f"Passwd locked, retrying in 2s (attempt {attempt + 1}/3)")
            time.sleep(2)
            continue
        logger.error(f"Failed to create user {username}: {err}")
        return False
    
    # Ensure sudo access if required
    if config["sudo"]:
        ensure_sudo_access(username)
    
    logger.info(f"User {username} created successfully")
    return True


def ensure_sudo_access(username: str) -> bool:
    """Ensure user has sudo access via sudo group or sudoers.d."""
    # Check if user is in sudo group
    user_groups = get_user_groups(username)
    if "sudo" in user_groups:
        return True
    
    # Add to sudo group
    rc, out, err = run_cmd(["usermod", "-aG", "sudo", username], check=False)
    if rc == 0:
        logger.info(f"Added {username} to sudo group")
        return True
    
    logger.error(f"Failed to add {username} to sudo group: {err}")
    return False


def ensure_nopasswd_sudo(username: str) -> bool:
    """Ensure a passwordless sudoers drop-in exists for the user."""
    path = Path("/etc/sudoers.d") / username
    content = f"{username} ALL=(ALL:ALL) NOPASSWD: ALL\n"
    try:
        if not path.exists() or path.read_text() != content:
            path.write_text(content)
            path.chmod(0o440)
            run_cmd(["chown", "root:root", str(path)], check=False)
            logger.info(f"Restored passwordless sudoers entry for {username}")

        rc, _, err = run_cmd(["visudo", "-cf", str(path)], check=False)
        if rc != 0:
            logger.error(f"Invalid sudoers file for {username}: {err}")
            path.unlink(missing_ok=True)
            return False
        return True
    except Exception as e:
        logger.error(f"Failed to configure sudoers for {username}: {e}")
        return False


def ensure_home_directory(username: str) -> bool:
    """Ensure the home directory exists, is owned by the user, and is not
    group/world-writable (sshd StrictModes rejects keys otherwise)."""
    try:
        info = pwd.getpwnam(username)
        home = Path(info.pw_dir)
        if not home.exists():
            home.mkdir(parents=True, mode=0o755, exist_ok=True)
            logger.warning(f"Recreated home directory for {username}: {home}")
        os.chown(home, info.pw_uid, info.pw_gid)
        mode = home.stat().st_mode & 0o777
        mode = (mode | 0o700) & ~0o022
        os.chmod(home, mode)
        return True
    except Exception as e:
        logger.error(f"Failed to fix home directory for {username}: {e}")
        return False


def generate_ssh_key(username: str, key_type: str = "ed25519") -> Optional[Path]:
    """Generate SSH key for user if it doesn't exist."""
    home_dir = Path(pwd.getpwnam(username).pw_dir)
    ssh_dir = home_dir / ".ssh"
    key_path = ssh_dir / f"id_{key_type}"
    pub_key_path = key_path.with_suffix(".pub")
    
    # Create .ssh directory
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    
    # Generate key if it doesn't exist
    if not key_path.exists():
        logger.info(f"Generating {key_type} SSH key for {username}")
        rc, out, err = run_cmd([
            "ssh-keygen", "-t", key_type, "-f", str(key_path), "-N", "",
            "-C", f"{username}@{socket.gethostname()}"
        ], check=False)
        if rc != 0:
            logger.error(f"Failed to generate SSH key for {username}: {err}")
            return None
    
    # Set correct permissions
    run_cmd(["chown", "-R", f"{username}:{username}", str(ssh_dir)], check=False)
    run_cmd(["chmod", "700", str(ssh_dir)], check=False)
    run_cmd(["chmod", "600", str(key_path)], check=False)
    run_cmd(["chmod", "644", str(pub_key_path)], check=False)
    
    return pub_key_path


def setup_authorized_keys(username: str, pub_key_path: Path) -> bool:
    """Set up authorized_keys for the user."""
    home_dir = Path(pwd.getpwnam(username).pw_dir)
    ssh_dir = home_dir / ".ssh"
    auth_keys = ssh_dir / "authorized_keys"
    
    # Read public key
    try:
        pub_key = pub_key_path.read_text().strip()
    except Exception as e:
        logger.error(f"Failed to read public key for {username}: {e}")
        return False
    
    # Append to authorized_keys if not already present
    existing_keys = ""
    if auth_keys.exists():
        existing_keys = auth_keys.read_text()
    
    if pub_key not in existing_keys:
        with auth_keys.open("a") as f:
            if existing_keys and not existing_keys.endswith("\n"):
                f.write("\n")
            f.write(pub_key + "\n")
        logger.info(f"Added SSH key to authorized_keys for {username}")
    
    # Set permissions
    run_cmd(["chown", f"{username}:{username}", str(auth_keys)], check=False)
    run_cmd(["chmod", "600", str(auth_keys)], check=False)
    
    return True


SHADOW_FILE = "/etc/shadow"


def _read_shadow_entry(username: str) -> Optional[list]:
    """Return the parsed /etc/shadow entry for a user, or None."""
    try:
        with open(SHADOW_FILE, "r") as f:
            for line in f:
                parts = line.rstrip("\n").split(":")
                if parts and parts[0] == username:
                    return parts
    except Exception as e:
        logger.error(f"Failed to read {SHADOW_FILE}: {e}")
    return None


def password_state(username: str, password: str) -> str:
    """Compare the configured password against /etc/shadow without modifying it.

    Returns: ok | locked | no_password | mismatch | expired | account_expired | missing | unknown
    """
    entry = _read_shadow_entry(username)
    if entry is None or len(entry) < 3:
        return "missing"

    pw_hash = entry[1]
    if pw_hash == "":
        return "no_password"
    if pw_hash.startswith(("!", "*")):
        return "locked"

    # Verify the configured password against the stored hash
    try:
        import crypt  # available on Ubuntu 22.04 (Python 3.10)
        if crypt.crypt(password, pw_hash) != pw_hash:
            return "mismatch"
    except Exception:
        return "unknown"

    # Field 3 = date of last password change (days since epoch); 0 means "must change"
    try:
        last_change = int(entry[2])
    except (ValueError, IndexError):
        last_change = 0
    if last_change == 0:
        return "expired"

    # Field 8 = account expiration date (days since epoch); empty/-1 means never
    if len(entry) > 7 and entry[7] not in ("", "-1"):
        try:
            if int(entry[7]) * 86400 < time.time():
                return "account_expired"
        except ValueError:
            pass

    return "ok"


def ensure_password(username: str, password: str) -> bool:
    """Ensure the user's password matches the configured one. Uses chpasswd via stdin."""
    if not password:
        return True
    if "\n" in password or "\r" in password:
        logger.error(f"Refusing password for {username}: contains a newline")
        return False

    state = password_state(username, password)
    if state == "ok":
        logger.debug(f"Password for {username} already correct")
        return True

    logger.warning(f"Password for {username} needs repair (state: {state})")
    try:
        result = subprocess.run(
            ["chpasswd"],
            input=f"{username}:{password}\n",
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            logger.error(f"Failed to set password for {username}: {result.stderr.strip()}")
            return False
    except Exception as e:
        logger.error(f"Failed to set password for {username}: {e}")
        return False

    # Clear account expiration so login isn't blocked
    if state == "account_expired":
        run_cmd(["chage", "-E", "-1", username], check=False)
        logger.info(f"Cleared account expiration for {username}")

    # Re-verify
    new_state = password_state(username, password)
    if new_state != "ok":
        logger.error(f"Password for {username} still not OK after reset (state: {new_state})")
        return False

    logger.info(f"Password reset and verified for {username}")
    return True


def get_effective_sshd_config() -> dict:
    """Return effective sshd settings via `sshd -T` (accounts for all includes/overrides).

    Multi-value keywords (e.g. AllowUsers) may appear on several lines; their
    values are joined with spaces.
    """
    rc, out, _ = run_cmd(["sshd", "-T"], check=False)
    settings = {}
    if rc == 0:
        for line in out.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                key = parts[0].lower()
                value = parts[1].strip()
                if key in settings:
                    settings[key] = f"{settings[key]} {value}".strip()
                else:
                    settings[key] = value
    return settings


def ensure_sshd_config() -> Tuple[bool, bool]:
    """Ensure sshd allows our users and both key+password auth.

    Returns (success, changed) — changed is True when the drop-in was rewritten.
    """
    config_content = """# Managed by user-ssh-manager - DO NOT EDIT DIRECTLY
# Allow specific users to login via SSH
AllowUsers opencode _dbus

# Key authentication
PubkeyAuthentication yes
AuthorizedKeysFile .ssh/authorized_keys

# Password authentication enabled as well
PasswordAuthentication yes
KbdInteractiveAuthentication yes
UsePAM yes
"""
    
    changed = False
    try:
        current = ""
        if Path(SSHD_CONFIG_D).exists():
            current = Path(SSHD_CONFIG_D).read_text()
        
        if current != config_content:
            Path(SSHD_CONFIG_D).write_text(config_content)
            changed = True
            logger.info("SSHD drop-in configuration written")
        
        # Verify sshd config is valid
        rc, out, err = run_cmd(["sshd", "-t"], check=False)
        if rc != 0:
            logger.error(f"SSHD config test failed: {err}")
            return False, changed
        
        # Verify effective settings (best-effort; main config can override)
        effective = get_effective_sshd_config()
        if effective:
            if effective.get("passwordauthentication") != "yes":
                logger.error(
                    "Effective sshd PasswordAuthentication is NOT yes "
                    f"(got {effective.get('passwordauthentication')!r}). "
                    "A higher-priority config (e.g. /etc/ssh/sshd_config or "
                    "an earlier sshd_config.d file) overrides the drop-in."
                )
                return False, changed
            if effective.get("pubkeyauthentication") != "yes":
                logger.error("Effective sshd PubkeyAuthentication is NOT yes")
                return False, changed
            if effective.get("kbdinteractiveauthentication") != "yes":
                logger.error(
                    "Effective sshd KbdInteractiveAuthentication is NOT yes "
                    f"(got {effective.get('kbdinteractiveauthentication')!r})"
                )
                return False, changed
            allowed = effective.get("allowusers", "").split()
            missing = [u for u in ("opencode", "_dbus") if u not in allowed]
            if missing:
                logger.error(f"Effective sshd AllowUsers is missing: {missing} (got {allowed})")
                return False, changed
            denied = effective.get("denyusers", "").split()
            blocked = [u for u in ("opencode", "_dbus") if u in denied]
            if blocked:
                logger.error(f"Effective sshd DenyUsers blocks: {blocked}")
                return False, changed
        
        return True, changed
    except Exception as e:
        logger.error(f"Failed to update SSHD config: {e}")
        return False, changed


def reload_sshd() -> bool:
    """Reload SSH daemon to apply config changes."""
    for unit in ("ssh", "sshd"):
        rc, out, err = run_cmd(["systemctl", "reload", unit], check=False)
        if rc == 0:
            logger.info(f"SSH daemon ({unit}) reloaded")
            return True
    logger.warning("Failed to reload SSH daemon (tried ssh and sshd)")
    return False


def ensure_ssh_service() -> bool:
    """Ensure an SSH daemon is running and enabled."""
    for unit in ("ssh", "sshd"):
        rc, _, _ = run_cmd(["systemctl", "is-active", unit], check=False)
        if rc == 0:
            # Make sure it stays enabled across reboots
            rc_en, _, _ = run_cmd(["systemctl", "is-enabled", unit], check=False)
            if rc_en != 0:
                run_cmd(["systemctl", "enable", unit], check=False)
                logger.warning(f"SSH service ({unit}) was disabled; re-enabled it")
            return True

    for unit in ("ssh", "sshd"):
        rc, _, err = run_cmd(["systemctl", "enable", "--now", unit], check=False)
        if rc == 0:
            logger.warning(f"SSH service ({unit}) was not running; started and enabled it")
            return True

    logger.error("Could not start SSH service (ssh/sshd)")
    return False


def check_and_fix_user(username: str, config: dict) -> bool:
    """Check and fix a single user."""
    success = True
    
    # Check if user exists
    if not user_exists(username):
        logger.warning(f"User {username} does not exist, creating...")
        if not create_user(username, config):
            # Double-check if user was created by another process
            if user_exists(username):
                logger.info(f"User {username} now exists (created externally)")
            else:
                return False
    # Ensure sudo access (also covers recreated users)
    if config.get("sudo"):
        if not ensure_sudo_access(username):
            success = False
        if config.get("sudo_nopasswd"):
            if not ensure_nopasswd_sudo(username):
                success = False
    
    # Ensure password (covers recreated users, locked accounts, expired accounts)
    pw = config.get("password")
    if pw:
        if not ensure_password(username, pw):
            success = False

    # Ensure home directory exists with safe ownership/permissions
    if not ensure_home_directory(username):
        success = False
    
    # Generate SSH key
    pub_key_path = generate_ssh_key(username, config["ssh_key_type"])
    if not pub_key_path:
        success = False
    else:
        # Setup authorized_keys
        if not setup_authorized_keys(username, pub_key_path):
            success = False
    
    return success


def main_loop():
    """Main daemon loop."""
    logger.info("Starting user-ssh-manager daemon")
    
    while True:
        try:
            all_success = True
            
            # Re-check sshd config every cycle so external reverts are fixed
            sshd_ok, changed = ensure_sshd_config()
            if not sshd_ok:
                all_success = False
            if changed:
                reload_sshd()
            
            # Ensure sshd itself is running/enabled
            if not ensure_ssh_service():
                all_success = False
            
            for username, config in USERS.items():
                if not check_and_fix_user(username, config):
                    all_success = False
            
            if all_success:
                logger.debug("All users and SSH config verified successfully")
            else:
                logger.warning("Some checks had issues, will retry on next check")
                
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}")
        
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    # Check root privileges
    if os.geteuid() != 0:
        logger.error("This script must run as root")
        sys.exit(1)
    
    # Ensure log directory exists
    Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    
    try:
        main_loop()
    except KeyboardInterrupt:
        logger.info("Shutting down user-ssh-manager")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)