[CmdletBinding()]
param(
    [string]$InstallRoot = "",
    [switch]$KeepData,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Utf8NoBom {
    param([string]$Path, [string]$Content)
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Stop-InstalledService {
    param([string]$BotPath)
    $pattern = [regex]::Escape([System.IO.Path]::GetFullPath($BotPath))
    $processes = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -match $pattern -and
            $_.CommandLine -match '(?:^|\s)--service(?:\s|$)' -and
            $_.Name -in @('pyw.exe', 'pythonw.exe', 'python.exe', 'py.exe')
        }
    foreach ($process in $processes) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Test-OwnedUserInputHook {
    param(
        [object]$Entry,
        [string]$ExpectedCommand
    )
    if (-not $Entry) {
        return $false
    }
    $matcherProperty = $Entry.PSObject.Properties['matcher']
    if (-not $matcherProperty -or [string]$matcherProperty.Value -notin @('^request_user_input$', '^request_user_input(?:_async)?$')) {
        return $false
    }
    $hooksProperty = $Entry.PSObject.Properties['hooks']
    if (-not $hooksProperty) {
        return $false
    }
    foreach ($hook in @($hooksProperty.Value)) {
        if (-not $hook) {
            continue
        }
        foreach ($propertyName in @('command', 'commandWindows')) {
            $property = $hook.PSObject.Properties[$propertyName]
            if (-not $property) {
                continue
            }
            $command = [string]$property.Value
            if (($ExpectedCommand -and $command -eq $ExpectedCommand) -or
                $command -match 'telegram_notify\.py(?:"|\s|$)') {
                return $true
            }
        }
    }
    return $false
}

try {
    if (-not $InstallRoot) {
        $InstallRoot = Join-Path $env:USERPROFILE '.codex\hooks\codex-telegram-bot'
    }
    $resolvedInstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
    $hooksRoot = [System.IO.Path]::GetFullPath((Join-Path $env:USERPROFILE '.codex\hooks'))
    $safePrefix = $hooksRoot.TrimEnd('\') + '\'
    if ($resolvedInstallRoot -eq $hooksRoot -or -not $resolvedInstallRoot.StartsWith($safePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝卸载：目标目录不在当前用户的 .codex\hooks 子目录内。"
    }
    if (-not (Test-Path -LiteralPath $resolvedInstallRoot -PathType Container)) {
        Write-Host "未找到安装目录，无需卸载：$resolvedInstallRoot"
        exit 0
    }

    if (-not $Force) {
        Write-Host "将停止 Bot、移除 Codex notify/等待输入 Hook 配置和自启动项。"
        if (-not $KeepData) {
            Write-Warning "本机加密 Token、状态、日志及回复缓存也会被删除。"
        }
        $answer = Read-Host "确认卸载？[y/N]"
        if ($answer.Trim().ToLowerInvariant() -notin @('y', 'yes', '是')) {
            Write-Host "已取消。"
            exit 0
        }
    }

    $manifestPath = Join-Path $resolvedInstallRoot 'install-manifest.json'
    $manifest = $null
    if (Test-Path -LiteralPath $manifestPath) {
        try {
            $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
        } catch {
            Write-Warning "安装清单无法读取，将按默认位置卸载。"
        }
    }

    Stop-InstalledService (Join-Path $resolvedInstallRoot 'telegram_bot.py')

    $startupTaskName = if ($manifest) {
        if ($manifest.PSObject.Properties['startupTaskName'] -and $manifest.startupTaskName) {
            [string]$manifest.startupTaskName
        } else {
            $null
        }
    } else {
        'Codex Telegram Bot'
    }
    if ($startupTaskName -and (Get-ScheduledTask -TaskName $startupTaskName -ErrorAction SilentlyContinue)) {
        Unregister-ScheduledTask -TaskName $startupTaskName -Confirm:$false
    }

    $startupShortcut = $null
    if ($manifest) {
        if ($manifest.PSObject.Properties['startupShortcut'] -and $manifest.startupShortcut) {
            $startupShortcut = [string]$manifest.startupShortcut
        }
    } else {
        $startupDirectory = [Environment]::GetFolderPath('Startup')
        if ($startupDirectory) {
            $startupShortcut = Join-Path $startupDirectory 'Codex Telegram Bot.lnk'
        }
    }
    if ($startupShortcut -and (Test-Path -LiteralPath $startupShortcut -PathType Leaf)) {
        Remove-Item -LiteralPath $startupShortcut -Force
    }

    $configPath = if ($manifest -and $manifest.configPath) {
        [string]$manifest.configPath
    } else {
        Join-Path $env:USERPROFILE '.codex\config.toml'
    }
    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        $configText = [string](Get-Content -Raw -LiteralPath $configPath)
        $notifyPattern = '(?m)^[ \t]*notify[ \t]*=.*$'
        $notifyMatch = [regex]::Match($configText, $notifyPattern)
        if ($notifyMatch.Success -and $notifyMatch.Value -match 'codex-telegram-bot') {
            $replacement = ''
            if ($manifest -and $manifest.previousNotifyLine) {
                $replacement = [string]$manifest.previousNotifyLine
            }
            $newConfig = $configText.Remove($notifyMatch.Index, $notifyMatch.Length).Insert($notifyMatch.Index, $replacement)
            $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
            Copy-Item -LiteralPath $configPath -Destination "$configPath.backup.uninstall.$timestamp" -Force
            Write-Utf8NoBom $configPath $newConfig
        }
    }

    $hooksPath = if ($manifest -and $manifest.PSObject.Properties['hooksPath'] -and $manifest.hooksPath) {
        [string]$manifest.hooksPath
    } else {
        Join-Path $env:USERPROFILE '.codex\hooks.json'
    }
    if (Test-Path -LiteralPath $hooksPath -PathType Leaf) {
        try {
            $hooksDocument = Get-Content -Raw -LiteralPath $hooksPath | ConvertFrom-Json
            if ($hooksDocument -and $hooksDocument.hooks -and
                $hooksDocument.hooks.PSObject.Properties['PreToolUse']) {
                $expectedCommand = if ($manifest -and $manifest.PSObject.Properties['userInputHookCommand'] -and $manifest.userInputHookCommand) {
                    [string]$manifest.userInputHookCommand
                } else {
                    ''
                }
                $remaining = @(
                    @($hooksDocument.hooks.PreToolUse) | Where-Object {
                        -not (Test-OwnedUserInputHook $_ $expectedCommand)
                    }
                )
                $hooksDocument.hooks.PreToolUse = @($remaining)
                $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
                Copy-Item -LiteralPath $hooksPath -Destination "$hooksPath.backup.uninstall.$timestamp" -Force
                Write-Utf8NoBom $hooksPath (($hooksDocument | ConvertTo-Json -Depth 12) + [Environment]::NewLine)
            }
        } catch {
            Write-Warning "等待输入 Hook 未能自动移除：$($_.Exception.Message)"
        }
    }

    if ($KeepData) {
        . (Join-Path $resolvedInstallRoot 'release_files.ps1')
    $programFiles = @(Get-ReleaseFiles $resolvedInstallRoot) + @('install-manifest.json')
        foreach ($file in $programFiles) {
            $path = Join-Path $resolvedInstallRoot $file
            if (Test-Path -LiteralPath $path -PathType Leaf) {
                Remove-Item -LiteralPath $path -Force
            }
        }
        Get-ChildItem -LiteralPath $resolvedInstallRoot -Filter '__pycache__' -Directory -ErrorAction SilentlyContinue |
            ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
        Write-Host "程序已卸载；凭据、状态、日志和回复缓存保留在：$resolvedInstallRoot"
    } else {
        Remove-Item -LiteralPath $resolvedInstallRoot -Recurse -Force
        Write-Host "卸载完成，本机 Bot 数据已删除。"
    }
    Write-Host "自定义插件和插件状态保留在用户 .codex\telegram-plugins 与 telegram-plugin-state 中。"
    Write-Host "请完全退出并重新打开 Codex Desktop。"
    exit 0
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
