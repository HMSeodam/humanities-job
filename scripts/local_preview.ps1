$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$port = 8765
$url = "http://127.0.0.1:$port/"

if (Get-Command py -ErrorAction SilentlyContinue) {
    $python = "py"
    $arguments = @("-3", "-m", "http.server", "$port", "--bind", "127.0.0.1", "--directory", $projectRoot)
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = "python"
    $arguments = @("-m", "http.server", "$port", "--bind", "127.0.0.1", "--directory", $projectRoot)
} else {
    Write-Host "Python을 찾을 수 없습니다. Python 3을 설치한 뒤 다시 실행해 주세요." -ForegroundColor Red
    Read-Host "Enter 키를 누르면 닫힙니다"
    exit 1
}

try {
    $server = Start-Process -FilePath $python -ArgumentList $arguments -PassThru -WindowStyle Hidden
    Start-Sleep -Milliseconds 900
    Start-Process $url
    Write-Host ""
    Write-Host "인문잡을 로컬에서 열었습니다: $url" -ForegroundColor Green
    Write-Host "이 창을 닫거나 Enter 키를 누르면 로컬 실행이 종료됩니다."
    Read-Host
} finally {
    if ($server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    }
}
