# Agents Web installer for Windows 10 and 11. In PowerShell:
#
#   irm https://raw.githubusercontent.com/gabrielmf1998/Agents-Web/main/install.ps1 | iex
#   & ([scriptblock]::Create((irm https://gitlab.com/gabriel17166/agents-web/-/raw/main/install.ps1))) -GitLab
#
# Downloads the installer of the latest release, checks it against the release's SHA256SUMS and installs it
# for your user (no administrator rights). Running it again updates Agents Web; it also updates by itself.
# https://github.com/gabrielmf1998/Agents-Web (MIT license)
param([switch]$GitLab, [string]$Version = "", [switch]$NoStart)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Say($t) { Write-Host "==> $t" -ForegroundColor Cyan }

if ($env:AGENTS_WEB_RELEASES_API) { $api = $env:AGENTS_WEB_RELEASES_API }
elseif ($GitLab) {
    $api = if ($Version) { "https://gitlab.com/api/v4/projects/gabriel17166%2Fagents-web/releases/v$Version" }
           else { "https://gitlab.com/api/v4/projects/gabriel17166%2Fagents-web/releases/permalink/latest" }
} else {
    $api = if ($Version) { "https://api.github.com/repos/gabrielmf1998/Agents-Web/releases/tags/v$Version" }
           else { "https://api.github.com/repos/gabrielmf1998/Agents-Web/releases/latest" }
}
$rel = Invoke-RestMethod -Uri $api -Headers @{ "User-Agent" = "agents-web" }
$tag = $rel.tag_name
$assets = @{}
if ($rel.assets -is [array]) { foreach ($a in $rel.assets) { $assets[$a.name] = $a.browser_download_url } }
elseif ($rel.assets.links) { foreach ($a in $rel.assets.links) { $assets[$a.name] = if ($a.direct_asset_url) { $a.direct_asset_url } else { $a.url } } }
$name = "AgentsWeb-Setup-$($tag.TrimStart('v')).exe"
if (-not $assets[$name]) { throw "release $tag has no $name" }
if (-not $assets["SHA256SUMS"]) { throw "release $tag has no SHA256SUMS, refusing to install an unchecked file" }

$dir = Join-Path ([IO.Path]::GetTempPath()) ("agents-web-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $dir | Out-Null
try {
    $exe = Join-Path $dir $name
    Say "Downloading $name"
    Invoke-WebRequest -Uri $assets[$name] -OutFile $exe -UseBasicParsing -Headers @{ "User-Agent" = "agents-web" }
    $sums = (Invoke-WebRequest -Uri $assets["SHA256SUMS"] -UseBasicParsing -Headers @{ "User-Agent" = "agents-web" }).Content
    if ($sums -is [byte[]]) { $sums = [Text.Encoding]::UTF8.GetString($sums) }
    $line = ($sums -split "`n") | Where-Object { ($_ -split '\s+')[1] -replace '^\*', '' -eq $name } | Select-Object -First 1
    if (-not $line) { throw "$name is not listed in SHA256SUMS" }
    $expected = ($line -split '\s+')[0].ToLower()
    $actual = (Get-FileHash -Algorithm SHA256 -Path $exe).Hash.ToLower()
    if ($actual -ne $expected) { throw "$name does not match its SHA256 sum, nothing was installed" }
    Say "SHA256 checked"
    $instArgs = @("/S")
    if (-not $NoStart) { $instArgs += "/START" }
    Say "Installing Agents Web $($tag.TrimStart('v'))"
    $p = Start-Process -FilePath $exe -ArgumentList $instArgs -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "the installer failed with code $($p.ExitCode)" }
} finally {
    Remove-Item -Recurse -Force $dir -ErrorAction SilentlyContinue
}
Say "Installed in $env:LOCALAPPDATA\Programs\Agents Web. Open 'Agents Web' from the Start menu: http://127.0.0.1:47830/"
if (-not (Get-Command claude -ErrorAction SilentlyContinue) -and -not (Test-Path "$HOME\.local\bin\claude.exe")) {
    Write-Host "!! Claude Code CLI not found. Install it with:  irm https://claude.ai/install.ps1 | iex" -ForegroundColor Yellow
}
