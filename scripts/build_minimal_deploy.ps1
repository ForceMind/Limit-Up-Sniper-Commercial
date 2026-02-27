param(
    [string]$OutputDir = "Minimal-Server-Deploy",
    [string]$ZipDir = "dist",
    [switch]$NoZip,
    [switch]$KeepRuntimeData
)

$ErrorActionPreference = "Stop"

function Ensure-PathExists {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        throw "缺少必要路径: $Path"
    }
}

function Copy-IfExists {
    param(
        [string]$Source,
        [string]$Destination,
        [switch]$Recurse
    )
    Ensure-PathExists -Path $Source
    if ($Recurse) {
        Copy-Item -Path $Source -Destination $Destination -Recurse -Force
    } else {
        Copy-Item -Path $Source -Destination $Destination -Force
    }
}

function Get-VersionInfo {
    param([string]$ProjectRoot)

    $backendVersion = ""
    $frontendVersion = ""

    $backendMain = Join-Path $ProjectRoot "backend/app/main.py"
    $frontendIndex = Join-Path $ProjectRoot "frontend/index.html"

    if (Test-Path $backendMain) {
        $content = Get-Content $backendMain -Raw -Encoding UTF8
        $m = [regex]::Match($content, 'SERVER_VERSION\s*=\s*"([^"]+)"')
        if ($m.Success) { $backendVersion = $m.Groups[1].Value.Trim() }
    }

    if (Test-Path $frontendIndex) {
        $content = Get-Content $frontendIndex -Raw -Encoding UTF8
        $m = [regex]::Match($content, "frontendVersion:\s*'([^']+)'")
        if ($m.Success) { $frontendVersion = $m.Groups[1].Value.Trim() }
    }

    $effective = if ($backendVersion) { $backendVersion } elseif ($frontendVersion) { $frontendVersion } else { "v0.0.0" }

    return @{
        Backend  = $backendVersion
        Frontend = $frontendVersion
        Effective = $effective
    }
}

function Write-MinimalReadme {
    param(
        [string]$Path,
        [string]$Version,
        [bool]$WithRuntimeData
    )

    $modeText = if ($WithRuntimeData) { "已包含当前运行数据（不含大缓存目录）" } else { "使用空白模板数据（适合新部署）" }

    $text = @"
# 最小部署包

- 版本: $Version
- 生成时间: $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
- 数据模式: $modeText

## 包含内容
- backend/app
- backend/requirements.txt
- backend/data（最小化）
- frontend（页面与静态资源）
- Server-Version（安装/更新脚本）

## 使用方式
1. 将本目录上传到服务器。
2. 执行 Server-Version/install.sh 进行安装。
3. 后续更新可执行 Server-Version/update.sh。
"@

    Set-Content -Path $Path -Value $text -Encoding UTF8
}

function Write-DataTemplates {
    param(
        [string]$DataDir,
        [switch]$OnlyIfMissing
    )

    $templates = @{
        "config.json" = "{}"
        "lhb_config.json" = '{"enabled":true,"days":3,"min_amount":20000000}'
        "watchlist.json" = "[]"
        "favorites.json" = "[]"
        "news_history.json" = "[]"
        "analysis_cache.json" = "{}"
        "ai_cache.json" = "{}"
        "user_accounts.json" = "{}"
        "referral_records.json" = '{"order_invites":{},"rewarded_invitees":{}}'
        "trial_fingerprints.json" = "{}"
        "seat_mappings.json" = "{}"
        "vip_seats.json" = "[]"
        "seat_profiles.json" = "{}"
    }

    foreach ($kv in $templates.GetEnumerator()) {
        $target = Join-Path $DataDir $kv.Key
        if ($OnlyIfMissing -and (Test-Path $target)) {
            continue
        }
        Set-Content -Path $target -Value $kv.Value -Encoding UTF8
    }
}

function Copy-StaticSeatFiles {
    param(
        [string]$ProjectRoot,
        [string]$DataDir
    )

    $sourceDataDir = Join-Path $ProjectRoot "backend/data"
    if (-not (Test-Path $sourceDataDir)) {
        return
    }

    $seedFiles = @(
        "seat_mappings.json",
        "vip_seats.json",
        "seat_profiles.json"
    )

    foreach ($name in $seedFiles) {
        $src = Join-Path $sourceDataDir $name
        $dst = Join-Path $DataDir $name
        if (Test-Path $src) {
            Copy-Item -Path $src -Destination $dst -Force
        }
    }
}

$root = Split-Path -Parent $PSScriptRoot
$dest = Join-Path $root $OutputDir
$zipRoot = Join-Path $root $ZipDir
$versionInfo = Get-VersionInfo -ProjectRoot $root

if ($versionInfo.Backend -and $versionInfo.Frontend -and ($versionInfo.Backend -ne $versionInfo.Frontend)) {
    Write-Warning "前后端版本号不一致: backend=$($versionInfo.Backend), frontend=$($versionInfo.Frontend)"
}

if (Test-Path $dest) {
    Remove-Item -Recurse -Force $dest
}
New-Item -ItemType Directory -Path $dest | Out-Null

