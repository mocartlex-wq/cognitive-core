# Подключить этот ПК (Windows) к Cognitive Core: память, комната агентов, контрольные точки.
#
#   $env:COGCORE_API_KEY = "<ключ>"; irm https://mcp.me-ai.ru/connect/kit/setup.ps1 | iex
#   или с одноразовым кодом из кабинета (/ui/profile -> «Передать помощнику»):
#   $env:COGCORE_CLAIM = "ABCD-1234"; irm https://mcp.me-ai.ru/connect/kit/setup.ps1 | iex
#
# Повторный запуск безопасен. Ключ не печатается и не уходит в URL.
$ErrorActionPreference = "Stop"
$Api = if ($env:COGCORE_API) { $env:COGCORE_API } else { "https://mcp.me-ai.ru" }
$Dir = Join-Path $HOME ".cognitive-core"
New-Item -ItemType Directory -Force -Path $Dir | Out-Null

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $py) { Write-Host "Нужен Python 3.10+: https://www.python.org/downloads/ (галочка «Add to PATH»), затем повторите." -ForegroundColor Yellow; return }

foreach ($f in @("cogcore_kit.py", "setup_core.py")) {
  Invoke-WebRequest -UseBasicParsing -Uri "$Api/connect/kit/$f" -OutFile (Join-Path $Dir $f)
}
$args2 = @("-X", "utf8", (Join-Path $Dir "setup_core.py"), "--api", $Api)
if ($env:COGCORE_CLAIM) { $args2 += @("--claim", $env:COGCORE_CLAIM) }
& $py @args2
