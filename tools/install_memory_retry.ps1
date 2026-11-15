param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Script,
    [Parameter(Mandatory=$true)][string]$Config
)
$ErrorActionPreference = 'Stop'
$taskName = 'Cognitive Core - Memory outbox'
$description = 'Cognitive Core memory adapter v1: retry existing local deliveries only'
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and $existing.Description -ne $description) { throw 'Task name is already used by another owner' }
foreach ($item in @($Python, $Script, $Config)) { if (-not (Test-Path -LiteralPath $item -PathType Leaf)) { throw "Missing file: $item" } }
$arguments = '-X utf8 "' + (Resolve-Path -LiteralPath $Script).Path + '" --config "' + (Resolve-Path -LiteralPath $Config).Path + '"'
$action = New-ScheduledTaskAction -Execute (Resolve-Path -LiteralPath $Python).Path -Argument $arguments
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes 2)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Description $description -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Select-Object TaskName,State
