@echo off
setlocal

echo Publishing self-contained exe...
echo.

cd /d "%~dp0ui"
dotnet publish -c Release -o publish

echo.
echo Done! Exe is at:  ui\publish\bili-7UID-search.exe
echo.

endlocal
