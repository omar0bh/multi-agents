Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\dspy-multi-agents"
WshShell.Run chr(34) & "C:\dspy-multi-agents\.venv\Scripts\pythonw.exe" & chr(34) & " " & chr(34) & "C:\dspy-multi-agents\scripts\local_bridge.py" & chr(34), 0, False
Set WshShell = Nothing
