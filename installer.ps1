#!/usr/bin/env pwsh
# Cognitive Core — universal one-click installer для Windows.
# Multi-client: Claude Desktop, Cherry Studio, Cursor, Claude Code.
#
# Usage:
#   cd <project>
#   .\installer.ps1
#
# Flow:
#   1. Docker check
#   2. .env (DeepSeek key prompt)
#   3. docker compose up -d
#   4. Wait for healthy
#   5. Auto-detect installed AI clients
#   6. For each installed: configure MCP automatically OR copy to clipboard + open client

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot

function Write-Step { param($msg) Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-OK { param($msg) Write-Host "    OK: $msg" -ForegroundColor Green }
function Write-Warn { param($msg) Write-Host "    !  $msg" -ForegroundColor Yellow }
function Write-Err { param($msg) Write-Host "    FAIL: $msg" -ForegroundColor Red }

Set-Location $projectRoot

# ===== Step 1: Docker =====
Write-Step "1/5 Docker Desktop"
# Native commands: suppress stderr at OS level via cmd, not via PS *>&1
# (PS 5.1 wraps stderr lines in ErrorRecord causing false NativeCommandError)
$dockerVer = & cmd /c "docker --version 2>nul"
if ($LASTEXITCODE -ne 0 -or -not $dockerVer) {
    Write-Err "Docker not found in PATH"
    Write-Host "Install: https://www.docker.com/products/docker-desktop/" -ForegroundColor Yellow
    exit 1
}
Write-OK $dockerVer
& cmd /c "docker info --format `"{{.ServerVersion}}`" >nul 2>nul"
if ($LASTEXITCODE -ne 0) {
    Write-Err "Docker Desktop installed but daemon not running"
    Write-Host "Start Docker Desktop and re-run installer." -ForegroundColor Yellow
    exit 1
}

# ===== Step 2: .env =====
Write-Step "2/5 Environment configuration"
$envPath = Join-Path $projectRoot ".env"
if (-not (Test-Path $envPath)) {
    Copy-Item ".env.example" $envPath
    $apiKey = Read-Host "Enter your DeepSeek API key (sk-...) or Enter to skip"
    if ($apiKey -and $apiKey.StartsWith("sk-")) {
        (Get-Content $envPath -Raw) -replace 'DEEPSEEK_API_KEY=sk-[a-zA-Z0-9-]+', "DEEPSEEK_API_KEY=$apiKey" | Set-Content -Path $envPath -NoNewline
        Write-OK ".env updated"
    } else {
        Write-Warn "DeepSeek key skipped - edit .env manually later"
    }
} else { Write-OK ".env exists" }

# ===== Step 3: docker compose up =====
Write-Step "3/5 Starting Docker stack (this may take 1-2 min on first run)"
# Через cmd чтобы PS не оборачивал stderr-progress в NativeCommandError
& cmd /c "docker compose up -d --build >nul 2>nul"
if ($LASTEXITCODE -ne 0) {
    Write-Err "docker compose up failed (exit code $LASTEXITCODE)"
    & cmd /c "docker compose logs --tail 20 api"
    exit 1
}
Write-OK "Stack started (postgres + redis + minio + api)"

# ===== Step 4: Wait healthy =====
Write-Step "4/5 Waiting for API healthy"
$ok = $false
for ($i = 1; $i -le 30; $i++) {
    try {
        $resp = Invoke-RestMethod "http://localhost:9001/health" -TimeoutSec 2 -EA Stop
        if ($resp.healthy) {
            Write-OK "API healthy (L1=$($resp.layers.l1), L3=$($resp.layers.l3_knowledge))"
            $ok = $true; break
        }
    } catch {}
    Start-Sleep 2
}
if (-not $ok) {
    Write-Err "API not healthy after 60s"
    & cmd /c "docker logs cognitive_api --tail 30"
    exit 1
}

# ===== Step 5: Configure AI clients =====
Write-Step "5/5 Configuring AI clients"

# Build MCP server config (universal)
$mcpServerBlock = @{
    command = "docker"
    args = @("exec", "-i", "cognitive_api", "python", "-u", "-m", "mcp_server.server")
    env = @{
        COGNITIVE_API_KEY = "key-design-001"
        COGNITIVE_AGENT_NAME = "ai_client"
        CC_IN_CONTAINER = "1"
        PYTHONUNBUFFERED = "1"
    }
}

$mcpJsonForImport = @{
    mcpServers = @{
        "cognitive-core" = $mcpServerBlock
    }
} | ConvertTo-Json -Depth 10

$installed = @()
$configured = @()
$manualSteps = @()

# --- Detect Claude Desktop ---
$claudeDesktopConfig = Join-Path $env:APPDATA "Claude\claude_desktop_config.json"
$claudeDesktopDir = Split-Path $claudeDesktopConfig -Parent
if (Test-Path $claudeDesktopDir) {
    $installed += "Claude Desktop"
    try {
        $cfg = if (Test-Path $claudeDesktopConfig) {
            Get-Content $claudeDesktopConfig -Raw | ConvertFrom-Json -AsHashtable
        } else { @{} }
        if (-not $cfg) { $cfg = @{} }
        if (-not $cfg.ContainsKey("mcpServers")) { $cfg["mcpServers"] = @{} }
        $cfg["mcpServers"]["cognitive-core"] = $mcpServerBlock
        $json = $cfg | ConvertTo-Json -Depth 10
        [System.IO.File]::WriteAllText($claudeDesktopConfig, $json, (New-Object System.Text.UTF8Encoding $false))
        Write-OK "Claude Desktop: config updated automatically"
        $configured += "Claude Desktop (restart needed)"
    } catch {
        Write-Warn "Claude Desktop: $_"
    }
}

# --- Detect Cursor ---
$cursorConfigDir = Join-Path $env:USERPROFILE ".cursor"
$cursorConfig = Join-Path $cursorConfigDir "mcp.json"
if (Test-Path $cursorConfigDir) {
    $installed += "Cursor"
    try {
        $cfg = if (Test-Path $cursorConfig) {
            Get-Content $cursorConfig -Raw | ConvertFrom-Json -AsHashtable
        } else { @{} }
        if (-not $cfg) { $cfg = @{} }
        if (-not $cfg.ContainsKey("mcpServers")) { $cfg["mcpServers"] = @{} }
        $cfg["mcpServers"]["cognitive-core"] = $mcpServerBlock
        $json = $cfg | ConvertTo-Json -Depth 10
        [System.IO.File]::WriteAllText($cursorConfig, $json, (New-Object System.Text.UTF8Encoding $false))
        Write-OK "Cursor: config updated automatically"
        $configured += "Cursor (restart needed)"
    } catch {
        Write-Warn "Cursor: $_"
    }
}

# --- Detect Claude Code ---
$claudeCli = Get-Command claude -ErrorAction SilentlyContinue
if ($claudeCli) {
    $installed += "Claude Code"
    $mcpAddCmd = "claude mcp add cognitive-core -- docker exec -i cognitive_api python -u -m mcp_server.server"
    & cmd /c "$mcpAddCmd >nul 2>nul"
    if ($LASTEXITCODE -eq 0) {
        Write-OK "Claude Code: added via 'claude mcp add'"
        $configured += "Claude Code (immediate)"
    } else {
        Write-Warn "Claude Code: 'claude mcp add' failed, run manually:"
        Write-Host "    $mcpAddCmd"
    }
}

# --- Detect Cherry Studio (clipboard approach — IndexedDB is binary) ---
$cherryDir = Join-Path $env:APPDATA "CherryStudio"
if (Test-Path $cherryDir) {
    $installed += "Cherry Studio"
    # Copy import-ready JSON to clipboard
    Set-Clipboard -Value $mcpJsonForImport
    Write-OK "Cherry Studio: import JSON copied to clipboard"
    $manualSteps += @"

Cherry Studio (3 clicks - JSON already in clipboard):
  1. Open Cherry Studio
  2. Settings (gear icon) -> MCP Server -> Add (or Import from JSON)
  3. Paste (Ctrl+V) - config applies
"@
}

# --- Final report ---
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  Cognitive Core installed!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""

if ($installed.Count -eq 0) {
    Write-Host "No AI clients detected on this machine." -ForegroundColor Yellow
    Write-Host "Install one of: Claude Desktop / Cursor / Cherry Studio / Claude Code"
    Write-Host ""
    Write-Host "Universal MCP config (paste manually):" -ForegroundColor Cyan
    Write-Host $mcpJsonForImport
} else {
    Write-Host "AI clients detected: $($installed -join ', ')" -ForegroundColor Cyan
    Write-Host ""

    if ($configured.Count -gt 0) {
        Write-Host "Auto-configured:" -ForegroundColor Green
        $configured | ForEach-Object { Write-Host "  - $_" }
    }

    if ($manualSteps.Count -gt 0) {
        Write-Host ""
        Write-Host "Manual steps required:" -ForegroundColor Yellow
        $manualSteps | ForEach-Object { Write-Host $_ }
    }

    Write-Host ""
    Write-Host "Restart your AI client(s) to load MCP:" -ForegroundColor Cyan

    if ($configured -match "Claude Desktop") {
        Write-Host "  Claude Desktop:" -ForegroundColor White
        Write-Host "    Stop-Process -Name Claude -Force -EA SilentlyContinue; Start-Sleep 2; Start-Process 'shell:AppsFolder\Claude_pzs8sxrjxfjjc!Claude'"
    }
    if ($configured -match "Cursor") {
        Write-Host "  Cursor: File → New Window (or restart app)" -ForegroundColor White
    }

    Write-Host ""
    Write-Host "Test command (in any AI chat):" -ForegroundColor Cyan
    Write-Host '  Use cognitive_health and show system status' -ForegroundColor White
}

Write-Host ""
Write-Host "Dashboard: http://localhost:9001/ui" -ForegroundColor Cyan
Write-Host ""
