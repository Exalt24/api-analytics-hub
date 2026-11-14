# LIFESPAN: permanent (the DAX runner behind powerbi/check_model.py; Windows only, needs Power BI Desktop)
<#
Runs DAX queries against the model a running Power BI Desktop has open, and prints the results as JSON.

    run_dax.ps1 -Queries '{"total":"EVALUATE ROW(\"n\", COUNTROWS(Snapshot))"}'
    run_dax.ps1 -List        prints the port of every Desktop model that is open, newest first

Desktop starts a private Analysis Services instance for each open file and writes its port to msmdsrv.port.txt next to the model's data. The port changes
on every start, so it is read from the newest msmdsrv process rather than typed.
#>
param([string]$Queries = "", [int]$Port = 0, [switch]$List)

$ErrorActionPreference = "Stop"

function Get-ModelPorts {
    $out = @()
    foreach ($p in (Get-CimInstance Win32_Process -Filter "Name='msmdsrv.exe'" | Sort-Object CreationDate -Descending)) {
        if ($p.CommandLine -match '-s "([^"]+)"') {
            $file = Join-Path $Matches[1] "msmdsrv.port.txt"
            if (Test-Path $file) {
                $port = (Get-Content $file -Encoding Unicode -Raw).Trim()
                if ($port -match '^\d+$') { $out += [pscustomobject]@{ Port = [int]$port; Pid = $p.ProcessId } }
            }
        }
    }
    return $out
}

if ($List) {
    Get-ModelPorts | ConvertTo-Json -Compress
    exit 0
}

if ($Port -eq 0) {
    $found = Get-ModelPorts
    if (-not $found) { throw "no Power BI Desktop model is open" }
    $Port = $found[0].Port
}

$pkg = Get-AppxPackage Microsoft.MicrosoftPowerBIDesktop
if (-not $pkg) { throw "Power BI Desktop (Microsoft Store build) is not installed on this machine" }
Add-Type -Path (Join-Path $pkg.InstallLocation "bin\Microsoft.PowerBI.AdomdClient.dll")

$conn = New-Object Microsoft.AnalysisServices.AdomdClient.AdomdConnection("Data Source=localhost:$Port")
$conn.Open()
$results = [ordered]@{}
try {
    $queryTable = $Queries | ConvertFrom-Json
    foreach ($prop in $queryTable.PSObject.Properties) {
        $cmd = $conn.CreateCommand()
        $cmd.CommandText = $prop.Value
        $reader = $cmd.ExecuteReader()
        $rows = New-Object System.Collections.ArrayList
        while ($reader.Read()) {
            $row = [ordered]@{}
            for ($i = 0; $i -lt $reader.FieldCount; $i++) {
                $v = $reader.GetValue($i)
                if ($v -is [DateTime]) { $v = $v.ToString("yyyy-MM-dd") }
                if ($v -is [DBNull]) { $v = $null }
                $row[$reader.GetName($i)] = $v
            }
            [void]$rows.Add($row)
        }
        $reader.Close()
        $results[$prop.Name] = $rows
    }
}
finally {
    $conn.Close()
}
$results | ConvertTo-Json -Depth 6 -Compress
