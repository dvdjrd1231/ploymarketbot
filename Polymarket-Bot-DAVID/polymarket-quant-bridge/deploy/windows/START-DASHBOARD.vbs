' Polymarket Quant Bridge - open the dashboard.
'
' Double-click this file. No black command window appears at all: it launches
' pythonw.exe (the windowed Python) with the console hidden, which is what
' "opens with a double click instead of CMD prompt" actually requires - a .bat
' always flashes a console, however briefly.
'
' If setup has not been run yet it says so plainly instead of failing silently.

Option Explicit
Dim fso, shell, here, root, pyw, py, cfg

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)
root = fso.GetParentFolderName(fso.GetParentFolderName(here))

pyw = root & "\.venv\Scripts\pythonw.exe"
py  = root & "\.venv\Scripts\python.exe"

If Not fso.FileExists(py) Then
  MsgBox "Setup has not been run yet." & vbCrLf & vbCrLf & _
         "Please double-click 1-SETUP.bat first, then try again.", _
         vbExclamation, "Polymarket Quant Bridge"
  WScript.Quit 1
End If

' pythonw runs with no console; fall back to python if it is somehow absent.
If Not fso.FileExists(pyw) Then pyw = py

shell.CurrentDirectory = root
' 0 = hidden window, False = do not wait for it to finish.
shell.Run """" & pyw & """ -m pqb.gui", 0, False
