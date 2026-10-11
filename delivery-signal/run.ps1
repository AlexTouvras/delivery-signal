# In your own PowerShell: .\run opens the configuration page.
# A single CSV still works: .\run issues.csv
# Does not install a runtime, a package, or a server.
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
$Page = "https://htmlpreview.github.io/?https://github.com/AlexTouvras/delivery-signal/blob/main/docs/index.html"
$Base = "https://raw.githubusercontent.com/AlexTouvras/delivery-signal/main/delivery-signal"

$Root = $PSScriptRoot
if (-not $Root -or -not (Test-Path (Join-Path $Root "pipeline.py")) -or -not (Test-Path (Join-Path $Root "link.py"))) {
    $Root = Join-Path $env:TEMP "delivery-signal"
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    Invoke-WebRequest "$Base/pipeline.py" -OutFile (Join-Path $Root "pipeline.py")
    Invoke-WebRequest "$Base/extract.py" -OutFile (Join-Path $Root "extract.py")
    Invoke-WebRequest "$Base/link.py" -OutFile (Join-Path $Root "link.py")
}

$Python = $null
$Prefix = @()
foreach ($Name in @("python3", "python", "py")) {
    $Command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($Command) {
        $Python = $Command.Source
        if ($Name -eq "py") { $Prefix = @("-3") }
        break
    }
}

if (-not $Python) {
    Write-Error "Python is not on this machine. Open the page and choose the CSV: $Page"
    exit 1
}

& $Python @Prefix (Join-Path $Root "pipeline.py") @Rest
exit $LASTEXITCODE
