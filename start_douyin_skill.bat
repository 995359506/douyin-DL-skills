@echo off
setlocal EnableDelayedExpansion
chcp 65001 > nul
cls

:: ==========================================
::    Douyin Downloader Skill Launcher
:: ==========================================

:: Get script directory and change to it
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

:: Check virtual environment
if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] Virtual environment not found!
    echo Please create it first: python -m venv .venv
    echo.
    pause
    exit /b 1
)

:: Activate virtual environment
call .venv\Scripts\activate
if errorlevel 1 (
    echo [ERROR] Failed to activate virtual environment!
    pause
    exit /b 1
)

:: Check Python
python --version > nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in virtual environment!
    pause
    exit /b 1
)

:: Check if python can run as module
call python -c "import sys; sys.exit(0)" > nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python module execution failed!
    pause
    exit /b 1
)

:: ==========================================
::               MAIN MENU
:: ==========================================
:menu
cls
echo.
echo    ==========================================
echo    ^|     Douyin Downloader Skill Launcher   ^|
echo    ==========================================
echo.
echo    WHAT IS THIS?
echo    This tool helps you download videos from Douyin (TikTok China).
echo    It consists of two parts: a backend service and a web interface.
echo.
echo    ==========================================
echo    ^|              OPTIONS                    ^|
echo    ==========================================
echo.
echo    [1] Start Backend Only (Port 8080)
echo        Use when: You want to use API directly or test the backend
echo        Access:   http://localhost:8080/docs (API documentation)
echo        Note:     No web interface, only API endpoints available
echo.
echo    [2] Start Frontend Only (Port 3000)
echo        Use when: The backend is already running elsewhere
echo        Access:   http://localhost:3000 (Web interface)
echo        Note:     Requires backend to be running on port 8080
echo.
echo    [3] Start Both Backend + Frontend (RECOMMENDED)
echo        Use when: First time setup or standalone usage
echo        Access:   http://localhost:3000 (Full web interface with backend)
echo        Note:     Opens both services automatically
echo.
echo    [4] Check Dependencies
echo        Use when: Verify all required dependencies are installed
echo.
echo    [5] Exit

:: ==========================================
::              USER INPUT
:: ==========================================
echo.
set /p choice="    Enter your choice (1-5): "

if "%choice%"=="1" goto :start_api
if "%choice%"=="2" goto :start_frontend
if "%choice%"=="3" goto :start_both
if "%choice%"=="4" goto :check_deps
if "%choice%"=="5" exit /b 0

echo.
echo    [ERROR] Invalid choice! Please enter 1, 2, 3, 4, or 5.
pause > nul
goto :menu

:: ==========================================
::           CHECK DEPENDENCIES
:: ==========================================
:check_deps
cls
echo.
echo    ==========================================
echo    ^|       Checking Dependencies            ^|
echo    ==========================================
echo.

echo    [1/5] Checking Python packages...
python -c "import fastapi, uvicorn, pydantic, aiosqlite, aiofiles" 2> nul
if errorlevel 1 (
    echo    [MISSING] Installing Python packages...
    pip install fastapi uvicorn pydantic aiosqlite aiofiles
    if errorlevel 1 (
        echo    [ERROR] Failed to install Python packages!
        pause
        goto :menu
    )
) else (
    echo    [OK] Python packages installed
)

echo.
echo    [2/5] Checking F2 framework...
python -c "import f2" 2> nul
if errorlevel 1 (
    echo    [MISSING] F2 framework not found!
    echo    Please install: pip install f2
    pause
    goto :menu
) else (
    echo    [OK] F2 framework installed
)

echo.
echo    [3/5] Checking Node.js...
node --version > nul 2>&1
if errorlevel 1 (
    echo    [MISSING] Node.js not found!
    echo    Please install Node.js from https://nodejs.org/
    pause
    goto :menu
) else (
    for /f "tokens=*" %%a in ('node --version') do echo    [OK] Node.js version: %%a
)

