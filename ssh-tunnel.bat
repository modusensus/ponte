@echo off
setlocal

set SSH_EXE=D:\Git\usr\bin\ssh.exe
set SSH_KEY=C:\ssh-tunnel\id_rsa
set SSH_KNOWN_HOSTS=C:\ssh-tunnel\known_hosts
set SSH_SERVER=YOUR_USER@YOUR_SERVER_IP
set SSH_OPTS=-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="%SSH_KNOWN_HOSTS%" -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -o TCPKeepAlive=yes
set LOG_FILE=C:\ssh-tunnel\tunnel.log

:: Forward ports:
::   23334 → localhost:2222 (WSL SSH via portproxy)
::   17897 → localhost:7897 (Windows proxy)
set SSH_FORWARDS=-N -R 23334:localhost:2222 -R 17897:localhost:7897

:loop
echo [%date% %time%] Starting SSH tunnel... >> "%LOG_FILE%"
"%SSH_EXE%" %SSH_OPTS% -i "%SSH_KEY%" %SSH_FORWARDS% %SSH_SERVER% 2>> "%LOG_FILE%"
set EXIT_CODE=%errorlevel%
echo [%date% %time%] SSH exited with code %EXIT_CODE%, restarting in 10s... >> "%LOG_FILE%"
timeout /t 10 /nobreak >nul
goto loop