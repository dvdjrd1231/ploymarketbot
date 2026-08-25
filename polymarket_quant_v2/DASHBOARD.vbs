' Polymarket Quant Bridge - V2 dashboard launcher.
' Double-click this file. It rebuilds the dashboard from the latest reports
' and opens it, with no command prompt window.
Option Explicit

Dim fso, shell, here, py, cmd, rc, htm, msg
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)

' -- find Python ------------------------------------------------------------
py = ""
If shell.Run("cmd /c python --version", 0, True) = 0 Then
  py = "python"
ElseIf shell.Run("cmd /c py --version", 0, True) = 0 Then
  py = "py"
End If

If py = "" Then
  MsgBox "Python was not found." & vbCrLf & vbCrLf & _
         "Install Python 3.11 or newer from https://python.org and tick " & _
         """Add python.exe to PATH"" during setup, then run this again.", _
         vbExclamation, "Polymarket Quant Bridge - V2"
  WScript.Quit 1
End If

' -- rebuild the dashboard --------------------------------------------------
' Hidden, and we wait for it so a stale page is never opened.
cmd = "cmd /c cd /d """ & here & """ && " & py & _
      " -m pqv2 gui --no-open > ""var\dashboard-build.log"" 2>&1"
rc = shell.Run(cmd, 0, True)

htm = here & "\var\dashboard.html"

If rc <> 0 Then
  msg = "Could not build the dashboard."
  If fso.FileExists(here & "\var\dashboard-build.log") Then
    Dim f
    Set f = fso.OpenTextFile(here & "\var\dashboard-build.log", 1)
    msg = msg & vbCrLf & vbCrLf & Left(f.ReadAll, 700)
    f.Close
  End If
  If fso.FileExists(htm) Then
    msg = msg & vbCrLf & vbCrLf & "Opening the previous dashboard instead."
    MsgBox msg, vbExclamation, "Polymarket Quant Bridge - V2"
    shell.Run """" & htm & """", 1, False
  Else
    msg = msg & vbCrLf & vbCrLf & _
          "There are no reports yet. Run INSTALL.bat first - it does the " & _
          "full research pass and then opens this dashboard."
    MsgBox msg, vbExclamation, "Polymarket Quant Bridge - V2"
  End If
  WScript.Quit 1
End If

If Not fso.FileExists(htm) Then
  MsgBox "The dashboard file was not created." & vbCrLf & vbCrLf & _
         "Run INSTALL.bat first.", vbExclamation, _
         "Polymarket Quant Bridge - V2"
  WScript.Quit 1
End If

' -- open it in the default browser -----------------------------------------
shell.Run """" & htm & """", 1, False
