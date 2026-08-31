' Hide the console window of scheduled AI bot tasks.
' Usage: wscript.exe run-hidden.vbs "C:\path\to\script.bat"
' Keeps the process in the interactive session (so night_runner's
' idle/game detection still works) but hides the black console window.
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

If WScript.Arguments.Count < 1 Then
    WScript.Quit 1
End If

target = WScript.Arguments(0)

' Resolve to absolute path if relative.
If Not fso.FileExists(target) Then
    target = fso.GetAbsolutePathName(target)
End If

' Run hidden (0 = hidden window, False = don't wait).
shell.Run Chr(34) & target & Chr(34), 0, False
