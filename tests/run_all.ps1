# Run the full test battery (Windows). Pass -Offline to skip the two live spot-checks.
param([switch]$Offline)
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONIOENCODING = "utf-8"
python "$here\test_math.py"
$math = $LASTEXITCODE
if ($Offline) { python "$here\smoke_cli.py" --offline } else { python "$here\smoke_cli.py" }
$smoke = $LASTEXITCODE
if (($math -ne 0) -or ($smoke -ne 0)) { Write-Output "TESTS FAILED (math=$math smoke=$smoke)"; exit 1 }
Write-Output "ALL TESTS PASSED"
