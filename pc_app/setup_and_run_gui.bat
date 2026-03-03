@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

set "VENV_DIR=.venv"
set "PYTHON_EXE="
set "PY_RUNNER="
set "VENV_PY="
set "RC=0"
set "TOTAL_STEPS=6"
set "STEP_NOW=0"

set "PYTHONUTF8=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

call :step "Check Python runtime"
for %%P in (py python) do (
    where %%P >NUL 2>&1
    if not errorlevel 1 if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
)
if not defined PYTHON_EXE (
    call :error "Python not found in PATH. Install Python 3.10+."
    set "RC=1"
    goto :finish
)
if /I "%PYTHON_EXE%"=="py" (
    set "PY_RUNNER=py -3"
) else (
    set "PY_RUNNER=%PYTHON_EXE%"
)
call :info "Python launcher: %PY_RUNNER%"

call :step "Prepare virtual environment"
if not exist "%VENV_DIR%\Scripts\python.exe" (
    call :info "Creating venv: %VENV_DIR%"
    call %PY_RUNNER% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        call :error "Failed to create venv."
        set "RC=1"
        goto :finish
    )
) else (
    call :info "Reusing existing venv: %VENV_DIR%"
)
set "VENV_PY=%CD%\%VENV_DIR%\Scripts\python.exe"

call :step "Upgrade pip"
call "%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 (
    call :error "Failed to upgrade pip."
    set "RC=1"
    goto :finish
)

call :step "Install dependencies"
call "%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 (
    call :error "Failed to install requirements.txt."
    set "RC=1"
    goto :finish
)

if /I "%~1"=="--setup-only" (
    call :step "Setup complete"
    call :info "GUI launch skipped (--setup-only)."
    set "RC=0"
    goto :finish
)

call :step "Launch GUI"
call :info "Starting app.py. This window returns after app exit."
call "%VENV_PY%" app.py
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    call :error "GUI exited with code %RC%."
) else (
    call :info "GUI exited normally."
)

goto :finish

:step
set /a STEP_NOW+=1
echo.
echo [STEP !STEP_NOW!/%TOTAL_STEPS%] %~1
exit /b 0

:info
echo [INFO] %~1
exit /b 0

:error
echo [ERROR] %~1
exit /b 0

:finish
endlocal & exit /b %RC%
