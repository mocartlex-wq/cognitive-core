# Cognitive Core — Windows PowerShell installer
#
# Ставит cogmedia CLI + сохраняет API-key для последующих вызовов.
# Запускать одной командой (от wizard'а /ui/connect):
#
#   $env:COGNITIVE_API_KEY = 'your-key-from-wizard'
#   iwr https://mcp.me-ai.ru/static/install-cogcore.ps1 | iex
#
# Что делает:
#   1. Создаёт ~/.cognitive-core/bin/ и ~/.config/cogcore/
#   2. Скачивает cogmedia (bash-скрипт)
#   3. Создаёт cogmedia.cmd обёртку для PowerShell вызовов
#   4. Сохраняет API-key в $env (User scope) + файл api-key
#   5. Добавляет ~/.cognitive-core/bin в PATH
#   6. Smoke test: GET /health
#
# Требования: PowerShell 5+ или PowerShell Core, наличие bash через WSL
# или Git for Windows (для запуска cogmedia bash-скрипта).

$ErrorActionPreference = 'Stop'

# ─── Config ─────────────────────────────────────────────────────────────
$BaseUrl = 'https://mcp.me-ai.ru'
$BinDir = Join-Path $env:USERPROFILE '.cognitive-core\bin'
$CfgDir = Join-Path $env:USERPROFILE '.config\cogcore'

# ─── 0. Pre-flight ──────────────────────────────────────────────────────
Write-Host "Cognitive Core Windows installer" -ForegroundColor Cyan
Write-Host "=================================" -ForegroundColor Cyan
Write-Host ""

$ApiKey = $env:COGNITIVE_API_KEY
if (-not $ApiKey) {
    Write-Host "❌ Не задан COGNITIVE_API_KEY." -ForegroundColor Red
    Write-Host "   Получить ключ: $BaseUrl/ui/connect"
    Write-Host ""
    Write-Host "   Запустить так:"
    Write-Host "     `$env:COGNITIVE_API_KEY = 'your-key'"
    Write-Host "     iwr $BaseUrl/static/install-cogcore.ps1 | iex"
    exit 1
}

# ─── 1. Создать директории ──────────────────────────────────────────────
Write-Host "[1/6] Создание директорий…"
New-Item -ItemType Directory -Force -Path $BinDir, $CfgDir | Out-Null
Write-Host "      $BinDir"
Write-Host "      $CfgDir"

# ─── 2. Сохранить API-key ───────────────────────────────────────────────
Write-Host "[2/6] Сохранение API-key…"
$KeyPath = Join-Path $CfgDir 'api-key'
Set-Content -Path $KeyPath -Value $ApiKey -NoNewline -Encoding utf8
Write-Host "      $KeyPath"

# ─── 3. Скачать cogmedia bash-скрипт ────────────────────────────────────
Write-Host "[3/6] Загрузка cogmedia…"
$CogMediaPath = Join-Path $BinDir 'cogmedia'
try {
    Invoke-WebRequest -Uri "$BaseUrl/static/cogmedia" -OutFile $CogMediaPath -UseBasicParsing
    Write-Host "      $CogMediaPath"
} catch {
    Write-Host "❌ Ошибка загрузки: $_" -ForegroundColor Red
    exit 1
}

# ─── 4. Wrapper cogmedia.cmd для PowerShell ─────────────────────────────
Write-Host "[4/6] Создание cogmedia.cmd обёртки…"
$WrapperPath = Join-Path $BinDir 'cogmedia.cmd'
$wrapperContent = @"
@echo off
REM Auto-generated wrapper для cogmedia bash-скрипта.
REM Пытается найти bash (Git for Windows / WSL / MSYS2).
setlocal

set BASH=
for %%P in (bash.exe) do (
  set "BASH=%%~`$PATH:P"
  if exist "%%~`$PATH:P" goto :run
)
if exist "C:\Program Files\Git\usr\bin\bash.exe" set "BASH=C:\Program Files\Git\usr\bin\bash.exe"
if exist "C:\Program Files\Git\bin\bash.exe" set "BASH=C:\Program Files\Git\bin\bash.exe"
if exist "C:\msys64\usr\bin\bash.exe" set "BASH=C:\msys64\usr\bin\bash.exe"
if exist "C:\Windows\System32\wsl.exe" set "BASH=wsl.exe bash"

if "%BASH%"=="" (
  echo ERROR: не нашёл bash. Установите Git for Windows или WSL.
  exit /b 1
)

:run
"%BASH%" "%USERPROFILE%\.cognitive-core\bin\cogmedia" %*
"@
Set-Content -Path $WrapperPath -Value $wrapperContent -Encoding ascii
Write-Host "      $WrapperPath"

# ─── 5. PATH + env ──────────────────────────────────────────────────────
Write-Host "[5/6] PATH + env переменные…"
$userPath = [Environment]::GetEnvironmentVariable('PATH', 'User')
if ($userPath -notlike "*$BinDir*") {
    [Environment]::SetEnvironmentVariable('PATH', "$userPath;$BinDir", 'User')
    $env:PATH = "$env:PATH;$BinDir"
    Write-Host "      Добавлен $BinDir в PATH (User scope)"
} else {
    Write-Host "      $BinDir уже в PATH"
}
[Environment]::SetEnvironmentVariable('COGNITIVE_API_KEY', $ApiKey, 'User')
$env:COGNITIVE_API_KEY = $ApiKey
Write-Host "      COGNITIVE_API_KEY → User env"

# ─── 6. Smoke test ──────────────────────────────────────────────────────
Write-Host "[6/6] Smoke test: GET $BaseUrl/health…"
try {
    $resp = Invoke-RestMethod -Uri "$BaseUrl/health" -TimeoutSec 8
    Write-Host "      healthy=$($resp.healthy) version=$($resp.version) L1=$($resp.layers.l1)" -ForegroundColor Green
} catch {
    Write-Host "      ⚠ Health check недоступен: $_" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "✅ Готово!" -ForegroundColor Green
Write-Host ""
Write-Host "Используйте в новом окне PowerShell или cmd:"
Write-Host "  cogmedia C:\path\to\photo.png"
Write-Host "  cogmedia C:\videos\demo.mp4"
Write-Host ""
Write-Host "Документация: $BaseUrl/ui/connect"
