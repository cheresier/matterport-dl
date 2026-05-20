<#
.SYNOPSIS
    Packages downloaded Matterport models into self-contained distributable bundles.
.DESCRIPTION
    Builds matterport-dl.exe once using PyInstaller, then creates a self-contained zip
    archive per model under .\bundles\. Each zip contains the exe, support files, and
    all model data. The recipient extracts the zip and double-clicks Launch.bat to start
    the server and open the tour in their default browser. No Python required.
.PARAMETER ModelId
    Optional. Package only this model ID. Omit to package all models in .\downloads\.
.PARAMETER BindHost
    Host address the local server listens on. Default: 127.0.0.1
.PARAMETER BindPort
    Port the local server listens on. Default: 8080
.EXAMPLE
    .\package_model.ps1
    .\package_model.ps1 -ModelId nXa2VtHUZYa
    .\package_model.ps1 -ModelId nXa2VtHUZYa -BindPort 9090
#>
param(
    [string]$ModelId  = "",
    [string]$BindHost = "127.0.0.1",
    [int]   $BindPort = 8080
)

$ErrorActionPreference = "Stop"

$RepoRoot     = $PSScriptRoot
$DownloadsDir = Join-Path $RepoRoot "downloads"
$BundlesDir   = Join-Path $RepoRoot "bundles"
$DistDir      = Join-Path $RepoRoot "dist"
$BuildDir     = Join-Path $RepoRoot "build"
$VenvPython   = Join-Path $RepoRoot "venv\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Error "Virtual environment not found at $VenvPython -- run 'python run.py' once first to create it."
    exit 1
}

# Determine which models to package
if ($ModelId) {
    $models = @($ModelId)
} else {
    $models = Get-ChildItem -Path $DownloadsDir -Directory |
              Select-Object -ExpandProperty Name
}

if ($models.Count -eq 0) {
    Write-Host "No models found in $DownloadsDir"
    exit 0
}

Write-Host "Models to package: $($models -join ', ')"

# Install PyInstaller if missing
Write-Host "Checking PyInstaller..."
& $VenvPython -m pip show pyinstaller 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing PyInstaller..."
    & $VenvPython -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { Write-Error "Failed to install PyInstaller."; exit 1 }
}

# Find a favicon.ico from the first available model to use as the exe icon
$IconPath = ""
foreach ($m in $models) {
    $candidate = Join-Path $DownloadsDir "$m\favicon.ico"
    if (Test-Path $candidate) { $IconPath = $candidate; break }
}

# Build the exe once -- it is reused for every model bundle
Write-Host "Building executable with PyInstaller (this takes a few minutes)..."
Push-Location $RepoRoot

$CurlLibsDir = Join-Path $RepoRoot "venv\Lib\site-packages\curl_cffi.libs"

$PyiArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--onedir",
    "--console",
    "--name", "matterport-dl",
    "--contents-directory", "package",
    "--distpath", $DistDir,
    "--workpath", $BuildDir,
    "--specpath", $BuildDir,
    "--add-data", "$RepoRoot\graph_posts;graph_posts/",
    "--add-data", "$RepoRoot\JSNetProxy.js;.",
    "--add-data", "$CurlLibsDir;curl_cffi.libs/"
)
if ($IconPath) { $PyiArgs += "--icon"; $PyiArgs += $IconPath }
$PyiArgs += ".\matterport-dl.py"

& $VenvPython @PyiArgs
$buildResult = $LASTEXITCODE
Pop-Location

if ($buildResult -ne 0) { Write-Error "PyInstaller build failed."; exit 1 }

$ExeSource  = Join-Path $DistDir "matterport-dl"
$BrowserUrl = "http://" + $BindHost + ":" + $BindPort

