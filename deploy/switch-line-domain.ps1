param(
    [Parameter(Mandatory = $true)]
    [string]$BaseUrl,

    [string]$ApiRoot = "http://127.0.0.1:8000",

    [switch]$SkipRichMenu
)

$ErrorActionPreference = "Stop"

function Set-EnvValue {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Key,
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Missing env file: $Path"
    }

    $content = Get-Content -LiteralPath $Path -Raw
    $pattern = "(?m)^" + [Regex]::Escape($Key) + "=.*$"
    $replacement = "${Key}=${Value}"

    if ([Regex]::IsMatch($content, $pattern)) {
        $updated = [Regex]::Replace($content, $pattern, $replacement)
    }
    else {
        $updated = $content.TrimEnd("`r", "`n") + "`r`n" + $replacement + "`r`n"
    }

    Set-Content -LiteralPath $Path -Value $updated -Encoding UTF8
}

if (-not $BaseUrl.StartsWith("https://")) {
    throw "BaseUrl must start with https://"
}

$normalizedBaseUrl = $BaseUrl.TrimEnd("/")
$projectRoot = Split-Path -Parent $PSScriptRoot
$envFiles = @(
    (Join-Path $projectRoot ".env"),
    (Join-Path $projectRoot "backend\\.env")
)

foreach ($file in $envFiles) {
    Set-EnvValue -Path $file -Key "PUBLIC_BASE_URL" -Value $normalizedBaseUrl
}

$health = Invoke-RestMethod -Method Get -Uri "$ApiRoot/api/line-management/status"
if (-not $health.configured.channel_access_token -or -not $health.configured.channel_secret) {
    throw "LINE channel credentials are missing in backend/.env"
}

$webhookPayload = @{
    base_url = $normalizedBaseUrl
    test_after_set = $true
} | ConvertTo-Json

$webhookResult = Invoke-RestMethod `
    -Method Post `
    -Uri "$ApiRoot/api/line-management/webhook/configure" `
    -ContentType "application/json" `
    -Body $webhookPayload

$richMenuResult = $null
if (-not $SkipRichMenu) {
    $richMenuPayload = @{
        base_url = $normalizedBaseUrl
    } | ConvertTo-Json

    $richMenuResult = Invoke-RestMethod `
        -Method Post `
        -Uri "$ApiRoot/api/line-management/richmenu/deploy" `
        -ContentType "application/json" `
        -Body $richMenuPayload
}

[PSCustomObject]@{
    base_url = $normalizedBaseUrl
    webhook_endpoint = $webhookResult.webhook_endpoint
    webhook_test = $webhookResult.test_result
    rich_menu = $richMenuResult
} | ConvertTo-Json -Depth 6
