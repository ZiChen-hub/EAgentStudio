# 构建 EAgent Studio：PyInstaller 目录包 + 可选 Inno Setup 安装程序
#
# 用法：
#   .\build.ps1                 # 只生成 dist\EAgentStudio\EAgentStudio.exe
#   .\build.ps1 -Installer      # 再生成 dist\installer\EAgentStudio-Setup-<版本>.exe
#   .\build.ps1 -Clean -Installer
#   .\build.ps1 -Installer -Sign  # 使用代码签名证书（需要环境变量，见 README）
#
# 代码签名环境变量：
#   EAGENT_SIGN_PFX        证书路径（.pfx）
#   EAGENT_SIGN_PASSWORD   证书密码（可选）
#   EAGENT_SIGN_TIMESTAMP  时间戳服务（可选，默认 http://timestamp.digicert.com）

param(
    [switch]$Clean,
    [switch]$Installer,
    [switch]$Sign
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$versionMatch = Select-String -Path "$Root\pyproject.toml" -Pattern '^version = "([^"]+)"'
if (-not $versionMatch) { throw "无法从 pyproject.toml 读取版本号" }
$Version = $versionMatch.Matches[0].Groups[1].Value

function Find-SignTool {
    $command = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $programFilesX86 = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFilesX86)
    $roots = @(
        (Join-Path $programFilesX86 "Windows Kits\10\bin"),
        (Join-Path $env:ProgramFiles "Windows Kits\10\bin")
    )
    foreach ($root in $roots) {
        if (-not (Test-Path $root)) { continue }
        $found = Get-ChildItem $root -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '\\x64\\' } |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    return $null
}

function Sign-File([string]$Path) {
    if (-not $Sign) { return }
    $pfx = $env:EAGENT_SIGN_PFX
    if (-not $pfx) { throw "使用 -Sign 需要设置 EAGENT_SIGN_PFX（证书路径）" }
    if (-not (Test-Path $pfx)) { throw "证书不存在：$pfx" }
    $tool = Find-SignTool
    if (-not $tool) { throw "未找到 signtool.exe，请安装 Windows SDK" }

    $signArgs = @("sign", "/fd", "SHA256", "/f", $pfx)
    if ($env:EAGENT_SIGN_PASSWORD) { $signArgs += @("/p", $env:EAGENT_SIGN_PASSWORD) }
    $timestamp = $env:EAGENT_SIGN_TIMESTAMP
    if (-not $timestamp) { $timestamp = "http://timestamp.digicert.com" }
    $signArgs += @("/tr", $timestamp, "/td", "SHA256", $Path)

    & $tool @signArgs
    if ($LASTEXITCODE -ne 0) { throw "签名失败：$Path" }
    Write-Host "已签名 $Path" -ForegroundColor Green
}

if ($Clean) {
    Remove-Item -Recurse -Force "$Root\build", "$Root\dist" -ErrorAction SilentlyContinue
}

python -m PyInstaller --noconfirm --clean packaging/eagent.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败" }

$exe = Join-Path $Root "dist\EAgentStudio\EAgentStudio.exe"
if (-not (Test-Path $exe)) { throw "没有找到打包产物：$exe" }
Sign-File $exe
Write-Host "已生成 $exe" -ForegroundColor Green

if ($Installer) {
    $iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
    if (-not $iscc) {
        $candidates = @(
            "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
            "C:\Program Files\Inno Setup 6\ISCC.exe"
        )
        $iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    }
    if (-not $iscc) {
        throw "未找到 Inno Setup 6（ISCC.exe），请先安装：https://jrsoftware.org/isdl.php"
    }
    & $iscc "packaging\eagent.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup 编译失败" }

    $setup = Join-Path $Root "dist\installer\EAgentStudio-Setup-$Version.exe"
    Sign-File $setup
    Write-Host "已生成 $setup" -ForegroundColor Green
}