Write-Host "[1/6] 复制后端核心文件..." -ForegroundColor Cyan
$backendDest = Join-Path $dest "backend"
New-Item -ItemType Directory -Path $backendDest | Out-Null
Copy-IfExists -Source (Join-Path $root "backend/app") -Destination (Join-Path $backendDest "app") -Recurse
Copy-IfExists -Source (Join-Path $root "backend/requirements.txt") -Destination (Join-Path $backendDest "requirements.txt")

Get-ChildItem -Path (Join-Path $backendDest "app") -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
Get-ChildItem -Path (Join-Path $backendDest "app") -Recurse -File -Filter "*.pyc" -ErrorAction SilentlyContinue | Remove-Item -Force

Write-Host "[2/6] 处理 backend/data..." -ForegroundColor Cyan
$dataDest = Join-Path $backendDest "data"
New-Item -ItemType Directory -Path $dataDest | Out-Null

if ($KeepRuntimeData) {
    $dataSource = Join-Path $root "backend/data"
    Ensure-PathExists -Path $dataSource
    $excludeDirs = @("kline_cache", "kline_day_cache")
    Get-ChildItem -Path $dataSource -Force | ForEach-Object {
        if ($_.PSIsContainer) {
            if ($excludeDirs -contains $_.Name) { return }
            Copy-Item -Path $_.FullName -Destination (Join-Path $dataDest $_.Name) -Recurse -Force
        } else {
            Copy-Item -Path $_.FullName -Destination (Join-Path $dataDest $_.Name) -Force
        }
    }
    Write-DataTemplates -DataDir $dataDest -OnlyIfMissing
} else {
    Write-DataTemplates -DataDir $dataDest
}
Copy-StaticSeatFiles -ProjectRoot $root -DataDir $dataDest

Write-Host "[3/6] 复制前端文件..." -ForegroundColor Cyan
$frontendDest = Join-Path $dest "frontend"
New-Item -ItemType Directory -Path $frontendDest | Out-Null

$frontendFiles = @("index.html", "lhb.html", "help.html", "config.js")
foreach ($name in $frontendFiles) {
    Copy-IfExists -Source (Join-Path $root ("frontend/" + $name)) -Destination (Join-Path $frontendDest $name)
}
Copy-IfExists -Source (Join-Path $root "frontend/static") -Destination (Join-Path $frontendDest "static") -Recurse
New-Item -ItemType Directory -Path (Join-Path $frontendDest "admin") | Out-Null
Copy-IfExists -Source (Join-Path $root "frontend/admin/index.html") -Destination (Join-Path $frontendDest "admin/index.html")

Write-Host "[4/6] 复制服务端部署脚本..." -ForegroundColor Cyan
$serverDest = Join-Path $dest "Server-Version"
New-Item -ItemType Directory -Path $serverDest | Out-Null
$deployFiles = @("install.sh", "update.sh", "fix_server.sh", "run.py", "Readme_Deploy.md")
foreach ($f in $deployFiles) {
    Copy-IfExists -Source (Join-Path $root ("Server-Version/" + $f)) -Destination (Join-Path $serverDest $f)
}

Write-Host "[5/6] 写入说明与构建清单..." -ForegroundColor Cyan
Write-MinimalReadme -Path (Join-Path $dest "README_MINIMAL.md") -Version $versionInfo.Effective -WithRuntimeData ([bool]$KeepRuntimeData)

$allFiles = Get-ChildItem -Path $dest -Recurse -File
$manifest = [ordered]@{
    build_time = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss K")
    source_root = $root
    output_dir = $dest
    backend_version = $versionInfo.Backend
    frontend_version = $versionInfo.Frontend
    effective_version = $versionInfo.Effective
    keep_runtime_data = [bool]$KeepRuntimeData
    file_count = $allFiles.Count
    total_size_bytes = ($allFiles | Measure-Object -Property Length -Sum).Sum
}

$manifest | ConvertTo-Json -Depth 6 | Set-Content -Path (Join-Path $dest "build-manifest.json") -Encoding UTF8

$zipPath = $null
if (-not $NoZip) {
    Write-Host "[6/6] 生成压缩包..." -ForegroundColor Cyan
    if (-not (Test-Path $zipRoot)) {
        New-Item -ItemType Directory -Path $zipRoot | Out-Null
    }
    $zipName = "minimal-server-deploy-$($versionInfo.Effective)-$(Get-Date -Format 'yyyyMMdd_HHmmss').zip"
    $zipPath = Join-Path $zipRoot $zipName
    if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
    $zipOk = $false
    for ($i = 1; $i -le 5; $i++) {
        try {
            Compress-Archive -Path (Join-Path $dest "*") -DestinationPath $zipPath -CompressionLevel Optimal
            $zipOk = $true
            break
        } catch {
            if ($i -lt 5) {
                Start-Sleep -Milliseconds 600
            } else {
                Write-Warning "压缩失败（文件被占用），已跳过 zip 输出：$($_.Exception.Message)"
            }
        }
    }
    if (-not $zipOk) {
        $zipPath = $null
    }
}

Write-Host ""
Write-Host "最小部署包构建完成" -ForegroundColor Green
Write-Host "目录: $dest"
if ($zipPath) {
    Write-Host "压缩包: $zipPath"
}
Write-Host "版本: backend=$($versionInfo.Backend) / frontend=$($versionInfo.Frontend)"
Write-Host "文件数: $($manifest.file_count), 大小: $($manifest.total_size_bytes) bytes"
