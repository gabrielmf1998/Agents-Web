# Installs, uses, updates and removes the Windows build, on a throwaway Windows (the windows-latest runner of CI).
#   packaging\test-windows.ps1 -Dist dist      dist\ has the new AgentsWeb-Setup-*.exe, dist\old\ an older one
# A fake claude.exe (the fake CLI of tests/test_server.py) answers the chat; updates come from file:// releases.
param([string]$Dist = "dist")
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$script:fails = 0
function Ok($m) { Write-Host "PASS $m" -ForegroundColor Green }
function Bad($m) { Write-Host "FAIL $m" -ForegroundColor Red; $script:fails++ }
function Until([scriptblock]$cond, [int]$secs) {
    $end = (Get-Date).AddSeconds($secs)
    while ((Get-Date) -lt $end) { try { if (& $cond) { return $true } } catch {} ; Start-Sleep -Milliseconds 300 }
    return $false
}

$Dist = (Resolve-Path $Dist).Path
$new = (Get-ChildItem "$Dist\AgentsWeb-Setup-*.exe").FullName
$old = (Get-ChildItem "$Dist\old\AgentsWeb-Setup-*.exe").FullName
$newV = [regex]::Match($new, 'Setup-(.+)\.exe$').Groups[1].Value
$oldV = [regex]::Match($old, 'Setup-(.+)\.exe$').Groups[1].Value
$inst = "$env:LOCALAPPDATA\Programs\Agents Web"
$port = 47830
$base = "http://127.0.0.1:$port"
$env:HOME = $env:USERPROFILE

Write-Host "==> fake claude.exe"
$fake = Join-Path $env:RUNNER_TEMP "fakeclaude"
New-Item -ItemType Directory -Force "$fake\pkg" | Out-Null
python -c "import sys; sys.path.insert(0, 'tests'); import test_server; open(sys.argv[1], 'w', encoding='utf-8').write(test_server.FAKE_CLAUDE)" "$fake\pkg\fakeclaude.py"
Set-Content -Encoding utf8 "$fake\pkg\fakecli.py" "import runpy, os`ndef main():`n    runpy.run_path(os.path.join(os.path.dirname(__file__), 'fakeclaude.py'), run_name='__main__')"
Set-Content -Encoding utf8 "$fake\pkg\pyproject.toml" "[project]`nname = 'fakeclaude'`nversion = '0'`n[project.scripts]`nclaude = 'fakecli:main'`n[tool.setuptools]`npy-modules = ['fakecli']"
python -m venv "$fake\venv"
& "$fake\venv\Scripts\python.exe" -m pip install -q "$fake\pkg"
Copy-Item "$fake\pkg\fakeclaude.py" "$fake\venv\Lib\site-packages\fakeclaude.py"
$env:PATH = "$fake\venv\Scripts;$env:PATH"
if ((Get-Command claude).Source -like "*fakeclaude*") { Ok "fake claude.exe on PATH" } else { Bad "fake claude.exe" }

