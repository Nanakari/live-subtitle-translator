Option Explicit

Dim shell, scriptPath
Set shell = CreateObject("WScript.Shell")
scriptPath = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName) & "\start_chrome_bridge.bat"
shell.Run "cmd.exe /c " & Chr(34) & scriptPath & Chr(34), 0, False
