' Top-level dashboard launcher. Opens the window with no command prompt.
Option Explicit
Dim fso, shell, here, target
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
target = here & "\polymarket-quant-bridge\deploy\windows\START-DASHBOARD.vbs"
If Not fso.FileExists(target) Then
  MsgBox "Cannot find the program folder." & vbCrLf & vbCrLf & _
         "Unzip the whole download into one folder, keeping " & _
         "polymarket-quant-bridge, ploymarketbot and qc_lean_bridge together.", _
         vbExclamation, "Polymarket Quant Bridge"
  WScript.Quit 1
End If
shell.Run "wscript.exe """ & target & """", 0, False
