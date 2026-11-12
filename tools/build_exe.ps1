# 批量混剪：打包成 exe（由「打包exe.bat」调用）。需要先运行过一次「双击运行.bat」
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location -LiteralPath $Root
$VenvPy = Join-Path $Root '.venv\Scripts\python.exe'
$FfDir  = Join-Path $Root 'ffmpeg'

function Fail($msg) { Write-Host "出错了：$msg" -ForegroundColor Red; Read-Host '按回车键关闭'; exit 1 }

if (-not (Test-Path -LiteralPath $VenvPy)) { Fail '请先双击「双击运行.bat」完成安装，再来打包' }

# ffmpeg 必须在 ffmpeg 文件夹里才能打包进去；如果只在系统 PATH 里，就复制一份过来
foreach ($n in @('ffmpeg', 'ffprobe')) {
    $local = Join-Path $FfDir "$n.exe"
    if (-not (Test-Path -LiteralPath $local)) {
        $cmd = Get-Command $n -ErrorAction SilentlyContinue
        if (-not $cmd) { Fail "没有找到 $n.exe，请先放进 ffmpeg 文件夹" }
        New-Item -ItemType Directory -Force -Path $FfDir | Out-Null
        Copy-Item -LiteralPath $cmd.Source -Destination $local -Force
    }
}

Write-Host '==> 安装 PyInstaller' -ForegroundColor Cyan
& $VenvPy -m pip install --disable-pip-version-check -r (Join-Path $Root 'requirements-build.txt')
if ($LASTEXITCODE -ne 0) {
    & $VenvPy -m pip install --disable-pip-version-check -r (Join-Path $Root 'requirements-build.txt') -i 'https://pypi.tuna.tsinghua.edu.cn/simple'
}

Write-Host '==> 打包中（1~3 分钟）' -ForegroundColor Cyan
& $VenvPy -m PyInstaller --noconfirm --clean --onedir --windowed --name BatchMixCut `
    --add-binary "ffmpeg\ffmpeg.exe;ffmpeg" `
    --add-binary "ffmpeg\ffprobe.exe;ffmpeg" `
    main.py
if ($LASTEXITCODE -ne 0) { Fail '打包失败，请把上面的报错发给我' }

$dist = Join-Path $Root 'dist\BatchMixCut'
Write-Host ''
Write-Host "打包完成：$dist\BatchMixCut.exe" -ForegroundColor Green
Write-Host '整个 BatchMixCut 文件夹一起拷贝，双击里面的 BatchMixCut.exe 即可（不需要装 Python 和 ffmpeg）'
Start-Process explorer.exe $dist
Read-Host '按回车键关闭'
