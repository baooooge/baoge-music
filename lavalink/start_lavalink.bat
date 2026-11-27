@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "JAVA_BIN="
where java >nul 2>&1
if %errorlevel%==0 (
    set "JAVA_BIN=java"
) else (
    for /d %%D in ("C:\Program Files\Eclipse Adoptium\jre-21*") do (
        if exist "%%~D\bin\java.exe" set "JAVA_BIN=%%~D\bin\java.exe"
    )
    if "!JAVA_BIN!"=="" if exist "%~dp0jre\bin\java.exe" (
        set "JAVA_BIN=%~dp0jre\bin\java.exe"
    )
)

if "!JAVA_BIN!"=="" (
    echo [ERROR] Java 21+ not found. Please install Eclipse Temurin JRE 21.
    pause
    exit /b 1
)

if not exist "%~dp0Lavalink.jar" (
    echo [ERROR] Lavalink.jar not found in %~dp0
    pause
    exit /b 1
)

echo Starting BaoGe Lavalink 4.2 using "!JAVA_BIN!"...
"!JAVA_BIN!" -Xms128m -Xmx512m -jar "%~dp0Lavalink.jar"