echo.
echo    [4/5] Checking frontend dependencies...
if not exist "frontend\package.json" (
    echo    [ERROR] Frontend directory not found!
    pause
    goto :menu
)

if not exist "frontend\node_modules" (
    echo    [MISSING] Frontend dependencies not installed
    echo    Please run: cd frontend ^&^& npm install
) else (
    echo    [OK] Frontend dependencies installed
)

echo.
echo    [5/5] Checking Playwright browsers...
python -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); p.chromium.launch(); p.stop()" 2> nul
if errorlevel 1 (
    echo    [MISSING] Playwright browsers not installed!
    echo    Please run: playwright install chromium
    echo    Or: python -m playwright install chromium
) else (
    echo    [OK] Playwright browsers installed
)

echo.
echo    ==========================================
echo    ^|    Dependency Check Complete!          ^|
echo    ==========================================
echo.
pause
goto :menu

:: ==========================================
::           KILL PORT PROCESS
:: ==========================================
:kill_port
set "PORT_TO_KILL=%~1"
echo    [Port] Checking port %PORT_TO_KILL%...

:: Check if port is in use
netstat -ano | findstr ":%PORT_TO_KILL%" | findstr "LISTENING" > nul 2>&1
if errorlevel 1 (
    echo    [OK] Port %PORT_TO_KILL% is free
    exit /b 0
)

:: Port is in use, get the PID
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT_TO_KILL%" ^| findstr "LISTENING"') do (
    echo    [INFO] Port %PORT_TO_KILL% is occupied by PID: %%a
    echo    [Action] Stopping process %%a...
    taskkill /F /PID %%a > nul 2>&1
    if errorlevel 1 (
        echo    [WARN] Failed to stop process %%a
    ) else (
        echo    [OK] Process %%a stopped
    )
)

:: Wait a moment for port to be released
timeout /t 2 /nobreak > nul
exit /b 0

:: ==========================================
::           START BACKEND ONLY
:: ==========================================
:start_api
echo.
echo    ==========================================
echo    ^|        Starting Backend Service        ^|
echo    ==========================================
echo.

:: Kill existing processes on port 8080
call :kill_port 8080

echo.
echo    [Step 1] Checking dependencies...

python -c "import fastapi, uvicorn, pydantic" 2> nul
if errorlevel 1 (
    echo    [ERROR] Missing dependencies!
    echo    Installing: pip install fastapi uvicorn pydantic
    pip install fastapi uvicorn pydantic aiosqlite aiofiles
    if errorlevel 1 (
        echo    [ERROR] Installation failed!
        pause
        goto :menu
    )
)

echo    [Step 2] Checking F2 framework...
python -c "import f2" 2> nul
if errorlevel 1 (
    echo    [ERROR] F2 framework not found!
    echo    Please install: pip install f2
    pause
    goto :menu
)

echo    [Step 3] Starting backend server...
echo    [Port]   8080
echo    [Note]   Port 8080 has been cleared
echo    [API]    http://localhost:8080
echo    [Docs]   http://localhost:8080/docs
echo.
echo    [INFO] If this window closes immediately, there might be an error.
echo.
echo    Press Ctrl+C to stop the server
echo.
echo    ==========================================
echo.

python -m skills.douyin_downloader.web --port 8080

if errorlevel 1 (
    echo.
    echo    [ERROR] Backend server failed to start!
    echo    [TIP] Common issues:
    echo          1. Missing dependencies (pip install fastapi uvicorn)
    echo          2. Python module path issues
    echo.
    pause
)

goto :menu

:: ==========================================
::          START FRONTEND ONLY
:: ==========================================
:start_frontend
cls
echo.
echo    ==========================================
echo    ^|       Starting Frontend Service        ^|
echo    ==========================================
echo.

if not exist "frontend\package.json" (
    echo    [ERROR] Frontend directory not found!
    echo    Expected: frontend/package.json
    pause
    goto :menu
)

