@echo off
rem One-click restart for AgyMonitor only (matches its own pythonw by command line).
set "APP_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%APP_DIR%kill.ps1"
if defined AGY_MONITOR_PYTHONW (
    start "" "%AGY_MONITOR_PYTHONW%" "%APP_DIR%agy_monitor.pyw"
) else (
    where pythonw >nul 2>nul
    if %errorlevel%==0 (
        start "" pythonw "%APP_DIR%agy_monitor.pyw"
    ) else (
        start "" pyw "%APP_DIR%agy_monitor.pyw"
    )
)
