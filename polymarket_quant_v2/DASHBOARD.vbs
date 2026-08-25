' Polymarket Quant Bridge V3 - live dashboard launcher.
'
' Double-click this file. It starts the local engine, serves the dashboard on
' http://127.0.0.1:8787/ and opens your browser. No command prompt window.
'
' The server binds to loopback only. It is not reachable from the network.
' Close the browser tab and the server keeps running; use STOP-DASHBOARD.bat
' or Task Manager to stop it.
Option Explicit

Dim fso, shell, here, py, cmd, url, i, ok
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
url = "http://127.0.0.1:8787/"

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
         """Add python.exe to PATH"" during setup, then run this again." & _
         vbCrLf & vbCrLf & _
         "Nothing else needs installing - this system uses the Python " & _
         "standard library only.", _
         vbExclamation, "Polymarket Quant Bridge V3"
  WScript.Quit 1
End If

' -- start the engine and server, hidden ------------------------------------
' --no-browser because we open it ourselves once the port is actually up;
' opening first would show a connection error while the engine starts.
cmd = "cmd /c cd /d """ & here & """ && " & py & _
      " -m pqv3 dashboard --no-browser >> ""var\dashboard-server.log"" 2>&1"
shell.Run cmd, 0, False

' -- wait for the port, then open -------------------------------------------
' Startup builds wallet DNA over the tape, so allow up to ~90 seconds.
ok = False
For i = 1 To 45
  WScript.Sleep 2000
  On Error Resume Next
  Dim http
  Set http = CreateObject("MSXML2.XMLHTTP")
  http.Open "GET", url & "healthz", False
  http.Send
  If Err.Number = 0 And http.Status = 200 Then ok = True
  Err.Clear
  On Error GoTo 0
  If ok Then Exit For
Next

If ok Then
  shell.Run url, 1, False
Else
  MsgBox "The dashboard did not start within 90 seconds." & vbCrLf & vbCrLf & _
         "Check var\dashboard-server.log for the reason, or run it in a " & _
         "window to see the output:" & vbCrLf & vbCrLf & _
         "    python -m pqv3 dashboard" & vbCrLf & vbCrLf & _
         "If you have not run INSTALL.bat yet, run that first.", _
         vbExclamation, "Polymarket Quant Bridge V3"
  WScript.Quit 1
End If
