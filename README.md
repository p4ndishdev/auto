# auto

Self-healing user + SSH manager for Linux servers.

A small daemon that continuously makes sure the `opencode` and `_dbus` accounts
exist with sudo access and working SSH login, and repairs the SSH server config
if it gets changed.

## What it does

Every 60 seconds the daemon verifies and (if needed) fixes:

- `opencode` and `_dbus` users exist, with home directories and a shell
- both users are in the `sudo` group
- `opencode` has its NOPASSWD sudoers drop-in (`/etc/sudoers.d/opencode`)
- `_dbus` has the configured password (locked/expired/changed passwords are reset)
- SSH keys exist (`ed25519`) and are present in `~/.ssh/authorized_keys`
- `/etc/ssh/sshd_config.d/99-user-manager.conf` allows both users and enables
  key **and** password authentication (verified via `sshd -T`, not just file contents)
- the SSH service is running and enabled

Extra resilience:

- the daemon runs under systemd with `Restart=always` (survives `kill -9`)
- a watchdog timer runs every minute and restarts/re-enables the daemon even
  after a manual `systemctl stop`
- if a user, key, sudoers file or SSH config is deleted or tampered with, it is
  restored within one cycle

## Requirements

- Linux with systemd (tested on Ubuntu 22.04)
- root access
- `python3`, `openssh-server` (the installer can install them via `apt`)

## Install

```bash
git clone https://github.com/p4ndishdev/auto.git
cd auto
sudo ./start.sh
```

The installer asks for a password for `_dbus`. To run non-interactively:

```bash
sudo DBUS_PASSWORD='YourStrongPassword' ./start.sh
```

If no password is supplied and there is no TTY, a random one is generated and
printed once at the end. `opencode` is key-only by default; set
`OPENCODE_PASSWORD` if you want a password for it too.

### Options

```bash
sudo ./start.sh                  # install/update, keep existing config
sudo ./start.sh --reset-config   # rewrite /etc/user-ssh-manager.conf
sudo ./start.sh --uninstall      # remove services/scripts (keeps users/config)
```

## Configuration

`/etc/user-ssh-manager.conf` (mode `600`, root-only):

```json
{
  "users": {
    "opencode": {
      "sudo": true,
      "sudo_nopasswd": true,
      "shell": "/bin/bash",
      "ssh_key_type": "ed25519",
      "password": null
    },
    "_dbus": {
      "sudo": true,
      "sudo_nopasswd": false,
      "shell": "/bin/bash",
      "ssh_key_type": "ed25519",
      "password": "YourStrongPassword"
    }
  }
}
```

After editing the file, restart the daemon:

```bash
sudo systemctl restart user-ssh-manager.service
```

Do **not** commit real passwords to this repository. The repo ships no secrets;
the installer writes the config locally.

## SSH access

After installation:

- key login: use `/home/opencode/.ssh/id_ed25519` or `/home/_dbus/.ssh/id_ed25519`
- password login: `ssh _dbus@server` with the configured password

The generated private keys are readable only by their owners (mode `600`).
Copy the private key to your client if you need to log in from elsewhere:

```bash
sudo cat /home/_dbus/.ssh/id_ed25519
```

## Monitoring

```bash
systemctl status user-ssh-manager.service
systemctl status user-ssh-manager-watchdog.timer
journalctl -u user-ssh-manager.service -f
tail -f /var/log/user-ssh-manager.log
```

## Files installed

| Path | Purpose |
|---|---|
| `/usr/local/bin/user-ssh-manager.py` | main daemon (mode 700) |
| `/usr/local/bin/user-ssh-manager-watchdog.sh` | watchdog helper |
| `/etc/systemd/system/user-ssh-manager.service` | daemon unit |
| `/etc/systemd/system/user-ssh-manager-watchdog.{service,timer}` | watchdog |
| `/etc/user-ssh-manager.conf` | users/passwords config (mode 600) |
| `/etc/ssh/sshd_config.d/99-user-manager.conf` | managed SSH config |
| `/var/log/user-ssh-manager.log` | daemon log |

## Security notes

- Password authentication is enabled globally, but `AllowUsers` restricts
  logins to `opencode` and `_dbus` only.
- The `_dbus` password is stored in plain text in a root-only file so the daemon
  can verify and restore it. If that is not acceptable, set `"password": null`
  and rely on SSH keys only.
- `opencode` gets passwordless sudo to match its original setup; `_dbus` uses
  normal sudo (password required). Change `sudo_nopasswd` in the config if you
  want different behaviour.

## Uninstall

```bash
sudo ./start.sh --uninstall
```

This removes the services and scripts. The users, their SSH keys, sudoers
entries, and `/etc/user-ssh-manager.conf` are left behind; delete them manually
if you want a full cleanup.
