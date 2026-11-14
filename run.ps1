<#
Exam paper OCR kit. Installs everything into THIS folder and runs the OCR.

  Sample (default): tries each model on the first pages of 8 sample PDFs,
                    then writes results_sample.zip to send back.
      .\run.ps1
  Full run, after a model is chosen (resumable: finished PDFs are skipped):
      .\run.ps1 -Mode full -Models chandra

Nothing is installed system-wide. Delete the folder to remove everything.
#>
param(
    [string[]]$Models = @('chandra', 'dots', 'qwen3vl8', 'qwen3vl32'),
    [ValidateSet('sample', 'full', 'download')][string]$Mode = 'sample',
    [ValidateSet('vulkan', 'cpu')][string]$Backend = 'vulkan',
    [string]$Pages = '1-5',
    [int]$MaxFiles = 0,
    [int]$Dpi = 150,
    [int]$Port = 1234,
    [int]$GpuLayers = 99,
    [int]$TimeoutSeconds = 900,
    [string]$CustomList = ''
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is very slow with the progress bar
$root = $PSScriptRoot
$tools = Join-Path $root 'tools'
$modelsDir = Join-Path $root 'models'
$backendDir = Join-Path $root 'backend'
$raw = Join-Path $backendDir 'data\raw_papers'
$logs = Join-Path $root 'logs'
New-Item -ItemType Directory -Force $tools, $modelsDir, $logs | Out-Null

# name -> Hugging Face repo, model file, vision file
$catalog = @{
    chandra   = @('SandLogicTechnologies/chandra-ocr-2-GGUF', 'chandra-ocr-2_Q6_K.gguf', 'chandra-ocr-2.mmproj-f16.gguf')
    dots      = @('SandLogicTechnologies/dots.mocr-GGUF', 'dots.mocr-IQ4_NL.gguf', 'mmproj-dotsmocr-bf16.gguf')
    qwen3vl8  = @('Qwen/Qwen3-VL-8B-Instruct-GGUF', 'Qwen3VL-8B-Instruct-Q8_0.gguf', 'mmproj-Qwen3VL-8B-Instruct-F16.gguf')
    qwen3vl32 = @('Qwen/Qwen3-VL-32B-Instruct-GGUF', 'Qwen3VL-32B-Instruct-Q4_K_M.gguf', 'mmproj-Qwen3VL-32B-Instruct-F16.gguf')
    tiny      = @('Qwen/Qwen3-VL-2B-Instruct-GGUF', 'Qwen3VL-2B-Instruct-Q4_K_M.gguf', 'mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf')
}
foreach ($m in $Models) { if (-not $catalog.ContainsKey($m)) { throw "Unknown model '$m'. Choose from: $($catalog.Keys -join ', ')" } }
if ($Mode -eq 'full' -and $Models.Count -ne 1) { throw 'Full mode needs exactly one model, e.g. -Mode full -Models chandra' }

function Get-File($url, $dest) {
    # curl resumes a broken download; a finished file is left alone.
    if (Test-Path "$dest.done") { return }
    Write-Host "  downloading $(Split-Path $dest -Leaf)"
    & "$env:SystemRoot\System32\curl.exe" -L --fail --retry 5 -C - -# -o $dest $url
    if ($LASTEXITCODE -ne 0) { throw "Download failed: $url" }
    Set-Content "$dest.done" 'ok'
}

function Expand-Zip($zip, $dest) {
    # Windows' own tar reads zip files; Expand-Archive breaks on some PowerShell setups.
    New-Item -ItemType Directory -Force $dest | Out-Null
    & "$env:SystemRoot\System32\tar.exe" -xf $zip -C $dest
    if ($LASTEXITCODE -ne 0) { throw "Could not unpack $zip" }
}

Write-Host '[1/4] Python environment'
$uv = Join-Path $tools 'uv\uv.exe'
if (-not (Test-Path $uv)) {
    Get-File 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' (Join-Path $tools 'uv.zip')
    Expand-Zip (Join-Path $tools 'uv.zip') (Join-Path $tools 'uv')
}
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    & $uv venv --python 3.12 (Join-Path $root '.venv'); if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment' }
}
& $uv pip install --quiet --python $py pymupdf requests; if ($LASTEXITCODE -ne 0) { throw 'Could not install Python libraries' }

if ($Mode -ne 'sample') {
    # The repo carries only the list of papers; the PDFs come from their official sites.
    Write-Host '[papers] fetching the scanned papers (resumable)'
    $env:PYTHONUTF8 = '1'
    & $py (Join-Path $root 'download_papers.py')
    if ($Mode -eq 'download') { exit 0 }
}

