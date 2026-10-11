# One-liner for Windows. Puts run on this machine.
# Uses python3, python, or py when one is already on PATH. Does not install a runtime.
$ErrorActionPreference = "Stop"
$Page = "https://htmlpreview.github.io/?https://github.com/AlexTouvras/delivery-signal/blob/main/docs/index.html"
$Base = "https://raw.githubusercontent.com/AlexTouvras/delivery-signal/main/delivery-signal"
$Dest = if ($env:DELIVERY_SIGNAL_HOME) { $env:DELIVERY_SIGNAL_HOME } else { Join-Path $env:USERPROFILE ".delivery-signal" }
$Names = @("pipeline.py", "extract.py", "link.py", "run", "run.ps1", "run.cmd")

New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$Here = $PSScriptRoot
foreach ($Name in $Names) {
    $Target = Join-Path $Dest $Name
    $Local = if ($Here) { Join-Path $Here $Name } else { $null }
    if ($Local -and (Test-Path $Local)) {
        Copy-Item $Local $Target -Force
    } else {
        Invoke-WebRequest "$Base/$Name" -OutFile $Target
    }
}
Copy-Item (Join-Path $Dest "run.cmd") (Join-Path $Dest "delivery-signal.cmd") -Force

$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $UserPath) { $UserPath = "" }
$Parts = $UserPath -split ";" | Where-Object { $_ -and ($_ -ne $Dest) }
$Updated = (@($Dest) + $Parts) -join ";"
[Environment]::SetEnvironmentVariable("Path", $Updated, "User")
$env:Path = "$Dest;$env:Path"

Write-Host "Installed. Open a new terminal and run:"
Write-Host "  run"
Write-Host "That opens the configuration page. A CSV is: run issues.csv"
$Python = Get-Command python3, python, py -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $Python) {
    Write-Host "Python is not on this machine. Open the page and choose the CSV: $Page"
}