# Downloads any missing JS chunk files, CSS chunk files, and static images that
# were not captured during the initial matterport-dl download run.
# JS/CSS chunks are lazy-loaded code-split bundles; the standard downloader only
# grabs named chunks. Static files like atlas.png are also sometimes missed.
function Ensure-ChunkFiles {
    param([string]$ModelDir)

    $jsDir    = Join-Path $ModelDir "js"
    $cssDir   = Join-Path $ModelDir "css"
    $imgDir   = Join-Path $ModelDir "images"
    $indexFile   = Join-Path $ModelDir "index.html"
    $runtimeFile = Join-Path $jsDir "runtime~showcase.js"

    if (-not (Test-Path $runtimeFile) -or -not (Test-Path $indexFile)) {
        Write-Warning "    Skipping chunk check: runtime~showcase.js or index.html not found"
        return
    }

    # Derive CDN base URL from the <base href> in index.html
    $baseHrefLine = Select-String -Path $indexFile -Pattern 'base href="([^"]+)"' | Select-Object -First 1
    if (-not $baseHrefLine) { Write-Warning "    Skipping chunk check: no <base href> found in index.html"; return }
    $cdnBase = $baseHrefLine.Matches[0].Groups[1].Value.TrimEnd('/')

    $runtimeContent = [System.IO.File]::ReadAllText($runtimeFile)

    # ---- JS CHUNKS -------------------------------------------------------
    # Named chunk IDs are already downloaded as human-readable files (e.g. 239 -> three-examples.js)
    $namedJsIds = [regex]::Matches($runtimeContent, '(\d+):"[a-z]') |
                  ForEach-Object { $_.Groups[1].Value }

    # Scan ALL downloaded JS files (including *.modified.js) for lazy-load calls.
    # Use 1-2 char variable names to cover typical webpack minification.
    $allJsIds = @()
    Get-ChildItem $jsDir -Filter "*.js" | ForEach-Object {
        $content = [System.IO.File]::ReadAllText($_.FullName)
        [regex]::Matches($content, '\b[a-zA-Z_$]{1,2}\.e\((\d+)\)') |
            ForEach-Object { $allJsIds += $_.Groups[1].Value }
    }
    $allJsIds = $allJsIds | Sort-Object { [int]$_ } -Unique

    $missingJs = $allJsIds | Where-Object {
        ($_ -notin $namedJsIds) -and (-not (Test-Path (Join-Path $jsDir "$_.js")))
    }

    # ---- CSS CHUNKS ------------------------------------------------------
    # Runtime embeds a CSS availability map: &&{229:1,4012:1,5385:1,...}[r]
    $cssAvailMatch = [regex]::Match($runtimeContent, '&&(\{(?:\d+:1,?)+\})\[')
    $allCssIds = @()
    if ($cssAvailMatch.Success) {
        $allCssIds = [regex]::Matches($cssAvailMatch.Groups[1].Value, '(\d+):1') |
                     ForEach-Object { $_.Groups[1].Value }
    }

    # Named CSS dict gives IDs that already have human-readable filenames
    $namedCssIds = @()
    $cssNamedMatch = [regex]::Match($runtimeContent, 'miniCssF[^{]*\{([^}]+)\}')
    if ($cssNamedMatch.Success) {
        $namedCssIds = [regex]::Matches($cssNamedMatch.Groups[1].Value, '(\d+):"') |
                       ForEach-Object { $_.Groups[1].Value }
    }

    $missingCss = $allCssIds | Where-Object {
        ($_ -notin $namedCssIds) -and (-not (Test-Path (Join-Path $cssDir "$_.css")))
    }

    # ---- STATIC FILES ----------------------------------------------------
    # atlas.png is a sprite sheet loaded at runtime; sometimes missed by the downloader
    $staticFiles = @("images/atlas.png")
    $missingStatic = $staticFiles | Where-Object { -not (Test-Path (Join-Path $ModelDir $_)) }

    # ---- DOWNLOAD --------------------------------------------------------
    # Helper: download a URL to a destination path, retrying up to 3 times on
    # transient failures. Returns $true on success, $false if all attempts fail
    # (e.g. genuine CDN 403/404).
    function Fetch-WithRetry {
        param([string]$Url, [string]$Dest, [string]$Dir)
        if ($Dir -and -not (Test-Path $Dir)) { New-Item -ItemType Directory -Path $Dir -Force | Out-Null }
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            try {
                Invoke-WebRequest -Uri $Url -OutFile $Dest -UseBasicParsing -TimeoutSec 30 -ErrorAction Stop | Out-Null
                return $true
            } catch {
                if (Test-Path $Dest) { Remove-Item $Dest -Force }
                if ($attempt -lt 3) { Start-Sleep -Milliseconds 500 }
            }
        }
        return $false
    }

    $totalMissing = $missingJs.Count + $missingCss.Count + $missingStatic.Count
    if ($totalMissing -eq 0) {
        Write-Host "    All referenced JS/CSS chunks and static files already present."
        return
    }

    Write-Host "    Fetching missing files: $($missingJs.Count) JS, $($missingCss.Count) CSS, $($missingStatic.Count) static..."
    $ok = 0; $fail = 0

    foreach ($id in $missingJs) {
        if (Fetch-WithRetry -Url "$cdnBase/js/$id.js" -Dest (Join-Path $jsDir "$id.js")) { $ok++ } else { $fail++ }
    }
    foreach ($id in $missingCss) {
        if (Fetch-WithRetry -Url "$cdnBase/css/$id.css" -Dest (Join-Path $cssDir "$id.css")) { $ok++ } else { $fail++ }
    }
    foreach ($rel in $missingStatic) {
        $dest = Join-Path $ModelDir $rel
        if (Fetch-WithRetry -Url "$cdnBase/$($rel -replace '\\','/')" -Dest $dest -Dir (Split-Path $dest -Parent)) { $ok++ } else { $fail++ }
    }
    Write-Host "    Downloaded: $ok  Unavailable (CDN blocked/removed): $fail"
}

