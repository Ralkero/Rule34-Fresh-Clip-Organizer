$NoPause = $args -contains "-NoPause"
$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Desktop = [Environment]::GetFolderPath("Desktop")
$Shell = New-Object -ComObject WScript.Shell

function New-Shortcut {
    param(
        [string]$Name,
        [string]$Target,
        [string]$Description,
        [string]$Icon
    )

    $shortcutPath = Join-Path $Desktop "$Name.lnk"
    $shortcut = $Shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = Join-Path $ScriptRoot $Target
    $shortcut.WorkingDirectory = $ScriptRoot
    $shortcut.Description = $Description
    $shortcut.IconLocation = $Icon
    $shortcut.Save()
    Write-Host "Created: $shortcutPath"
}

New-Shortcut `
    -Name "Rule34 Preview" `
    -Target "Preview Rule34 Batch.cmd" `
    -Description "Preview a fresh Rule34 download folder and open the editable CSV plan." `
    -Icon "$env:SystemRoot\System32\shell32.dll,70"

New-Shortcut `
    -Name "Rule34 Apply Approved Plan" `
    -Target "Apply Approved Rule34 Plan.cmd" `
    -Description "Apply approved rows from a reviewed Rule34 organizer CSV plan." `
    -Icon "$env:SystemRoot\System32\shell32.dll,167"

Write-Host ""
Write-Host "Desktop shortcuts are ready."
if (-not $NoPause) {
    Read-Host "Press Enter to close this window" | Out-Null
}
