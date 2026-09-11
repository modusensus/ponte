# legacy/ — deprecated pre-Python scripts

These files are the **old** PowerShell / batch / shell implementation that
`ponte` replaced. They are kept only for reference and for people who still
have them wired into a machine. **Do not use them for new setups.**

Reasons they are deprecated:

- Every path is hardcoded (`C:\ssh-tunnel`, `D:\Git\usr\bin\ssh.exe`), so they
  only work on one particular machine.
- The restart loop is a fixed `sleep 10` with no backoff, no jitter, no health
  check, and no remote-port verification.
- `setup.ps1` **copied your private key** from `%USERPROFILE%\.ssh` into the
  project directory — a private key must not be duplicated into a folder that
  gets archived, synced or shared. That logic has been removed.
- They are Windows-only while the tunnel itself is cross-platform.

Use the Python CLI instead:

| Legacy | Replacement |
|--------|-------------|
| `setup.ps1` | `ponte init` then `ponte test` |
| `tunnel.ps1 start` | `ponte start` |
| `tunnel.ps1 stop` | `ponte stop` |
| `tunnel.ps1 status` | `ponte status` |
| `tunnel.ps1 restart` | `ponte restart` |
| `tunnel.ps1 install` | `ponte install` |
| `tunnel.ps1 uninstall` | `ponte uninstall` |
| `tunnel.ps1 log` | `ponte logs -n 100 --follow` |
| `ssh-tunnel.bat` + `ssh-tunnel.vbs` | the OS auto-start service from `ponte install` |
| `fix-wsl-tunnel.sh` | `ponte install` on the Linux side (systemd user unit) |

`fix-wsl-tunnel.sh` also rewrote a **system-wide** unit
(`/etc/systemd/system/autossh-reverse-tunnel.service`) via `sudo`, with a
`User=` field you had to edit by hand — `ponte` installs a per-user unit
instead, which needs no elevation.
