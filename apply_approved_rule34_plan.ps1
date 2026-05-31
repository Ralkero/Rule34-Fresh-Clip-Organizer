param(
    [Parameter(Position = 0)]
    [string]$Plan,
    [switch]$NoOpen,
    [switch]$SkipConfirm
)

$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

function Pause-IfInteractive {
    if (-not $NoOpen) {
        Write-Host ""
        Read-Host "Press Enter to close this window" | Out-Null
    }
}

function Select-PlanFile {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = "Choose reviewed Rule34 preview CSV"
    $dialog.Filter = "Rule34 preview CSV (r34_preview_*.csv)|r34_preview_*.csv|CSV files (*.csv)|*.csv|All files (*.*)|*.*"
    $dialog.Multiselect = $false
    $result = $dialog.ShowDialog()
    if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
        return $dialog.FileName
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

function Is-ApprovedValue {
    param([string]$Value)
    if ($null -eq $Value) {
        $normalized = ""
    }
    else {
        $normalized = $Value.Trim().ToLowerInvariant()
    }
    return @("1", "true", "yes", "y", "approved", "apply") -contains $normalized
}

try {
    if (-not $Plan) {
        $Plan = Select-PlanFile
    }
    if (-not $Plan) {
        Write-Host "No CSV plan selected. Nothing to do."
        Pause-IfInteractive
        exit 0
    }

    $planPath = (Resolve-Path -LiteralPath $Plan).Path
    if (-not (Test-Path -LiteralPath $planPath -PathType Leaf)) {
        throw "Plan is not a file: $planPath"
    }

    $rows = @(Import-Csv -LiteralPath $planPath)
    $actionableRows = @($rows | Where-Object {
        $status = ""
        if ($null -ne $_.status) {
            $status = $_.status.Trim().ToLowerInvariant()
        }
        ($status -eq "content_review") -or
        ((Is-ApprovedValue $_.approved) -and
            ($status -notin @("blocked", "duplicate", "missing_source", "unmatched", "invalid", "content_review")))
    })

    Write-Host "Rule34 Organizer Apply"
    Write-Host "Plan: $planPath"
    Write-Host "Rows in plan: $($rows.Count)"
    Write-Host "Rows apply will process: $($actionableRows.Count)"
    Write-Host ""

    if ($actionableRows.Count -eq 0) {
        Write-Host "There are no approved rows or content-review holds to process. Edit the CSV first, then run this launcher again."
        Pause-IfInteractive
        exit 0
    }

    if (-not $SkipConfirm) {
        $answer = Read-Host "Move/rename the approved rows now? Type APPLY to continue"
        if ($answer -ne "APPLY") {
            Write-Host "Cancelled. No files were moved."
            Pause-IfInteractive
            exit 0
        }
    }

    Write-Host ""
    Write-Host "Starting apply. Progress will appear below."
    Write-Host ""

    $pythonCommand = @(Resolve-PythonCommand)
    $pythonExe = $pythonCommand[0]
    $pythonPrefixArgs = @()
    if ($pythonCommand.Count -gt 1) {
        $pythonPrefixArgs = $pythonCommand[1..($pythonCommand.Count - 1)]
    }
    Push-Location $ScriptRoot
    try {
        & $pythonExe @pythonPrefixArgs ".\r34_organizer.py" apply --plan $planPath
        if ($LASTEXITCODE -ne 0) {
            throw "Apply failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }

    $planDir = Split-Path -Parent $planPath
    $log = Get-ChildItem -LiteralPath $planDir -Filter "r34_apply_*.csv" -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1

    Write-Host ""
    Write-Host "Apply finished."
    if ($log) {
        Write-Host "Apply log: $($log.FullName)"
        if (-not $NoOpen) {
            Invoke-Item -LiteralPath $log.FullName
        }
    }
    else {
        Write-Warning "No apply log was found next to the plan."
    }
}
catch {
    Write-Host ""
    Write-Host "Apply failed:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Pause-IfInteractive
    exit 1
}

Pause-IfInteractive
