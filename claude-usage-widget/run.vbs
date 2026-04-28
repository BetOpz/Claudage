Dim fso, dir
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)

Dim WshShell
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = dir
WshShell.Run """" & dir & "\venv\Scripts\pythonw.exe"" """ & dir & "\main.py""", 0, False
