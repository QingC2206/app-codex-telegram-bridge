[CmdletBinding()]
param(
    [string]$InstallRoot = "",
    [switch]$CheckOnly,
    [switch]$Reconfigure,
    [switch]$ReplaceExistingNotify,
    [switch]$NoStartup,
    [switch]$NonInteractive,
    [switch]$SkipTelegramSetup,
    [switch]$SkipConnectionCheck,
    [switch]$DoNotStart,
    [ValidateSet('Communication','Notifications','Quota','Interactions','Screenshots','Plugins','SideChat','Security','Images','Feedback','Full')]
    [string[]]$TestSuite = @('Communication')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Utf8NoBom {
    param([string]$Path, [string]$Content)
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function ConvertTo-TomlString {
    param([string]$Value)
    $escaped = $Value.Replace('\', '\\').Replace('"', '\"')
    return '"' + $escaped + '"'
}

function Join-ProcessArguments {
    param([string[]]$Values)
    $quoted = foreach ($value in $Values) {
        if ($value -match '[\s"]') {
            '"' + $value.Replace('"', '\"') + '"'
        } else {
            $value
        }
    }
    return ($quoted -join ' ')
}

function Test-OwnedUserInputHook {
    param([object]$Entry)
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
            if ($property -and [string]$property.Value -match 'telegram_notify\.py(?:"|\s|$)') {
                return $true
            }
        }
    }
    return $false
}

function Install-UserInputHook {
    param(
        [string]$HooksPath,
        [hashtable]$Runtime,
        [string]$NotifyPath
    )

    $created = -not (Test-Path -LiteralPath $HooksPath -PathType Leaf)
    $backup = $null
    if ($created) {
        $document = [pscustomobject]@{
            description = 'User-level Codex lifecycle hooks.'
            hooks = [pscustomobject]@{}
        }
    } else {
        try {
            $document = Get-Content -Raw -LiteralPath $HooksPath | ConvertFrom-Json
        } catch {
            throw "hooks.json 无法解析，未进行修改：$HooksPath"
        }
        if (-not $document -or -not ($document -is [System.Management.Automation.PSCustomObject])) {
            throw "hooks.json 顶层必须是 JSON 对象，未进行修改：$HooksPath"
        }
        $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $backup = "$HooksPath.backup.$timestamp"
        Copy-Item -LiteralPath $HooksPath -Destination $backup -Force
    }

    if (-not $document.PSObject.Properties['hooks'] -or -not $document.hooks) {
        $hooksContainer = [pscustomobject]@{}
        if ($document.PSObject.Properties['hooks']) {
            $document.hooks = $hooksContainer
        } else {
            $document | Add-Member -NotePropertyName hooks -NotePropertyValue $hooksContainer
        }
    } elseif (-not ($document.hooks -is [System.Management.Automation.PSCustomObject])) {
        throw "hooks.json 的 hooks 字段必须是 JSON 对象，未进行修改：$HooksPath"
    }
    $hooksContainer = $document.hooks

    $preToolUse = @()
    if ($hooksContainer.PSObject.Properties['PreToolUse']) {
        $preToolUse = @(
            @($hooksContainer.PreToolUse) | Where-Object {
                -not (Test-OwnedUserInputHook $_)
            }
        )
    }
    $hookCommand = Join-ProcessArguments (
        @($Runtime.Console) + @($Runtime.ConsolePrefix) + @($NotifyPath)
    )
    $preToolUse += [pscustomobject]@{
        matcher = '^request_user_input(?:_async)?$'
        hooks = @(
            [pscustomobject]@{
                type = 'command'
                command = $hookCommand
                async = $true
                timeout = 30
            }
        )
    }
    if ($hooksContainer.PSObject.Properties['PreToolUse']) {
        $hooksContainer.PreToolUse = @($preToolUse)
    } else {
        $hooksContainer | Add-Member -NotePropertyName PreToolUse -NotePropertyValue @($preToolUse)
    }

    Write-Utf8NoBom $HooksPath (($document | ConvertTo-Json -Depth 12) + [Environment]::NewLine)
    return [pscustomobject]@{
        Path = $HooksPath
        Backup = $backup
        Created = $created
        Command = $hookCommand
    }
}

function Resolve-PythonRuntime {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $consolePath = $launcher.Source
        $prefix = @('-3')
        $guiCommand = Get-Command pyw.exe -ErrorAction SilentlyContinue
        $guiPath = if ($guiCommand) { $guiCommand.Source } else { $consolePath }
        return @{
            Console = $consolePath
            ConsolePrefix = $prefix
            Gui = $guiPath
            GuiPrefix = $prefix
        }
    }

    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $python) {
        throw "未找到 Python。请先安装 Python 3.10 或更高版本，并勾选 Add Python to PATH。"
    }
    $consolePath = $python.Source
    $guiCandidate = Join-Path (Split-Path -Parent $consolePath) 'pythonw.exe'
    $guiPath = if (Test-Path -LiteralPath $guiCandidate) { $guiCandidate } else { $consolePath }
    return @{
        Console = $consolePath
        ConsolePrefix = @()
        Gui = $guiPath
        GuiPrefix = @()
    }
}

