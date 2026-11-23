$ErrorActionPreference = 'Stop'
if ((Get-TimeZone).Id -ne 'India Standard Time') {
    throw 'This installer expects India Standard Time. No task was registered.'
}
$supplierTaskName = 'ProcurementSupplierEvidenceDaily-agentic_project_1'
$supplierScript = Join-Path $PSScriptRoot 'supplier_refresh.py'
$supplierPython = (Get-Command python).Source
$supplierPythonWindowless = Join-Path (Split-Path $supplierPython) 'pythonw.exe'
if (Test-Path -LiteralPath $supplierPythonWindowless) { $supplierPython = $supplierPythonWindowless }
$supplierExisting = Get-ScheduledTask -TaskName $supplierTaskName -ErrorAction SilentlyContinue
if ($supplierExisting) { throw "Task $supplierTaskName already exists. Inspect it before changing or replacing it." }
$supplierAction = New-ScheduledTaskAction -Execute $supplierPython -Argument ('-B "' + $supplierScript + '"') -WorkingDirectory $PSScriptRoot
$supplierTrigger = New-ScheduledTaskTrigger -Daily -At '12:00PM'
$supplierSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$supplierPrincipal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $supplierTaskName -Action $supplierAction -Trigger $supplierTrigger -Settings $supplierSettings -Principal $supplierPrincipal -Description 'Daily noon IST public supplier publication checks. Stores evidence snapshots, failures and change flags. Does not fetch private quotes, discover new vendors, or send RFQs.' | Out-Null
Get-ScheduledTask -TaskName $supplierTaskName | Get-ScheduledTaskInfo | Select-Object TaskName,NextRunTime,LastTaskResult
