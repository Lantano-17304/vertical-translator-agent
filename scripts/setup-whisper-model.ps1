# 交互式配置 WHISPER_MODEL_PATH（检查本地模型文件并写入 .env）
param(
    [string]$ModelDir = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$EnvFile = Join-Path $Root ".env"
$Required = @("model.bin", "config.json", "tokenizer.json", "vocabulary.txt")

function Test-ModelDir([string]$Dir) {
    if (-not (Test-Path $Dir)) {
        Write-Host "目录不存在: $Dir" -ForegroundColor Red
        return $false
    }
    $missing = @()
    foreach ($name in $Required) {
        if (-not (Test-Path (Join-Path $Dir $name))) {
            $missing += $name
        }
    }
    if ($missing.Count -gt 0) {
        Write-Host "缺少文件: $($missing -join ', ')" -ForegroundColor Red
        Write-Host "请从 https://hf-mirror.com/Systran/faster-whisper-small 下载完整模型文件夹。" -ForegroundColor Yellow
        return $false
    }
    return $true
}

function Set-EnvWhisperPath([string]$Dir) {
    $normalized = $Dir.Trim().TrimEnd('\')
    $line = "WHISPER_MODEL_PATH=$normalized"
    if (-not (Test-Path $EnvFile)) {
        if (Test-Path (Join-Path $Root ".env.example")) {
            Copy-Item (Join-Path $Root ".env.example") $EnvFile
        } else {
            Set-Content -Path $EnvFile -Value $line -Encoding UTF8
            return
        }
    }
    $content = Get-Content $EnvFile -Raw -Encoding UTF8
    if ($content -match '(?m)^WHISPER_MODEL_PATH=.*$') {
        $content = [regex]::Replace($content, '(?m)^WHISPER_MODEL_PATH=.*$', $line)
    } else {
        if (-not $content.EndsWith("`n")) { $content += "`n" }
        $content += "$line`n"
    }
    Set-Content -Path $EnvFile -Value $content.TrimEnd() -Encoding UTF8
}

Write-Host ""
Write-Host "  Whisper 本地模型配置" -ForegroundColor Cyan
Write-Host "  项目目录: $Root"
Write-Host ""

if (-not $ModelDir) {
    $ModelDir = Read-Host "请输入模型文件夹路径 (例如 C:\models\faster-whisper-small)"
}
$ModelDir = $ModelDir.Trim().Trim('"')

if (-not (Test-ModelDir $ModelDir)) {
    exit 1
}

Set-EnvWhisperPath $ModelDir
Write-Host ""
Write-Host "已写入 .env : WHISPER_MODEL_PATH=$ModelDir" -ForegroundColor Green
Write-Host "请重启 start.bat 后，在页面查看 Whisper 状态是否为「已就绪」。" -ForegroundColor Green
Write-Host ""
