param(
    [switch]$SmokeTest,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$backendRoot = Join-Path $repoRoot "backend"
$frontendRoot = Join-Path $repoRoot "frontend"
$venvRoot = Join-Path $backendRoot ".venv"
$pythonPath = Join-Path $venvRoot "Scripts\python.exe"
$viteCli = Join-Path $frontendRoot "node_modules\vite\bin\vite.js"
$runLogRoot = Join-Path $repoRoot ".dev-run"
$hostAddress = "127.0.0.1"
$backendPort = 8000
$frontendPort = 5173
$allowedOrigin = "http://${hostAddress}:${frontendPort}"
$backendProcess = $null
$frontendProcess = $null
$httpClient = $null
$previousEnvironment = @{}
$secret = $null

function Set-ChildEnvironment([string]$Name, [string]$Value) {
    if (-not $previousEnvironment.ContainsKey($Name)) {
        $previousEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, "Process")
    }
    [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
}

function Restore-ChildEnvironment {
    foreach ($name in $previousEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], "Process")
    }
}

function Test-HttpStatus([string]$Url, [string]$Origin, [string]$FetchSite, [int]$ExpectedStatus) {
    $request = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Get, $Url)
    [void]$request.Headers.TryAddWithoutValidation("Origin", $Origin)
    [void]$request.Headers.TryAddWithoutValidation("Sec-Fetch-Site", $FetchSite)
    try {
        $response = $httpClient.SendAsync($request).GetAwaiter().GetResult()
        try { return [int]$response.StatusCode -eq $ExpectedStatus }
        finally { $response.Dispose() }
    }
    catch { return $false }
    finally { $request.Dispose() }
}

function Stop-ChildProcess($Process) {
    if ($null -ne $Process) {
        try {
            $Process.Refresh()
            if (-not $Process.HasExited) {
                Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
                [void]$Process.WaitForExit(5000)
            }
        }
        catch { }
    }
}

