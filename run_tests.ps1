[CmdletBinding()]
param(
    [ValidateSet('Communication','Notifications','Quota','Interactions','Screenshots','Plugins','SideChat','Security','Images','Feedback','Full')]
    [string[]]$Suite = @('Communication')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSCommandPath
$pythonCommand = Get-Command py.exe -ErrorAction SilentlyContinue
$pythonPrefix = @('-3', '-X', 'utf8')
if (-not $pythonCommand) {
    $pythonCommand = Get-Command python.exe -ErrorAction Stop
    $pythonPrefix = @('-X', 'utf8')
}
$groups = [ordered]@{
    Communication = @('test_desktop_bridge.py','test_desktop_receipts.py','test_connection_check.py','test_protocol_adaptation.py','test_async_inputs.py')
    Notifications = @('test_telegram_tables.py')
    Quota = @('test_telegram_quota.py')
    Interactions = @('test_interactions.py','test_running_tasks.py')
    Screenshots = @('test_screen_capture.py')
    Plugins = @('test_plugins.py')
    SideChat = @('test_side_chat.py')
    Security = @('test_hardening.py')
    Images = @('test_local_images.py')
    Feedback = @('test_operation_feedback.py')
}
$full = $Suite -contains 'Full'
$selected = if ($full) { @($groups.Keys) } else { @($Suite) }
Write-Host "离线回归（模拟外部通信，不证明真实链路可用）：$($selected -join ', ')"
if ($full) {
    & $pythonCommand.Source @pythonPrefix (Join-Path $projectRoot 'telegram_bot.py') --self-test
    if ($LASTEXITCODE -ne 0) { throw '基础自检失败。' }
}
$files = @($selected | ForEach-Object { $groups[$_] } | Select-Object -Unique)
foreach ($file in $files) {
    & $pythonCommand.Source @pythonPrefix (Join-Path $projectRoot $file)
    if ($LASTEXITCODE -ne 0) { throw "离线回归失败：$file" }
}
Write-Host '所选离线回归通过；真实通信以 connection_check.py 的报告为准。'
