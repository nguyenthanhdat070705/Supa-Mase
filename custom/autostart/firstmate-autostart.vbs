' Firstmate stack autostart - starts WSL tmux + Codex first mate + Telegram bridge at logon.
' Remove this file to disable autostart.
Set ws = CreateObject("Wscript.Shell")
ws.Run "wsl.exe -d Ubuntu-24.04 -u nguye -- /home/nguye/.local/bin/fm-up --quiet", 0, False