function Invoke-Python {
    param(
        [hashtable]$Runtime,
        [string[]]$Arguments
    )
    & $Runtime.Console @($Runtime.ConsolePrefix) @Arguments |
        ForEach-Object { Write-Host $_ }
    $exitCode = $LASTEXITCODE
    return [int]$exitCode
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

function Register-BotStartupTask {
    param(
        [string]$TaskName,
        [hashtable]$Runtime,
        [string]$BotPath,
        [string]$WorkingDirectory
    )
    $arguments = Join-ProcessArguments (@($Runtime.GuiPrefix) + @($BotPath, '--service'))
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction -Execute $Runtime.Gui -Argument $arguments -WorkingDirectory $WorkingDirectory
    $logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
    $healthTrigger = New-ScheduledTaskTrigger `
        -Once `
        -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 1) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew `
        -StartWhenAvailable
    $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger @($logonTrigger, $healthTrigger) `
        -Settings $settings `
        -Principal $principal `
        -Force | Out-Null
}

try {
    if ($env:OS -ne 'Windows_NT') {
        throw "当前安装包仅支持 Windows Codex Desktop。"
    }

    $sourceRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSCommandPath))
    if (-not $InstallRoot) {
        $InstallRoot = Join-Path $env:USERPROFILE '.codex\hooks\codex-telegram-bot'
    }
    $resolvedInstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
    . (Join-Path $sourceRoot 'release_files.ps1')
    $requiredFiles = @(Get-ReleaseFiles $sourceRoot)
    foreach ($file in $requiredFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot $file) -PathType Leaf)) {
            throw "安装包缺少文件：$file"
        }
    }

    $python = Resolve-PythonRuntime
    $versionExit = Invoke-Python $python @(
        '-c',
        'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
    )
    if ($versionExit -ne 0) {
        throw "需要 Python 3.10 或更高版本。"
    }

    & (Join-Path $sourceRoot 'run_tests.ps1') -Suite $TestSuite
    if ($CheckOnly) {
        Write-Host "安装包文件与所选离线回归检查通过；本命令未验证真实通信。"
        exit 0
    }

    $codexRoot = Join-Path $env:USERPROFILE '.codex'
    $hooksRoot = Join-Path $codexRoot 'hooks'
    New-Item -ItemType Directory -Path $hooksRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $resolvedInstallRoot -Force | Out-Null

    $manifestPath = Join-Path $resolvedInstallRoot 'install-manifest.json'
    $previousNotifyLine = $null
    if (Test-Path -LiteralPath $manifestPath) {
        try {
            $oldManifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
            if ($oldManifest.previousNotifyLine) {
                $previousNotifyLine = [string]$oldManifest.previousNotifyLine
            }
        } catch {
            Write-Warning "旧安装清单无法读取，将继续覆盖程序文件。"
        }
    }

    $installedFiles = @()
    foreach ($file in $requiredFiles) {
        $destination = Join-Path $resolvedInstallRoot $file
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $sourceRoot $file) -Destination $destination -Force
        $installedFiles += $file
    }

    $credentialPath = Join-Path $resolvedInstallRoot 'telegram_notify.credentials.json'
    if ($SkipTelegramSetup) {
        Write-Warning "已跳过 Telegram 配置向导；此模式仅用于自动化测试。"
    } elseif ($Reconfigure -or -not (Test-Path -LiteralPath $credentialPath)) {
        Write-Host "`n即将启动 Telegram 配置向导。Token 只在本机输入并由 Windows DPAPI 加密。"
        $setupExit = Invoke-Python $python @((Join-Path $resolvedInstallRoot 'telegram_notify.py'), '--setup')
        if ($setupExit -eq 1) {
            throw "Telegram 配置未完成。"
        }
        if ($setupExit -eq 2) {
            Write-Warning "凭据已保存，但 Telegram 测试消息暂时发送失败。"
        }
    } else {
        Write-Host "保留现有的本机加密 Telegram 凭据。使用 -Reconfigure 可重新配置。"
    }

    $configPath = Join-Path $codexRoot 'config.toml'
    if (-not (Test-Path -LiteralPath $configPath)) {
        Write-Utf8NoBom $configPath ""
    }
    $configText = [string](Get-Content -Raw -LiteralPath $configPath)
    $notifyValues = @($python.Console) + @($python.ConsolePrefix) + @((Join-Path $resolvedInstallRoot 'telegram_notify.py'))
    $notifyItems = $notifyValues | ForEach-Object { ConvertTo-TomlString ([string]$_) }
    $notifyLine = 'notify = [ ' + ($notifyItems -join ', ') + ' ] # codex-telegram-bot'
    $notifyPattern = '(?m)^[ \t]*notify[ \t]*=.*$'
    $notifyMatch = [regex]::Match($configText, $notifyPattern)
    if ($notifyMatch.Success) {
        $existingLine = $notifyMatch.Value.Trim()
        $ownedNotify = $existingLine -match 'codex-telegram-bot|telegram_notify\.py'
        if (-not $ownedNotify) {
            if (-not $ReplaceExistingNotify) {
                if ($NonInteractive) {
                    throw "config.toml 已有其他 notify 配置。请人工合并，或使用 -ReplaceExistingNotify。"
                }
                Write-Warning "检测到其他 notify 配置：$existingLine"
                $answer = Read-Host "是否备份并替换它？[y/N]"
                if ($answer.Trim().ToLowerInvariant() -notin @('y', 'yes', '是')) {
                    throw "已取消，原 notify 配置未修改。"
                }
            }
            $previousNotifyLine = $existingLine
        }
        $configText = $configText.Remove($notifyMatch.Index, $notifyMatch.Length).Insert($notifyMatch.Index, $notifyLine)
    } else {
        $configText = $notifyLine + [Environment]::NewLine + $configText
    }
    $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $configBackup = "$configPath.backup.$timestamp"
    Copy-Item -LiteralPath $configPath -Destination $configBackup -Force
    Write-Utf8NoBom $configPath $configText

    $hooksPath = Join-Path $codexRoot 'hooks.json'
    $hookInstall = Install-UserInputHook `
        -HooksPath $hooksPath `
        -Runtime $python `
        -NotifyPath (Join-Path $resolvedInstallRoot 'telegram_notify.py')

    $startupShortcut = $null
    $startupTaskName = $null
    if (-not $NoStartup) {
        $startupTaskName = 'Codex Telegram Bot'
        Register-BotStartupTask `
            -TaskName $startupTaskName `
            -Runtime $python `
            -BotPath (Join-Path $resolvedInstallRoot 'telegram_bot.py') `
            -WorkingDirectory $resolvedInstallRoot
        $startupDirectory = [Environment]::GetFolderPath('Startup')
        $legacyShortcut = Join-Path $startupDirectory 'Codex Telegram Bot.lnk'
        if (Test-Path -LiteralPath $legacyShortcut -PathType Leaf) {
            Remove-Item -LiteralPath $legacyShortcut -Force
        }
    }

    $version = (Get-Content -Raw -LiteralPath (Join-Path $resolvedInstallRoot 'VERSION')).Trim()
    $manifest = [ordered]@{
        version = $version
        installedAt = (Get-Date).ToString('o')
        installRoot = $resolvedInstallRoot
        configPath = $configPath
        configBackup = $configBackup
        notifyLine = $notifyLine
        previousNotifyLine = $previousNotifyLine
        hooksPath = $hookInstall.Path
        hooksBackup = $hookInstall.Backup
        hooksCreated = $hookInstall.Created
        userInputHookCommand = $hookInstall.Command
        startupShortcut = $startupShortcut
        startupTaskName = $startupTaskName
        pythonConsole = $python.Console
        pythonConsolePrefix = @($python.ConsolePrefix)
        pythonGui = $python.Gui
        pythonGuiPrefix = @($python.GuiPrefix)
        installedFiles = $installedFiles
    }
    Write-Utf8NoBom $manifestPath (($manifest | ConvertTo-Json -Depth 5) + [Environment]::NewLine)

    if (-not $SkipConnectionCheck) {
        Write-Host "正在只读检查 Telegram 与 Codex Desktop 原生接口（不提交任务消息）..."
        $checkExit = Invoke-Python $python @((Join-Path $resolvedInstallRoot 'telegram_bot.py'), '--check')
        if ($checkExit -ne 0) {
            Write-Warning "连接检查未通过。可以稍后在安装目录运行 telegram_bot.py --check。"
        }
    }

    $botPath = Join-Path $resolvedInstallRoot 'telegram_bot.py'
    Stop-InstalledService $botPath
    if (-not $DoNotStart) {
        if ($startupTaskName) {
            Start-ScheduledTask -TaskName $startupTaskName
        } else {
            $serviceArguments = Join-ProcessArguments (@($python.GuiPrefix) + @($botPath, '--service'))
            Start-Process -FilePath $python.Gui -ArgumentList $serviceArguments -WorkingDirectory $resolvedInstallRoot -WindowStyle Hidden
        }
    }

    Write-Host "`n安装完成。"
    Write-Host "版本：$version"
    Write-Host "目录：$resolvedInstallRoot"
    Write-Host "配置备份：$configBackup"
    if ($hookInstall.Backup) {
        Write-Host "Hook 备份：$($hookInstall.Backup)"
    }
    Write-Host "请完全退出并重新打开 Codex Desktop；若出现 Hook 审核提示，请确认信任。"
    exit 0
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
