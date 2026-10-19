[CmdletBinding()]
param(
    [string]$ThreadId = '',
    [string]$CredentialsPath = '',
    [string]$CodexHome = '',
    [ValidateSet('Communication','Notifications','Quota','Interactions','Screenshots','Plugins','SideChat','Security','Images','Feedback','Full')]
    [string[]]$TestSuite = @('Communication')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSCommandPath))
$version = (Get-Content -Raw -LiteralPath (Join-Path $projectRoot 'VERSION')).Trim()
$releaseName = "codex-telegram-bot-v$version"
$distRoot = Join-Path $projectRoot 'dist'
$stagingRoot = Join-Path $projectRoot '.release-staging'
$packageRoot = Join-Path $stagingRoot $releaseName
$zipPath = Join-Path $distRoot "$releaseName.zip"
$hashPath = "$zipPath.sha256"
if ([System.IO.Path]::GetFullPath($stagingRoot) -ne (Join-Path $projectRoot '.release-staging')) {
    throw 'Release staging path is outside the expected project directory.'
}
. (Join-Path $projectRoot 'release_files.ps1')
$releaseFiles = @(Get-ReleaseFiles $projectRoot)

foreach ($file in $releaseFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $file) -PathType Leaf)) {
        throw "缺少发布文件：$file"
    }
}

if (-not $ThreadId) {
    throw '发布必须通过真实投递验收。请用 -ThreadId 指定当前正在运行的验收任务；会发送一条带标记的自检消息。'
}
$pythonCommand = Get-Command py.exe -ErrorAction SilentlyContinue
$pythonPrefix = @('-3', '-X', 'utf8')
if (-not $pythonCommand) {
    $pythonCommand = Get-Command python.exe -ErrorAction Stop
    $pythonPrefix = @('-X', 'utf8')
}
# The exact files checked below must be the ones placed in the ZIP.
$releaseHashes = @{}
foreach ($file in $releaseFiles) {
    $releaseHashes[$file] = (Get-FileHash -LiteralPath (Join-Path $projectRoot $file) -Algorithm SHA256).Hash
}
$validationRoot = Join-Path $projectRoot '.validation'
$liveReportPath = Join-Path $validationRoot ('live-' + [guid]::NewGuid().ToString('N') + '.json')
$checkArguments = @((Join-Path $projectRoot 'connection_check.py'), '--thread-id', $ThreadId,
                    '--send-probe', '--report', $liveReportPath)
if ($CredentialsPath) { $checkArguments += @('--credentials', $CredentialsPath) }
if ($CodexHome) { $checkArguments += @('--codex-home', $CodexHome) }
Write-Host '先验证真实 Desktop 投递与接收记录，以及 Telegram 消息发送...'
& $pythonCommand.Source @pythonPrefix @checkArguments
if ($LASTEXITCODE -ne 0) {
    throw "真实通信验收失败，已停止发布。报告：$liveReportPath"
}
$liveReport = Get-Content -Raw -LiteralPath $liveReportPath | ConvertFrom-Json
if ($liveReport.success -ne $true -or $liveReport.mode -ne 'submission-and-receipt' -or
    $liveReport.thread_id -ne $ThreadId -or $liveReport.desktop_attempts -ne 1 -or
    $liveReport.desktop_route -ne 'desktop-user-input' -or $liveReport.receipt_origin -ne 'user' -or
    $liveReport.checks.desktop_delivery -ne 'passed' -or $liveReport.checks.telegram_delivery -ne 'passed') {
    throw '真实投递验收证据不完整，已停止发布。'
}
& (Join-Path $projectRoot 'run_tests.ps1') -Suite $TestSuite
foreach ($file in $releaseFiles) {
    if ((Get-FileHash -LiteralPath (Join-Path $projectRoot $file) -Algorithm SHA256).Hash -ne $releaseHashes[$file]) {
        throw "验收后源文件发生变化，请重新验收：$file"
    }
}

$forbiddenNames = @(
    'telegram_notify.credentials.json',
    'telegram_bot.state.json',
    'telegram_notify.log',
    'telegram_bot.log'
)
foreach ($name in $forbiddenNames) {
    if ($releaseFiles -contains $name) {
        throw "发布白名单中包含敏感文件：$name"
    }
}

$secretPattern = '(?<![A-Za-z0-9_])\d{6,}:[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_])'
foreach ($file in $releaseFiles) {
    $path = Join-Path $projectRoot $file
    $content = Get-Content -Raw -LiteralPath $path
    if ($content -match $secretPattern) {
        throw "疑似 Telegram Bot Token：$file"
    }
    if ($content -match 'C:\\Users\\[^\\\r\n]+') {
        throw "发布文件包含开发者绝对路径：$file"
    }
}

New-Item -ItemType Directory -Path $distRoot -Force | Out-Null
if (Test-Path -LiteralPath $stagingRoot) {
    Remove-Item -LiteralPath $stagingRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null
foreach ($file in $releaseFiles) {
    New-Item -ItemType Directory -Path (Split-Path -Parent (Join-Path $packageRoot $file)) -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $projectRoot $file) -Destination (Join-Path $packageRoot $file)
}
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
Compress-Archive -LiteralPath $packageRoot -DestinationPath $zipPath -CompressionLevel Optimal
$hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText($hashPath, "$hash  $releaseName.zip`n", (New-Object System.Text.UTF8Encoding($false)))
Remove-Item -LiteralPath $stagingRoot -Recurse -Force

$validationPath = Join-Path $distRoot "$releaseName.validation.json"
$validation = [ordered]@{
    version = $version
    sha256 = $hash
    live = $liveReport
    offlineSuites = @($TestSuite)
    offlineResult = 'passed'
    sourceHashes = $releaseHashes
}
[IO.File]::WriteAllText($validationPath, ($validation | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))

Write-Host "Release: $zipPath"
Write-Host "SHA256: $hash"
Write-Host "验收报告（含真实链路与离线回归的独立结果）：$validationPath"
