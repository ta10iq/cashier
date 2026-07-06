param(
    [string]$Domain = "taha-cashier.duckdns.org",
    [string]$BackendUrl = "http://127.0.0.1:3737",
    [string]$SiteName = "CashierPOS"
)

$ErrorActionPreference = "Stop"

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script as Administrator."
    }
}

function Install-MsiIfMissing {
    param(
        [string]$Name,
        [string]$CheckPath,
        [string]$Url,
        [string]$InstallerPath
    )

    if (Test-Path -LiteralPath $CheckPath) {
        Write-Host "$Name already installed."
        return
    }

    Write-Host "Downloading $Name..."
    Invoke-WebRequest -Uri $Url -OutFile $InstallerPath -UseBasicParsing

    Write-Host "Installing $Name..."
    $process = Start-Process -FilePath "msiexec.exe" -ArgumentList "/i `"$InstallerPath`" /qn /norestart" -Wait -PassThru
    if ($process.ExitCode -notin @(0, 3010)) {
        throw "$Name installation failed. Exit code: $($process.ExitCode)"
    }
}

Assert-Administrator

$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProxyRoot = "C:\inetpub\cashier-proxy"
$TempDir = Join-Path $env:TEMP "cashier-iis-setup"
$RewriteMsi = Join-Path $TempDir "rewrite_amd64_en-US.msi"
$ArrMsi = Join-Path $TempDir "requestRouter_amd64.msi"

$UrlRewriteUrl = "https://download.microsoft.com/download/1/2/8/128E2E22-C1B9-44A4-BE2A-5859ED1D4592/rewrite_amd64_en-US.msi"
$ArrUrl = "https://go.microsoft.com/fwlink/?LinkID=615136"

Write-Host "=========================================="
Write-Host "Cashier IIS reverse proxy setup"
Write-Host "=========================================="
Write-Host "Domain     : $Domain"
Write-Host "Backend    : $BackendUrl"
Write-Host "Site name  : $SiteName"
Write-Host "App folder : $AppDir"
Write-Host "=========================================="

New-Item -ItemType Directory -Force -Path $TempDir | Out-Null
New-Item -ItemType Directory -Force -Path $ProxyRoot | Out-Null

Write-Host "Installing IIS features..."
Install-WindowsFeature Web-Server, Web-Mgmt-Tools, Web-Scripting-Tools, Web-Static-Content, Web-Default-Doc, Web-Http-Errors | Out-Null

Install-MsiIfMissing `
    -Name "IIS URL Rewrite 2.1" `
    -CheckPath "$env:windir\System32\inetsrv\rewrite.dll" `
    -Url $UrlRewriteUrl `
    -InstallerPath $RewriteMsi

Install-MsiIfMissing `
    -Name "IIS Application Request Routing 3.0" `
    -CheckPath "${env:ProgramFiles}\IIS\Application Request Routing\requestRouter.dll" `
    -Url $ArrUrl `
    -InstallerPath $ArrMsi

Import-Module WebAdministration

Write-Host "Enabling ARR proxy..."
$AppCmd = "$env:windir\System32\inetsrv\appcmd.exe"
& $AppCmd set config -section:system.webServer/proxy /enabled:"True" /preserveHostHeader:"True" /commit:apphost | Out-Null

Write-Host "Writing reverse proxy web.config..."
$WebConfig = @"
<?xml version="1.0" encoding="UTF-8"?>
<configuration>
  <system.webServer>
    <rewrite>
      <rules>
        <rule name="CashierReverseProxy" stopProcessing="true">
          <match url="(.*)" />
          <action type="Rewrite" url="$BackendUrl/{R:1}" />
        </rule>
      </rules>
    </rewrite>
  </system.webServer>
</configuration>
"@
Set-Content -LiteralPath (Join-Path $ProxyRoot "web.config") -Value $WebConfig -Encoding UTF8

Write-Host "Creating or updating IIS site..."
if (-not (Test-Path "IIS:\Sites\$SiteName")) {
    New-Website -Name $SiteName -PhysicalPath $ProxyRoot -Port 80 -HostHeader $Domain -Force | Out-Null
} else {
    Set-ItemProperty "IIS:\Sites\$SiteName" -Name physicalPath -Value $ProxyRoot
}

$Bindings = Get-WebBinding -Name $SiteName -Protocol "http" -ErrorAction SilentlyContinue
$HasDomainBinding = $false
foreach ($Binding in $Bindings) {
    if ($Binding.bindingInformation -eq "*:80:$Domain") {
        $HasDomainBinding = $true
    }
}
if (-not $HasDomainBinding) {
    New-WebBinding -Name $SiteName -Protocol "http" -Port 80 -HostHeader $Domain | Out-Null
}

Write-Host "Opening firewall ports 80 and 3737..."
foreach ($Port in @(80, 3737)) {
    $RuleName = "Cashier POS Port $Port"
    if (-not (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Any | Out-Null
    }
}

Write-Host "Starting IIS site..."
Start-Website -Name $SiteName

Write-Host "Testing local backend..."
try {
    Invoke-WebRequest -Uri "$BackendUrl/login" -UseBasicParsing -TimeoutSec 8 | Out-Null
    Write-Host "Backend OK: $BackendUrl/login"
} catch {
    Write-Warning "Backend did not respond. Start the cashier app with setup_and_run_cashier.cmd, then test again."
}

Write-Host "Testing IIS local host header..."
try {
    Invoke-WebRequest -Uri "http://127.0.0.1/" -Headers @{ Host = $Domain } -UseBasicParsing -TimeoutSec 8 | Out-Null
    Write-Host "IIS reverse proxy OK."
} catch {
    Write-Warning "IIS did not proxy successfully yet: $($_.Exception.Message)"
}

Write-Host "=========================================="
Write-Host "Done."
Write-Host "Open: http://$Domain"
Write-Host "Backend must stay running on: $BackendUrl"
Write-Host "=========================================="
