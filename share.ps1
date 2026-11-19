# Shares ClipCrew through a free Cloudflare Quick Tunnel (https://....trycloudflare.com, no account needed).
# Prints the full link for friends (address + secret key) and saves it to share-link.txt.
# Reconnects automatically if the tunnel drops. Close this window to stop sharing.
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$Host.UI.RawUI.WindowTitle = "ClipCrew - sharing"

function Test-App {
    try { Invoke-WebRequest "http://127.0.0.1:8765/api/state" -UseBasicParsing -TimeoutSec 3 | Out-Null; $true } catch { $false }
}
if (-not (Test-App)) {
    Write-Host "Starting ClipCrew..."
    Start-Process cmd -ArgumentList "/c", "start.bat" -WindowStyle Minimized
    for ($i = 0; $i -lt 30 -and -not (Test-App); $i++) { Start-Sleep -Seconds 2 }
}

$key = ((Get-Content ".env" | Select-String "^CLIPCREW_ACCESS_KEY=").Line -split "=", 2)[1]
$cloudflared = Join-Path $PSScriptRoot "tools\cloudflared.exe"
if (-not (Test-Path $cloudflared)) {
    Write-Host "tools\cloudflared.exe is missing. Download cloudflared-windows-amd64.exe from" -ForegroundColor Red
    Write-Host "https://github.com/cloudflare/cloudflared/releases and save it as tools\cloudflared.exe" -ForegroundColor Red
    exit 1
}

while ($true) {
    Write-Host "Opening the tunnel..."
    & $cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:8765" 2>&1 |
        ForEach-Object {
            $line = "$_"
            if ($line -match "(https://[a-z0-9-]+\.trycloudflare\.com)") {
                $link = "$($Matches[1])/k/$key"
                Set-Content -Path "share-link.txt" -Value $link -Encoding utf8
                Write-Host ""
                Write-Host "  ClipCrew is live. Send your friends this link:" -ForegroundColor Green
                Write-Host "    $link" -ForegroundColor Yellow
                Write-Host "  (also saved in share-link.txt - keep this window open while they use it)"
                Write-Host ""
            } elseif ($line -match " ERR ") {
                Write-Host $line -ForegroundColor DarkYellow
            }
        }
    Write-Host "Tunnel dropped - reconnecting in 5 seconds (the address will change; check share-link.txt)..."
    Start-Sleep -Seconds 5
}
