Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\workspace\pipbot"
WshShell.Run """C:\workspace\pipbot\.venv\Scripts\pythonw.exe"" ""C:\workspace\pipbot\bot.py""", 0, False
