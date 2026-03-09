@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "RUNNER="
set "RUNNER_KIND="
set "RC=0"

call :step "Resolve offline Python runtime"
if exist ".venv\Scripts\python.exe" (
    set "RUNNER=%CD%\.venv\Scripts\python.exe"
    set "RUNNER_KIND=path"
)
if defined RUNNER (
    call :info "Using existing venv: !RUNNER!"
    goto :check_runtime
)

for %%P in (py python) do (
    where %%P >NUL 2>&1
    if errorlevel 1 (
        rem not found
    ) else (
        if /I "%%P"=="py" (
            call :probe "py -3"
        ) else (
            call :probe "python"
        )
    )
)
if defined RUNNER goto :check_runtime

call :error "Offline launch requires .venv or a Python with tkinter + pyserial already installed."
call :error "Run setup_and_run_gui.bat once while online, then use this launcher offline."
set "RC=1"
goto :finish

:check_runtime
call :step "Verify required modules"
if /I "%RUNNER_KIND%"=="path" (
    call "%RUNNER%" -c "import tkinter, serial"
) else (
    call %RUNNER% -c "import tkinter, serial"
)
if errorlevel 1 (
    call :error "Selected Python is missing tkinter or pyserial."
    call :error "Prepare the environment online first, then retry offline."
    set "RC=1"
    goto :finish
)

call :step "Launch GUI"
call :info "Starting app.py without any network setup step."
if /I "%RUNNER_KIND%"=="path" (
    call "%RUNNER%" app.py
) else (
    call %RUNNER% app.py
)
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    call :error "GUI exited with code %RC%."
) else (
    call :info "GUI exited normally."
)
goto :finish

:probe
set "CANDIDATE=%~1"
call %CANDIDATE% -c "import tkinter, serial" >NUL 2>&1
if errorlevel 1 (
    exit /b 0
)
set "RUNNER=%CANDIDATE%"
set "RUNNER_KIND=command"
call :info "Using preinstalled Python: %RUNNER%"
exit /b 0

:step
echo.
echo [STEP] %~1
exit /b 0

:info
echo [INFO] %~1
exit /b 0

:error
echo [ERROR] %~1
exit /b 0

:finish
endlocal & exit /b %RC%
