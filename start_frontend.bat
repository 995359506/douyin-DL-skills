@echo off
chcp 65001 > nul
cd frontend

:: 检查 node_modules
if not exist "node_modules" (
    echo [INFO] Installing npm packages...
    call npm install
    if errorlevel 1 (
        echo [ERROR] npm install failed!
        pause
        exit /b 1
    )
)

echo [INFO] Starting frontend server...
echo [URL] http://localhost:3000
echo.
call npm run dev

if errorlevel 1 (
    echo.
    echo [ERROR] Frontend failed to start!
    pause
)