Write-Host "[2/4] llama.cpp server ($Backend)"
$llamaDir = Join-Path $tools "llama-$Backend"
$server = Get-ChildItem $llamaDir -Recurse -Filter 'llama-server.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $server) {
    $releases = Invoke-RestMethod 'https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=10' -Headers @{ 'User-Agent' = 'exam-ocr-kit' }
    $asset = $releases | ForEach-Object { $_.assets } | Where-Object { $_.name -like "llama-*-bin-win-$Backend-x64.zip" } | Select-Object -First 1
    if (-not $asset) { throw "No llama.cpp Windows $Backend build found in the latest releases" }
    Get-File $asset.browser_download_url (Join-Path $tools $asset.name)
    Expand-Zip (Join-Path $tools $asset.name) $llamaDir
    $server = Get-ChildItem $llamaDir -Recurse -Filter 'llama-server.exe' | Select-Object -First 1
}

$listFile = Join-Path $raw $(if ($Mode -eq 'sample') { 'ocr_sample.txt' } else { 'ocr_queue.txt' })
if ($CustomList) { $listFile = (Resolve-Path $CustomList).Path }
if ($MaxFiles -gt 0) {
    $short = Join-Path $logs 'list_short.txt'
    # WriteAllLines writes UTF-8 without a byte-order mark; Set-Content on Windows PowerShell 5 adds one.
    [IO.File]::WriteAllLines($short, [string[]](Get-Content $listFile -TotalCount $MaxFiles -Encoding UTF8))
    $listFile = $short
}

$env:PYTHONUTF8 = '1'
$failed = @()
foreach ($name in $Models) {
    $repo, $modelFile, $visionFile = $catalog[$name]
    Write-Host "[3/4] Model '$name'"
    $dir = Join-Path $modelsDir $name
    New-Item -ItemType Directory -Force $dir | Out-Null
    Get-File "https://huggingface.co/$repo/resolve/main/$modelFile" (Join-Path $dir $modelFile)
    Get-File "https://huggingface.co/$repo/resolve/main/$visionFile" (Join-Path $dir $visionFile)

    $serverArgs = "-m `"$(Join-Path $dir $modelFile)`" --mmproj `"$(Join-Path $dir $visionFile)`" --port $Port --host 127.0.0.1 -ngl $GpuLayers -c 16384 --alias $name"
    $proc = Start-Process $server.FullName -ArgumentList $serverArgs -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs "server-$name.out.log") -RedirectStandardError (Join-Path $logs "server-$name.err.log")
    try {
        $up = $false
        foreach ($i in 1..300) {
            if ($proc.HasExited) { break }
            try { if ((Invoke-WebRequest "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200) { $up = $true; break } } catch { }
            Start-Sleep 2
        }
        if (-not $up) { throw "The model server did not start. See logs\server-$name.err.log" }

        Write-Host "[4/4] OCR with '$name' ($Mode)"
        $ocrArgs = @((Join-Path $backendDir 'scripts\ocr_vlm.py'), '--list', $listFile, '--model', $name, '--api-url', "http://127.0.0.1:$Port/v1", '--dpi', $Dpi, '--timeout', $TimeoutSeconds)
        if ($Mode -eq 'sample') { $ocrArgs += @('--pages', $Pages, '--tag', $name) }
        & $py @ocrArgs
        if ($LASTEXITCODE -ne 0) { $failed += $name }
    }
    catch { Write-Host "  FAILED '$name': $_" -ForegroundColor Red; $failed += $name }
    finally { if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }; Start-Sleep 2 }
}

# Collect what has to travel back, keeping the folder structure.
$pattern = if ($Mode -eq 'sample') { '*.ocr.*.txt' } else { '*.pdf.ocr.txt' }
$stage = Join-Path $root "results_$Mode"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
$found = @(Get-ChildItem $raw -Recurse -Filter $pattern)
foreach ($f in $found) {
    $dest = Join-Path $stage $f.FullName.Substring($raw.Length + 1)
    New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
    Copy-Item -LiteralPath $f.FullName -Destination $dest
}
$report = Join-Path $raw 'ocr_vlm_report.csv'
if (Test-Path $report) { New-Item -ItemType Directory -Force $stage | Out-Null; Copy-Item $report $stage }
Copy-Item $logs (Join-Path $stage 'logs') -Recurse -Force
$zip = Join-Path $root "results_$Mode.zip"
if (Test-Path $zip) { Remove-Item $zip }
# Name the entries: a zip whose paths start with './' looks empty in Windows Explorer.
$items = @(Get-ChildItem $stage -Name)
& "$env:SystemRoot\System32\tar.exe" -a -cf $zip -C $stage @items
if ($LASTEXITCODE -ne 0) { throw 'Could not write the results zip' }
Remove-Item $stage -Recurse -Force

Write-Host ''
Write-Host "Done. $($found.Count) text files. Send back: $zip"
if ($failed) { Write-Host "Models with problems: $($failed -join ', ') (details are in the zip under logs)" -ForegroundColor Yellow; exit 1 }