cd frontend
if errorlevel 1 (
    echo    [ERROR] Failed to change to frontend directory!
    pause
    goto :menu
)

echo    [Step 1] Checking Node.js...
node --version > nul 2>&1
if errorlevel 1 (
    echo    [ERROR] Node.js not found!
    echo    Please install Node.js from https://nodejs.org/
    pause
    cd ..
    goto :menu
)

for /f "tokens=*" %%a in ('node --version') do (
    echo    [OK] Node.js version: %%a
)

echo.
echo    [Step 2] Checking npm packages...
if not exist "node_modules" (
    echo    [INFO] Installing npm packages...
    call npm install
    if errorlevel 1 (
        echo    [ERROR] npm install failed!
        pause
        cd ..
        goto :menu
    )
) else (
    echo    [OK] npm packages already installed
)

echo.
:: Kill existing processes on port 3000
call :kill_port 3000

echo.
echo    [Step 3] Starting frontend server...
echo    [Note]   Port 3000 has been cleared

:: Get port from vite.config.ts or package.json, default to 3000
set "FRONTEND_PORT=3000"
if exist "vite.config.ts" (
    for /f "tokens=*" %%a in ('findstr /r "port.*:" vite.config.ts 2^>nul') do (
        for /f "tokens=2 delims=: " %%b in ("%%a") do (
            set "FRONTEND_PORT=%%b"
            set "FRONTEND_PORT=!FRONTEND_PORT: =!"
        )
    )
)

echo    [Port]   %FRONTEND_PORT%
echo    [URL]    http://localhost:%FRONTEND_PORT%
echo.
echo    [WARNING] Make sure backend is running on port 8080!
echo    [WARNING] Otherwise the web interface won't work properly.
echo.
echo    Press Ctrl+C to stop the server
echo.
echo    ==========================================
echo.

call npm run dev

cd ..
if errorlevel 1 (
    echo.
    echo    [ERROR] Frontend server failed!
    pause
)

goto :menu

:: ==========================================
::          START BOTH SERVICES
:: ==========================================
:start_both
cls
echo.
echo    ==========================================
echo    ^|     Starting Both Services...          ^|
echo    ==========================================
echo.

:: Check dependencies first
echo    [Checking] Backend dependencies...
python -c "import fastapi, uvicorn, pydantic" 2> nul
if errorlevel 1 (
    echo    [INFO] Installing backend dependencies...
    pip install fastapi uvicorn pydantic aiosqlite aiofiles
    if errorlevel 1 (
        echo    [ERROR] Failed to install backend dependencies!
        pause
        goto :menu
    )
)

echo    [Checking] F2 framework...
python -c "import f2" 2> nul
if errorlevel 1 (
    echo    [ERROR] F2 framework not found!
    echo    Please install: pip install f2
    pause
    goto :menu
)

:: Check frontend dependencies
if not exist "frontend\node_modules" (
    echo    [Checking] Frontend dependencies...
    if exist "frontend\package.json" (
        echo    [INFO] Installing frontend dependencies...
        cd frontend
        call npm install
        if errorlevel 1 (
            echo    [ERROR] Failed to install frontend dependencies!
            cd ..
            pause
            goto :menu
        )
        cd ..
    )
)

:: Set temp directory for startup scripts
set "TEMP_DIR=%TEMP%\douyin_skill_%RANDOM%"
mkdir "%TEMP_DIR%" 2> nul

:: Start backend in new window
echo.
echo    [Step 0/2] Clearing ports...
call :kill_port 8080
call :kill_port 3000
echo.
echo    [Step 1/2] Starting backend service...
echo    [Window]   "Douyin Backend"
echo.

