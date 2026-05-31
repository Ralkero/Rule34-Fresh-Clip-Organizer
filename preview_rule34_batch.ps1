param(
    [Parameter(Position = 0)]
    [string]$Source,
    [switch]$NoOpen
)

$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

function Pause-IfInteractive {
    if (-not $NoOpen) {
        Write-Host ""
        Read-Host "Press Enter to close this window" | Out-Null
    }
}

function Select-SourceFolder {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = "Choose the fresh Rule34 download folder to preview"
    $dialog.ShowNewFolderButton = $false
    $result = $dialog.ShowDialog()
    if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
        return $dialog.SelectedPath
    }
    return ""
}

function Resolve-PythonCommand {
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        return @($python.Source)
    }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        return @($py.Source, "-3")
    }
    throw "Python was not found. Install Python 3 or add python.exe to PATH."
}

try {
    if (-not $Source) {
        $Source = Select-SourceFolder
    }
    if (-not $Source) {
        Write-Host "No source folder selected. Nothing to do."
        Pause-IfInteractive
        exit 0
    }

    $sourcePath = (Resolve-Path -LiteralPath $Source).Path
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Container)) {
        throw "Source is not a folder: $sourcePath"
    }

    Write-Host "Rule34 Organizer Preview"
    Write-Host "Source: $sourcePath"
    Write-Host ""

    $pythonCommand = @(Resolve-PythonCommand)
    $pythonExe = $pythonCommand[0]
    $pythonPrefixArgs = @()
    if ($pythonCommand.Count -gt 1) {
        $pythonPrefixArgs = $pythonCommand[1..($pythonCommand.Count - 1)]
    }
    Push-Location $ScriptRoot
    try {
        & $pythonExe @pythonPrefixArgs ".\r34_organizer.py" preview --source $sourcePath
        if ($LASTEXITCODE -ne 0) {
            throw "Preview failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }

    $csv = Get-ChildItem -LiteralPath $sourcePath -Filter "r34_preview_*.csv" -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    $summary = Get-ChildItem -LiteralPath $sourcePath -Filter "r34_preview_*.md" -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1

    Write-Host ""
    Write-Host "Preview finished."
    if ($csv) {
        Write-Host "CSV plan: $($csv.FullName)"
        if (-not $NoOpen) {
            Invoke-Item -LiteralPath $csv.FullName
        }
    }
    if ($summary) {
        Write-Host "Summary:  $($summary.FullName)"
        if (-not $NoOpen) {
            Invoke-Item -LiteralPath $summary.FullName
        }
    }
    if (-not $csv) {
        Write-Warning "No preview CSV was found in the source folder."
    }
}
catch {
    Write-Host ""
    Write-Host "Preview failed:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Pause-IfInteractive
    exit 1
}

Pause-IfInteractive