try {
    $nodeVersionText = (& node --version 2>$null).Trim().TrimStart("v")
    if ($LASTEXITCODE -ne 0) { throw "Node.js chưa được cài hoặc không nằm trong PATH." }
    $nodeVersion = [version]$nodeVersionText
    if ($nodeVersion.Major -ne 22 -or $nodeVersion -lt [version]"22.12.0") {
        throw "Vite 8 cần Node.js 22.12+ hoặc 20.19+; launcher này yêu cầu nhánh 22.12+. Đang có $nodeVersionText."
    }

    $pythonVersionText = (& py -3.14 --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Cần Python 3.14 để tạo môi trường backend. $pythonVersionText" }

    $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in @($backendPort, $frontendPort) })
    if ($listeners.Count -gt 0) {
        $portsInUse = ($listeners | ForEach-Object { $_.LocalPort } | Sort-Object -Unique) -join ", "
        throw "Port local $portsInUse đang được sử dụng. Launcher dừng để không kết nối nhầm service."
    }

    if (-not (Test-Path $pythonPath)) {
        & py -3.14 -m venv $venvRoot
        if ($LASTEXITCODE -ne 0) { throw "Không tạo được backend/.venv." }
    }
    & $pythonPath -m pip install --disable-pip-version-check -r (Join-Path $backendRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "Cài backend requirements thất bại." }

    if (-not (Test-Path $viteCli)) {
        & npm ci --prefix $frontendRoot
        if ($LASTEXITCODE -ne 0) { throw "npm ci thất bại." }
    }
    if (-not (Test-Path $viteCli)) { throw "Không tìm thấy Vite CLI sau cài đặt frontend." }

    New-Item -ItemType Directory -Path $runLogRoot -Force | Out-Null
    $randomBytes = New-Object byte[] 32
    $random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $random.GetBytes($randomBytes) } finally { $random.Dispose() }
    $secret = [Convert]::ToBase64String($randomBytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")

    Set-ChildEnvironment "APP_HOST" $hostAddress
    Set-ChildEnvironment "APP_PORT" ([string]$backendPort)
    Set-ChildEnvironment "APP_ALLOWED_ORIGIN" $allowedOrigin
    Set-ChildEnvironment "AUTODUB_BOOTSTRAP_SECRET" $secret
    $geminiKey = [Environment]::GetEnvironmentVariable("AUTODUB_GEMINI_API_KEY", "Process")
    $envFile = Join-Path $repoRoot ".env"
    if ([string]::IsNullOrWhiteSpace($geminiKey) -and (Test-Path -LiteralPath $envFile -PathType Leaf)) {
        foreach ($line in [System.IO.File]::ReadLines($envFile)) {
            if ($line -match '^\s*AUTODUB_GEMINI_API_KEY\s*=\s*(.*?)\s*$') {
                $geminiKey = $Matches[1]
                if ($geminiKey.Length -ge 2 -and (($geminiKey.StartsWith('"') -and $geminiKey.EndsWith('"')) -or ($geminiKey.StartsWith("'") -and $geminiKey.EndsWith("'")))) {
                    $geminiKey = $geminiKey.Substring(1, $geminiKey.Length - 2)
                }
                break
            }
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($geminiKey)) { Set-ChildEnvironment "AUTODUB_GEMINI_API_KEY" $geminiKey }
    $geminiCredentials = [Environment]::GetEnvironmentVariable("AUTODUB_GEMINI_CREDENTIALS_JSON", "Process")
    if ([string]::IsNullOrWhiteSpace($geminiCredentials) -and (Test-Path -LiteralPath $envFile -PathType Leaf)) {
        foreach ($line in [System.IO.File]::ReadLines($envFile)) {
            if ($line -match '^\s*AUTODUB_GEMINI_CREDENTIALS_JSON\s*=\s*(.*?)\s*$') {
                $geminiCredentials = $Matches[1]
                if ($geminiCredentials.Length -ge 2 -and (($geminiCredentials.StartsWith('"') -and $geminiCredentials.EndsWith('"')) -or ($geminiCredentials.StartsWith("'") -and $geminiCredentials.EndsWith("'")))) {
                    $geminiCredentials = $geminiCredentials.Substring(1, $geminiCredentials.Length - 2)
                }
                break
            }
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($geminiCredentials)) { Set-ChildEnvironment "AUTODUB_GEMINI_CREDENTIALS_JSON" $geminiCredentials }

    $stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    $backendOut = Join-Path $runLogRoot "backend-$stamp.out.log"
    $backendErr = Join-Path $runLogRoot "backend-$stamp.err.log"
    $frontendOut = Join-Path $runLogRoot "frontend-$stamp.out.log"
    $frontendErr = Join-Path $runLogRoot "frontend-$stamp.err.log"

    $backendProcess = Start-Process -FilePath $pythonPath `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", $hostAddress, "--port", [string]$backendPort) `
        -WorkingDirectory $backendRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr

    $nodePath = (Get-Command node).Source
    $frontendProcess = Start-Process -FilePath $nodePath `
        -ArgumentList @($viteCli, "--host", $hostAddress, "--port", [string]$frontendPort, "--strictPort") `
        -WorkingDirectory $frontendRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $frontendOut -RedirectStandardError $frontendErr

    Add-Type -AssemblyName System.Net.Http
    $httpClient = [System.Net.Http.HttpClient]::new()
    $backendReady = $false
    $frontendReady = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        $backendProcess.Refresh()
        $frontendProcess.Refresh()
        if ($backendProcess.HasExited) { throw "Backend exited during startup. See $backendErr" }
        if ($frontendProcess.HasExited) { throw "Frontend exited during startup. See $frontendErr" }
        if (-not $backendReady) {
            $backendReady = Test-HttpStatus "http://${hostAddress}:${backendPort}/api/v1/health" $allowedOrigin "same-origin" 401
        }
        if (-not $frontendReady) {
            $frontendReady = Test-HttpStatus "${allowedOrigin}/" $allowedOrigin "same-origin" 200
        }
        if ($backendReady -and $frontendReady) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $backendReady -or -not $frontendReady) {
        throw "Backend/frontend chưa sẵn sàng trong 30 giây. Xem log trong .dev-run."
    }

    if ($SmokeTest) {
        $oldSmokeSecret = [Environment]::GetEnvironmentVariable("AUTODUB_SMOKE_BOOTSTRAP_SECRET", "Process")
        try {
            $env:AUTODUB_SMOKE_BOOTSTRAP_SECRET = $secret
            Push-Location $frontendRoot
            try {
                & node --test tests/smoke.test.mjs
                if ($LASTEXITCODE -ne 0) { throw "Frontend/API smoke tests thất bại với exit code $LASTEXITCODE." }
            }
            finally { Pop-Location }
        }
        finally { [Environment]::SetEnvironmentVariable("AUTODUB_SMOKE_BOOTSTRAP_SECRET", $oldSmokeSecret, "Process") }
        Write-Host "Development integration smoke tests: PASS"
        exit 0
    }

    $sessionUrl = "${allowedOrigin}/#bootstrap=$secret"
    if (-not $NoBrowser) { Start-Process -FilePath $sessionUrl | Out-Null }
    Write-Host "AutoDub Local dev đang chạy tại $allowedOrigin"
    Write-Host "Dừng bằng Ctrl+C. API liveness: $allowedOrigin/api/v1/health (cần session)."

    while ($true) {
        Start-Sleep -Seconds 1
        $backendProcess.Refresh()
        $frontendProcess.Refresh()
        if ($backendProcess.HasExited) { throw "Backend đã dừng. Xem $backendErr" }
        if ($frontendProcess.HasExited) { throw "Frontend đã dừng. Xem $frontendErr" }
    }
}
finally {
    if ($null -ne $httpClient) { $httpClient.Dispose() }
    Stop-ChildProcess $frontendProcess
    Stop-ChildProcess $backendProcess
    Restore-ChildEnvironment
}
