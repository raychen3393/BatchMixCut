# 批量混剪：一键安装并运行（由「双击运行.bat」调用）
# 第一次：自动安装 Python（如果没有）、创建独立运行环境、安装 PySide6、下载 ffmpeg、创建桌面快捷方式
# 之后：检查都已通过，直接打开程序

# 注意：Windows PowerShell 5.1 下设为 Stop 时，pip 等外部程序输出警告会让脚本中断，所以用 Continue + 手动检查
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'   # 关闭进度条，下载会快很多
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location -LiteralPath $Root
$VenvPy  = Join-Path $Root '.venv\Scripts\python.exe'
$VenvPyw = Join-Path $Root '.venv\Scripts\pythonw.exe'
$FfDir   = Join-Path $Root 'ffmpeg'
$MainPy  = Join-Path $Root 'main.py'

function Step($msg) { Write-Host ''; Write-Host "==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    $msg" -ForegroundColor Yellow }

function Fail($msg) {
    Write-Host ''
    Write-Host "出错了：$msg" -ForegroundColor Red
    Write-Host ''
    Read-Host '按回车键关闭'
    exit 1
}

# ---------- 1. 找 Python（3.9 及以上） ----------
function Test-PythonCmd($exe, $extraArgs) {
    try {
        $args2 = @($extraArgs) + @('-c', 'import sys; print(sys.executable) if sys.version_info >= (3, 9) else sys.exit(1)')
        $out = & $exe @args2 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) {
            $p = ($out | Select-Object -Last 1).ToString().Trim()
            if (Test-Path -LiteralPath $p) { return $p }
        }
    } catch {}
    return $null
}

function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $p = Test-PythonCmd 'py' @('-3')
        if ($p) { return $p }
    }
    foreach ($name in @('python', 'python3')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        # 跳过 Windows 自带的「应用商店假 python」（WindowsApps 目录下的占位程序）
        if ($cmd -and ($cmd.Source -notlike '*\WindowsApps\*')) {
            $p = Test-PythonCmd $cmd.Source @()
            if ($p) { return $p }
        }
    }
    $dirs = @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles\Python*", "${env:ProgramFiles(x86)}\Python*")
    foreach ($d in $dirs) {
        $found = Get-ChildItem -Path $d -Filter python.exe -Recurse -Depth 2 -ErrorAction SilentlyContinue |
                 Sort-Object FullName -Descending
        foreach ($f in $found) {
            $p = Test-PythonCmd $f.FullName @()
            if ($p) { return $p }
        }
    }
    return $null
}

$firstRun = -not (Test-Path -LiteralPath $VenvPyw)

if (-not (Test-Path -LiteralPath $VenvPy)) {
    Step '检查 Python'
    $py = Find-Python
    if (-not $py) {
        Warn '没有找到 Python，正在自动安装 Python 3.12（只装给当前用户，不需要管理员权限）…'
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            & winget install -e --id Python.Python.3.12 --scope user --silent `
                --accept-package-agreements --accept-source-agreements
            $py = Find-Python
        }
        if (-not $py) {
            Start-Process 'https://www.python.org/downloads/windows/'
            Fail ("自动安装 Python 失败。已为你打开 Python 官网：请下载安装 Python 3.12，" +
                  "安装时勾选 「Add python.exe to PATH」，装好后再双击「双击运行.bat」。")
        }
    }
    Ok "使用 Python：$py"

    Step '创建独立运行环境（.venv）'
    & $py -m venv (Join-Path $Root '.venv')
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPy)) { Fail '创建运行环境失败' }
    Ok '完成'
}

# ---------- 2. 安装 PySide6 ----------
& $VenvPy -c 'import PySide6' 2>$null
if ($LASTEXITCODE -ne 0) {
    Step '安装界面组件 PySide6（约 100MB，第一次需要几分钟）'
    & $VenvPy -m pip install --disable-pip-version-check -r (Join-Path $Root 'requirements.txt')
    if ($LASTEXITCODE -ne 0) {
        Warn '官方源安装失败，改用国内镜像重试…'
        & $VenvPy -m pip install --disable-pip-version-check -r (Join-Path $Root 'requirements.txt') `
            -i 'https://pypi.tuna.tsinghua.edu.cn/simple'
    }
    & $VenvPy -c 'import PySide6' 2>$null
    if ($LASTEXITCODE -ne 0) { Fail '安装 PySide6 失败，请检查网络后重试' }
    Ok '完成'
}

# ---------- 3. 准备 ffmpeg ----------
$haveLocal = (Test-Path -LiteralPath (Join-Path $FfDir 'ffmpeg.exe')) -and
             (Test-Path -LiteralPath (Join-Path $FfDir 'ffprobe.exe'))
$haveOnPath = (Get-Command ffmpeg -ErrorAction SilentlyContinue) -and
              (Get-Command ffprobe -ErrorAction SilentlyContinue)
if (-not $haveLocal -and -not $haveOnPath) {
    Step '下载 ffmpeg（约 100MB）'
    New-Item -ItemType Directory -Force -Path $FfDir | Out-Null
    $urls = @(
        'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip',
        'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip'
    )
    $zip = Join-Path $env:TEMP 'batchmixcut_ffmpeg.zip'
    $tmp = Join-Path $env:TEMP 'batchmixcut_ffmpeg'
    $done = $false
    foreach ($u in $urls) {
        try {
            Ok "从 $u 下载…"
            Invoke-WebRequest -Uri $u -OutFile $zip -UseBasicParsing -ErrorAction Stop
            if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
            Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force -ErrorAction Stop
            $ff = Get-ChildItem -Path $tmp -Filter ffmpeg.exe -Recurse | Select-Object -First 1
            $fp = Get-ChildItem -Path $tmp -Filter ffprobe.exe -Recurse | Select-Object -First 1
            if ($ff -and $fp) {
                Copy-Item -LiteralPath $ff.FullName -Destination $FfDir -Force -ErrorAction Stop
                Copy-Item -LiteralPath $fp.FullName -Destination $FfDir -Force -ErrorAction Stop
                $done = $true
                break
            }
        } catch {
            Warn "下载失败：$($_.Exception.Message)"
        }
    }
    Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    if (-not $done) {
        Fail ("自动下载 ffmpeg 失败。请手动下载 ffmpeg（见 README.md 第 3 节），" +
              "把 ffmpeg.exe 和 ffprobe.exe 放进程序里的 ffmpeg 文件夹，再双击「双击运行.bat」。")
    }
    Ok '完成'
}

# ---------- 4. 桌面快捷方式（以后双击它就能打开，没有黑窗口） ----------
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop '批量混剪.lnk'
if ($firstRun -or -not (Test-Path -LiteralPath $lnk)) {
    try {
        $ws = New-Object -ComObject WScript.Shell -ErrorAction Stop
        $sc = $ws.CreateShortcut($lnk)
        $sc.TargetPath = $VenvPyw
        $sc.Arguments = '"' + $MainPy + '"'
        $sc.WorkingDirectory = $Root
        $sc.Description = '批量混剪'
        $sc.Save()
        Step '已在桌面创建快捷方式「批量混剪」，以后双击它即可打开'
    } catch {
        Warn "创建桌面快捷方式失败（不影响使用）：$($_.Exception.Message)"
    }
}

# ---------- 5. 启动 ----------
Step '启动批量混剪…'
Start-Process -FilePath $VenvPyw -ArgumentList ('"' + $MainPy + '"') -WorkingDirectory $Root
if ($firstRun) {
    Ok '安装完成！以后直接双击桌面上的「批量混剪」。这个窗口 5 秒后自动关闭。'
    Start-Sleep -Seconds 5
}
