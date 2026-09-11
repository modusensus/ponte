# SSH Tunnel Setup — DEPRECATED
#
# This script used to copy your private key from %USERPROFILE%\.ssh into the
# project directory and register the old PowerShell tunnel. Both are gone:
# duplicating a private key into a folder that may be archived, synced or
# shared is a security hazard.
#
# Use the Python CLI instead:
#
#   pipx install .          # install ponte
#   ponte init              # create the config file
#   # edit the printed path: set [ssh] host / user / identity_file
#   ponte test              # verify connectivity
#   ponte install           # register auto-start + crash restart
#
# See ../legacy/README.md for the full mapping.

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "setup.ps1 has been removed (it copied your private key into the project)." -ForegroundColor Yellow
Write-Host ""
Write-Host "Use the CLI instead:" -ForegroundColor Cyan
Write-Host "  ponte init      # create the config file"
Write-Host "  ponte test      # verify SSH connectivity"
Write-Host "  ponte install   # register the auto-start service"
Write-Host ""
Write-Host "See legacy/README.md for details." -ForegroundColor Cyan
Write-Host ""

exit 1
