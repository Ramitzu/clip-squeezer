Option Explicit
' Starts Clip Squeezer with no console window.
Dim fso, shell, here, exe, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = here

exe = "pyw.exe"
If shell.Run("cmd /c where pyw.exe", 0, True) <> 0 Then
    If shell.Run("cmd /c where pythonw.exe", 0, True) = 0 Then
        exe = "pythonw.exe"
    Else
        MsgBox "Python was not found." & vbCrLf & vbCrLf & _
               "Install Python 3 from python.org (tick ""Add to PATH""), " & _
               "then run setup.bat.", vbExclamation, "Clip Squeezer"
        WScript.Quit 1
    End If
End If

If exe = "pyw.exe" Then
    cmd = """pyw.exe"" -3 """ & here & "\ClipSqueezer.py"""
Else
    cmd = """pythonw.exe"" """ & here & "\ClipSqueezer.py"""
End If
shell.Run cmd, 0, False