# the releases are served over http, like GitHub's
python packaging/fake-release.py "$Dist" "http://127.0.0.1:8765"
python packaging/fake-release.py "$Dist\old" "http://127.0.0.1:8765/old"
$web = Start-Process python -ArgumentList "-m", "http.server", "8765", "--bind", "127.0.0.1", "--directory", "`"$Dist`"" -PassThru -WindowStyle Hidden
Until { (Invoke-WebRequest "http://127.0.0.1:8765/release.json" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 } 20 | Out-Null
$env:AGENTS_WEB_RELEASES_API = "http://127.0.0.1:8765/release.json"
$env:AGENTS_WEB_UPDATE_DELAY = "3600"

Write-Host "==> silent install of $oldV"
$p = Start-Process $old -ArgumentList "/S" -Wait -PassThru
if ($p.ExitCode -eq 0 -and (Test-Path "$inst\AgentsWeb.exe") -and (Test-Path "$inst\python\pythonw.exe")) { Ok "installed $oldV in $inst" } else { Bad "install exit $($p.ExitCode)" }
$reg = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AgentsWeb"
if ($reg.DisplayVersion -eq $oldV) { Ok "Apps and features entry, version $oldV" } else { Bad "uninstall entry $($reg.DisplayVersion)" }
if (Test-Path "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Agents Web.lnk") { Ok "Start menu shortcut" } else { Bad "no Start menu shortcut" }
if ((Get-Content "$inst\app\install-kind") -eq "windows") { Ok "install-kind windows" } else { Bad "install-kind" }

Write-Host "==> start the tray"
Start-Process "$inst\AgentsWeb.exe"
if (Until { (Invoke-WebRequest "$base/saude" -UseBasicParsing -TimeoutSec 3).Content -like "ok*" } 60) { Ok "server answers on $port" }
else { Bad "server did not start"; Get-Content "$env:LOCALAPPDATA\agents-web\server.log" -ErrorAction SilentlyContinue }
$token = (Get-Content "$env:APPDATA\agents-web\config.json" -Raw | ConvertFrom-Json).token
$h = @{ "X-Token" = $token }
function Api($path, $body) {
    if ($null -ne $body) { Invoke-RestMethod "$base$path" -Method Post -Headers $h -ContentType "application/json" -Body ($body | ConvertTo-Json -Compress) -TimeoutSec 30 }
    else { Invoke-RestMethod "$base$path" -Headers $h -TimeoutSec 30 }
}
$e = Api "/api/estado"
if ($e.versao -eq $oldV) { Ok "page reports $oldV" } else { Bad "version $($e.versao)" }

Write-Host "==> a chat with the fake claude.exe"
$c = Api "/api/chats" @{ agente = "claude" }
Api "/api/chats/$($c.id)/enviar" @{ texto = "hello from windows" } | Out-Null
if (Until { $m = (Api "/api/chats/$($c.id)"); (-not $m.rodando) -and $m.mensagens[-1].texto -eq "echo: hello from windows" } 60) { Ok "reply came back through the CLI" }
else { Bad "no reply: $((Api "/api/chats/$($c.id)") | ConvertTo-Json -Depth 6 -Compress)" }
Api "/api/chats/$($c.id)/enviar" @{ texto = "again" } | Out-Null
if (Until { $m = (Api "/api/chats/$($c.id)"); (-not $m.rodando) -and $m.mensagens[-1].texto -eq "echo: again" } 60) { Ok "second message resumed the session" } else { Bad "second reply" }

Write-Host "==> update in the page to $newV"
$d = Api "/api/atualizacao?forcar=1"
if ($d.nova -and $d.tipo -eq "windows" -and $d.sozinha) { Ok "page sees $($d.ultima) for kind windows" } else { Bad "check: $($d | ConvertTo-Json -Compress)" }
Api "/api/atualizar" @{} | Out-Null
if (Until { (Api "/api/estado").versao -eq $newV } 180) { Ok "updated by itself to $newV, tray started again" }
else {
    Bad "still old after the update"
    Get-Content "$env:LOCALAPPDATA\agents-web\update.log" -ErrorAction SilentlyContinue
    Get-Content "$env:LOCALAPPDATA\agents-web\server.log" -ErrorAction SilentlyContinue
}
$reg = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AgentsWeb"
if ($reg.DisplayVersion -eq $newV) { Ok "Apps and features shows $newV" } else { Bad "uninstall entry $($reg.DisplayVersion)" }
$trays = @(Get-Process AgentsWeb -ErrorAction SilentlyContinue)
if ($trays.Count -eq 1) { Ok "one tray running" } else { Bad "$($trays.Count) trays running" }
$m = Api "/api/chats/$($c.id)"
if ($m.mensagens.Count -ge 4) { Ok "chat kept across the update" } else { Bad "chat lost" }

Write-Host "==> quit and uninstall"
Start-Process "$inst\AgentsWeb.exe" -ArgumentList "--quit" -Wait
if (Until { -not (Get-Process pythonw -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$inst*" }) } 20) { Ok "--quit stopped the server" } else { Bad "server left running" }
$p = Start-Process "$inst\Uninstall.exe" -ArgumentList "/S", "/PURGE" -Wait -PassThru
# the NSIS uninstaller copies itself to %TEMP% and returns at once
Until { -not (Test-Path "$inst\AgentsWeb.exe") } 60 | Out-Null
if (-not (Test-Path $inst) -and -not (Test-Path "$env:APPDATA\agents-web") -and -not (Test-Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AgentsWeb")) {
    Ok "uninstall /PURGE removed files, data and the Apps entry"
} else { Bad "left behind: $(Get-ChildItem $inst -ErrorAction SilentlyContinue)" }

Write-Host "==> install.ps1 in Windows PowerShell 5.1 (what irm | iex runs on most PCs), against the fake release"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File install.ps1 -NoStart
if ((Get-Content "$inst\app\server.py" | Select-String '^VERSAO = ').Line -like "*$newV*") { Ok "install.ps1 checked the SHA256 and installed $newV" } else { Bad "install.ps1" }
Start-Process "$inst\Uninstall.exe" -ArgumentList "/S", "/PURGE" -Wait
Until { -not (Test-Path "$inst\AgentsWeb.exe") } 60 | Out-Null

Stop-Process $web -ErrorAction SilentlyContinue
if ($script:fails) { Write-Host "$($script:fails) check(s) failed"; exit 1 }
Write-Host "all Windows checks passed"
