@echo off
setlocal

echo ========================================
echo  bili-7UID-search
echo ========================================
echo.

cd /d "%~dp0ui"

REM Restore NuGet packages on first run
if not exist "obj" (
    echo First run - restoring packages...
    dotnet restore
    echo.
)

echo Launching...
dotnet run

endlocal
