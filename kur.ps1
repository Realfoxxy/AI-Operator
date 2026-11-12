# Orkestra kurulum yardımcısı (Windows PowerShell)
#
# Çalıştırma:  powershell -ExecutionPolicy Bypass -File .\kur.ps1
#
# Ne yapar:
#   1. Python 3.11+ ve Node.js/npm varlığını denetler.
#   2. Eksik resmi CLI araçlarını npm ile kurmayı ÖNERİR; onay vermezsen hiçbir şey kurmaz.
#   3. Varsayılan çalışma klasörünü (..\orkestra-calisma) oluşturur.
#   4. Testleri ve "doctor" denetimini çalıştırır.
# İnternetten betik indirip çalıştırmaz; Orkestra'nın kendisi hiçbir Python paketi gerektirmez.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$py = $null
foreach ($c in @("py -3", "python", "python3")) {
    $parts = $c.Split(" ")
    $exe = $parts[0]
    $args0 = @($parts | Select-Object -Skip 1)
    if (Get-Command $exe -ErrorAction SilentlyContinue) {
        & $exe @args0 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $py = @($exe) + $args0; break }
    }
}
if (-not $py) {
    Write-Host "✖ Python 3.11 veya üstü bulunamadı. Kur: https://www.python.org/downloads/ (kurulumda 'Add to PATH' işaretle)"
    exit 1
}
$pyExe = $py[0]
$pyArgs = @($py | Select-Object -Skip 1)
Write-Host ("✔ Python: " + (& $pyExe @pyArgs --version))

$haveNpm = [bool](Get-Command npm -ErrorAction SilentlyContinue)
if ($haveNpm) { Write-Host ("✔ Node.js: " + (node --version) + " · npm: " + (npm --version)) }
else { Write-Host "⚠ Node.js/npm bulunamadı. CLI araçları için Node.js LTS kur: https://nodejs.org/" }

$missing = @()
foreach ($pair in @(@("claude", "@anthropic-ai/claude-code"), @("gemini", "@google/gemini-cli"), @("copilot", "@github/copilot"))) {
    if (Get-Command $pair[0] -ErrorAction SilentlyContinue) { Write-Host ("✔ " + $pair[0] + " kurulu") }
    else { Write-Host ("• " + $pair[0] + " kurulu değil (" + $pair[1] + ")"); $missing += $pair[1] }
}

if ($missing.Count -gt 0 -and $haveNpm) {
    $ans = Read-Host ("Eksik CLI araçları kurulsun mu? (npm install -g " + ($missing -join " ") + ") [e/H]")
    if ($ans -match '^(e|evet|y|yes)$') { npm install -g @missing }
    else { Write-Host ("Atlandı. Daha sonra: npm install -g " + ($missing -join " ")) }
}

$ws = Join-Path (Split-Path -Parent $PSScriptRoot) "orkestra-calisma"
New-Item -ItemType Directory -Force -Path $ws | Out-Null
Write-Host ("✔ Çalışma klasörü: " + $ws)

Write-Host ""
Write-Host "Testler çalıştırılıyor…"
& $pyExe @pyArgs -m unittest -q
if ($LASTEXITCODE -eq 0) { Write-Host "✔ Testler geçti" } else { Write-Host "✖ Bazı testler başarısız oldu" }

Write-Host ""
& $pyExe @pyArgs -m orkestra doctor

$pyShown = ($py -join " ")
Write-Host @"

Sonraki adımlar:
  1. Her CLI'ı bir kez açıp giriş yap:  claude   ·   gemini   ·   copilot  (içeride /login)
  2. Copilot'un çalışma klasörüne güvenmesi için:  cd $ws ; copilot
  3. Abonelik harcamadan dene:          $pyShown -m orkestra ui --mock
  4. Gerçek çalıştırma:                 $pyShown -m orkestra ui

Not: PowerShell "running scripts is disabled" derse:  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
"@
