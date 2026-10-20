<#
.SYNOPSIS
  SportNerdBot deploy helper.
  Triggers pull-based deployment via sportnerd-gateway (/deploy) or SSH.
  Does NOT copy .env (managed securely on VM: /etc/sportnerdbot/env).
  Does NOT invoke ad-hoc migration scripts (handled by versioned migrations in deploy.sh).
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Sha,

    [string]$GatewayUrl = $env:GATEWAY_URL,
    [string]$HmacSecret = $env:GATEWAY_HMAC_SECRET,

    [switch]$UseSsh,
    [string]$VmInstance = "sportnerd-bot-vm",
    [string]$GcpProject = "gen-lang-client-0185250272",
    [string]$GcpZone = "us-central1-a"
)

$ErrorActionPreference = "Stop"

if (-not $Sha) {
    try {
        $Sha = (git rev-parse HEAD).Trim()
    } catch {
        Write-Error "Could not determine git HEAD commit SHA. Please pass -Sha explicitly."
        exit 1
    }
}

Write-Host "Target deployment SHA: $Sha"

if ($GatewayUrl -and $HmacSecret) {
    Write-Host "Triggering deployment via sportnerd-gateway at $GatewayUrl/deploy..."
    $ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $requestId = [Guid]::NewGuid().ToString()

    $payloadObj = [ordered]@{
        contract_version = "1.0"
        request_id       = $requestId
        sha              = $Sha
        ts               = $ts
    }
    $bodyJson = $payloadObj | ConvertTo-Json -Compress

    # HMAC-SHA256 signature
    $hmac = New-Object System.Security.Cryptography.HMACSHA256
    $hmac.Key = [System.Text.Encoding]::UTF8.GetBytes($HmacSecret)
    $msgToSign = [System.Text.Encoding]::UTF8.GetBytes("$bodyJson$ts")
    $sigBytes = $hmac.ComputeHash($msgToSign)
    $signature = [BitConverter]::ToString($sigBytes).Replace("-", "").ToLower()

    $payloadObj["signature"] = $signature
    $finalJson = $payloadObj | ConvertTo-Json -Compress

    $response = Invoke-RestMethod -Uri "$GatewayUrl/deploy" -Method Post -Body $finalJson -ContentType "application/json"
    Write-Host "Gateway response:" ($response | ConvertTo-Json)
} elseif ($UseSsh) {
    Write-Host "Triggering deployment on $VmInstance via gcloud compute ssh..."
    $gcloud = "gcloud"
    & $gcloud compute ssh $VmInstance --project=$GcpProject --zone=$GcpZone --command="sudo -u sportnerd /usr/local/bin/sportnerd-deploy.sh $Sha"
} else {
    Write-Host "To deploy via Gateway, set GATEWAY_URL and GATEWAY_HMAC_SECRET or pass -GatewayUrl and -HmacSecret."
    Write-Host "To deploy via SSH, pass -UseSsh."
    Write-Host "Example:"
    Write-Host "  .\deploy.ps1 -GatewayUrl https://example.com:8443 -HmacSecret <secret>"
    Write-Host "  .\deploy.ps1 -UseSsh"
}
