#!/bin/bash
# DEPRECATED — superseded by `ponte install` on the Linux side, which writes a
# per-user systemd unit (no sudo, no hardcoded User=). This script overwrites a
# system-wide unit via sudo; keep it only as a reference. See ../legacy/README.md.
#
# Fix WSL autossh reverse tunnel - write new service file

cat > /tmp/autossh-reverse-tunnel.service << 'EOF'
[Unit]
Description=AutoSSH Reverse Tunnel to Cloud Server
After=network-online.target ssh.service
Wants=network-online.target

[Service]
Type=simple
User=your_linux_user
ExecStart=/usr/bin/autossh -M 0 \
    -o "ServerAliveInterval=30" \
    -o "ServerAliveCountMax=3" \
    -o "ExitOnForwardFailure=no" \
    -o "StrictHostKeyChecking=accept-new" \
    -o "TCPKeepAlive=yes" \
    -N -R 23335:localhost:22 \
    YOUR_USER@YOUR_SERVER_IP
Restart=always
RestartSec=15
Environment="AUTOSSH_GATETIME=0"
Environment="AUTOSSH_POLL=30"

[Install]
WantedBy=multi-user.target
EOF

sudo cp /tmp/autossh-reverse-tunnel.service /etc/systemd/system/autossh-reverse-tunnel.service
sudo systemctl daemon-reload
sudo systemctl restart autossh-reverse-tunnel
echo "Service restarted"

sleep 3
systemctl status autossh-reverse-tunnel --no-pager | head -15
