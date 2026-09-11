' DEPRECATED — this launcher belongs to the old batch tunnel.
' Use `ponte install` to register a Scheduled Task instead.
' See ../legacy/README.md.
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """C:\ssh-tunnel\ssh-tunnel.bat""", 0, False
