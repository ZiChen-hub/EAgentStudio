# 构建 EAgent Studio：PyInstaller 目录包 + 可选 Inno Setup 安装程序
#
# 用法：
#   .\build.ps1                 # 只生成 dist\EAgentStudio\EAgentStudio.exe
#   .\build.ps1 -Installer      # 再生成 dist\installer\EAgentStudio-Setup-0.1.0.exe
#   .\build.ps1 -Clean -Installer

param(
    [switch]$Clean,
    [switch]$Installer
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if ($Clean) {
    Remove-Item -Recurse -Force "$Root\build", "$Root\dist" -ErrorAction SilentlyContinue
}

python -m PyInstaller --noconfirm --clean packaging/eagent.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败" }

$exe = Join-Path $Root "dist\EAgentStudio\EAgentStudio.exe"
if (-not (Test-Path $exe)) { throw "没有找到打包产物：$exe" }
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
    Write-Host "已生成 dist\installer\EAgentStudio-Setup-0.1.0.exe" -ForegroundColor Green
}
