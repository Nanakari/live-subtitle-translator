Option Explicit

' Double-click this file to launch the desktop subtitle window without a
' visible command prompt. The batch file still performs its normal setup,
' while this launcher hides its console host.
Dim shell, scriptFolder, batchPath
Set shell = CreateObject("WScript.Shell")
scriptFolder = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
batchPath = scriptFolder & "\start.bat"

shell.Environment("PROCESS")("GEMINI_TRANSLATOR_HIDDEN") = "1"
shell.Run "cmd.exe /c " & Chr(34) & batchPath & Chr(34), 0, False