# Create a self-contained bundle for each model
foreach ($model in $models) {
    $modelDir = Join-Path $DownloadsDir $model
    if (-not (Test-Path $modelDir)) {
        Write-Warning "Model folder not found: $modelDir -- skipping"
        continue
    }

    Write-Host "Creating bundle for: $model"
    $bundleDir = Join-Path $BundlesDir $model
    if (Test-Path $bundleDir) { Remove-Item $bundleDir -Recurse -Force }
    New-Item -ItemType Directory -Path $bundleDir | Out-Null

    # Ensure all lazy-loaded webpack chunk files are present in the source download folder.
    # This fetches any numeric chunks (e.g. 7941.js) that run.py missed during the download.
    Write-Host "  Checking for missing JS chunks..."
    Ensure-ChunkFiles -ModelDir $modelDir

    # Copy exe + package/ into bundle root
    Copy-Item -Path (Join-Path $ExeSource "*") -Destination $bundleDir -Recurse

    # Copy model files into downloads\MODEL_ID\ using robocopy.
    # robocopy handles paths longer than 260 chars (common in tile directories).
    # Exit codes 0-7 are all success variants; 8+ indicates an actual error.
    $bundleModelDir = Join-Path $bundleDir "downloads\$model"
    $null = robocopy $modelDir $bundleModelDir /E /NJH /NJS /XF "run_report.log"
    if ($LASTEXITCODE -ge 8) {
        Write-Error "robocopy failed with exit code $LASTEXITCODE for model $model"
        continue
    }

    # Create Launch.bat
    # How it works:
    #   cd /d "%~dp0"  sets CWD to the bundle root regardless of how the bat is launched.
    #   main() does os.chdir("./downloads") then startServer() does os.chdir(MODEL_ID),
    #   so the exe finds .\downloads\MODEL_ID\ relative to that bundle-root CWD.
    #   'start "" ...' opens the server in its own console window so the bat can exit.
    #   timeout + start URL opens the default browser after the server has had time to bind.
    $batLines = @(
        "@echo off",
        "cd /d ""%~dp0""",
        "start """" ""%~dp0matterport-dl.exe"" $model $BindHost $BindPort",
        "timeout /t 2 /nobreak >nul",
        "start $BrowserUrl"
    )
    $batContent = ($batLines -join "`r`n") + "`r`n"
    [System.IO.File]::WriteAllText(
        (Join-Path $bundleDir "Launch.bat"),
        $batContent,
        [System.Text.Encoding]::ASCII
    )

    Write-Host "  -> $bundleDir"

    # Zip the bundle and remove the staging folder.
    # The zip contains a top-level folder named after the model ID so
    # the recipient can extract anywhere and keep things organised.
    $zipPath = "$bundleDir.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Write-Host "  Compressing to $zipPath ..."
    Compress-Archive -Path $bundleDir -DestinationPath $zipPath
    Remove-Item $bundleDir -Recurse -Force
    Write-Host "  -> $zipPath"
}

# Clean up PyInstaller temporaries
if (Test-Path $DistDir)  { Remove-Item $DistDir  -Recurse -Force }
if (Test-Path $BuildDir) { Remove-Item $BuildDir -Recurse -Force }

Write-Host ""
Write-Host "Done. Bundles are in: $BundlesDir"
Write-Host "Share the MODEL_ID.zip file. Recipient extracts it and double-clicks Launch.bat."
