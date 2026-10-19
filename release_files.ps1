function Get-ReleaseFiles {
    param([string]$Root)
    $files = @(
        'telegram_bot.py', 'telegram_notify.py', 'quota_reset.py', 'screen_capture.py',
        'plugin_api.py', 'plugin_host.py', 'plugin_worker.py', 'bundle_install.py', 'side_chat.py', 'process_lifetime.py', 'desktop_bridge.py', 'desktop_receipts.py', 'local_images.py', 'connection_check.py',
        'test_interactions.py', 'test_telegram_quota.py', 'test_telegram_tables.py', 'test_screen_capture.py', 'test_plugins.py', 'test_side_chat.py', 'test_hardening.py', 'test_desktop_bridge.py', 'test_desktop_receipts.py', 'test_local_images.py', 'test_operation_feedback.py',
        'thread_history.py', 'elicitation.py', 'async_inputs.py', 'running_tasks.py', 'test_running_tasks.py', 'test_protocol_adaptation.py', 'test_async_inputs.py',
        'test_connection_check.py', 'README.md', 'CHANGELOG.md', 'SECURITY.md', 'PLUGIN_API.zh-CN.md', 'BOTFATHER_GUIDE.zh-CN.md', 'LICENSE', 'VERSION',
        'install.ps1', 'install.cmd', 'uninstall.ps1', 'uninstall.cmd', 'build_release.ps1', 'run_tests.ps1', 'release_files.ps1', 'bundled_plugins.json'
    )
    $catalog = Get-Content -Raw -LiteralPath (Join-Path $Root 'bundled_plugins.json') | ConvertFrom-Json
    foreach ($plugin in $catalog.plugins.PSObject.Properties) {
        if ($plugin.Name -notmatch '^[a-z][a-z0-9_]{0,31}$') { throw '非法随包插件 ID' }
        foreach ($file in @($plugin.Value)) {
            if ([IO.Path]::IsPathRooted($file) -or $file -match '(^|[\\/])\.\.([\\/]|$)' -or $file -match ':') { throw '非法插件文件路径' }
            $files += "bundled_plugins/$($plugin.Name)/$file"
        }
    }
    return @($files | Select-Object -Unique)
}