:: Create backend startup script in temp directory
(
    echo @echo off
    echo chcp 65001 ^> nul
    echo title Douyin Backend Server
    echo echo ==========================================
    echo echo Backend Server - Port 8080
    echo echo ==========================================
    echo echo.
    echo cd /d "%SCRIPT_DIR%"
    echo call .venv\Scripts\activate
    echo if errorlevel 1 ^(
    echo   echo [ERROR] Failed to activate virtual environment!
    echo   pause
    echo   exit /b 1
    echo ^)
    echo python -m skills.douyin_downloader.web --port 8080
    echo if errorlevel 1 ^(
    echo   echo.
    echo   echo [ERROR] Server crashed with error code %%errorlevel%%
    echo   pause
    echo ^)
) > "%TEMP_DIR%\backend_start.bat"

start "Douyin Backend" cmd /c "%TEMP_DIR%\backend_start.bat"
if errorlevel 1 (
    echo    [ERROR] Failed to start backend window!
    rmdir /s /q "%TEMP_DIR%" 2> nul
    pause
    goto :menu
)

:: Wait for backend to initialize
echo    [Waiting] Backend initializing (5 seconds)...
timeout /t 5 /nobreak > nul

:: Check if backend is running (try multiple times)
echo    [Checking] Verifying backend is running...
set "BACKEND_READY=0"
for /l %%i in (1,1,3) do (
    curl -s http://localhost:8080/ > nul 2>&1
    if not errorlevel 1 (
        set "BACKEND_READY=1"
        goto :backend_ok
    )
    echo    [Retry %%i/3] Backend not ready yet, waiting...
    timeout /t 2 /nobreak > nul
)
:backend_ok

if "%BACKEND_READY%"=="0" (
    echo    [WARNING] Backend might not be ready yet.
    echo    [INFO] Continuing anyway, it may take a few more seconds...
) else (
    echo    [OK] Backend is responding
)

:: Start frontend in new window
echo.
echo    [Step 2/2] Starting frontend service...
echo    [Window]   "Douyin Frontend"
echo.

if exist "frontend\package.json" (
    cd frontend
    
    :: Get frontend port
    set "FRONTEND_PORT=3000"
    
    :: Create frontend startup script in temp directory
    (
        echo @echo off
        echo chcp 65001 ^> nul
        echo title Douyin Frontend Server
        echo echo ==========================================
        echo echo Frontend Server
        echo echo ==========================================
        echo cd /d "%SCRIPT_DIR%\frontend"
        echo npm run dev
        echo if errorlevel 1 ^(
        echo   echo [ERROR] Frontend server crashed!
        echo   pause
        echo ^)
    ) > "%TEMP_DIR%\frontend_start.bat"
    
    start "Douyin Frontend" cmd /c "%TEMP_DIR%\frontend_start.bat"
    if errorlevel 1 (
        echo    [WARNING] Failed to start frontend window!
        cd ..
    ) else (
        cd ..
    )
) else (
    echo    [WARNING] Frontend not found, starting backend only.
)

:: Clean up temp directory after a delay
start /b cmd /c "timeout /t 10 /nobreak > nul && rmdir /s /q ""%TEMP_DIR%"" 2> nul"

:: Display success message
cls
echo.
echo    ==========================================
echo    ^|      Services Started!                 ^|
echo    ==========================================
echo.
echo    [Backend]   http://localhost:8080  (API Server)
echo    [Frontend]  http://localhost:3000  (Web Interface)
echo.
echo    [IMPORTANT] Wait 10-15 seconds for services to fully start
echo.
echo    [Next Step] Open your browser and visit:
echo                http://localhost:3000
echo.
echo    [Note] Two command windows are now running.
echo           Do not close them while using the application.
echo.
echo    [To Stop] Press Ctrl+C in each window or close them
echo.
echo    ==========================================
echo.
pause

goto :menu

:: ==========================================
::              EXIT
:: ==========================================
:end
cls
echo.
echo    Thank you for using Douyin Downloader!
echo.
echo    Press any key to close this window...
pause > nul
exit /b 0
