# 使用 SignPath 免费签名（开源项目）

本项目已使用 MIT License，且仓库公开，可以申请 SignPath Foundation 的免费开源代码签名。
签名在 SignPath 的云端 HSM 中完成，不需要购买证书或 USB Token。

> SignPath 的开源计划需要人工审核；具体额度和流程以 SignPath 官网为准。

## 1. 申请与初始配置

1. 在 https://signpath.io/ 注册账号，申请 Open Source 项目（SignPath Foundation）。
2. 审核通过后创建组织（Organization）和项目（Project），项目 slug 例如 `eagent-studio`。
3. 创建签名策略（Signing Policy），例如 `release-signing`。
4. 创建构件配置（Artifact Configuration），例如 `windows-installer`，包含：
   - `EAgentStudio-Setup-*.exe`
   - `EAgentStudio.exe`
5. 在 GitHub 仓库安装 SignPath GitHub App，并授权目标仓库。
6. 在仓库 Secrets 中添加：
   - `SIGNPATH_API_TOKEN`
   - `SIGNPATH_ORGANIZATION_ID`

## 2. GitHub Actions 工作流模板

把下面的内容保存为 `.github/workflows/signpath.yml`，并替换尖括号中的 slug。
模板使用手动触发，避免未配置 Secrets 时影响普通 CI：

```yaml
name: signpath

on:
  workflow_dispatch:

jobs:
  sign:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: 安装打包依赖
        run: python -m pip install pyinstaller

      - name: 构建未签名产物
        run: |
          python -m PyInstaller --noconfirm --clean packaging/eagent.spec
          Compress-Archive -Path dist\EAgentStudio\* -DestinationPath dist\EAgentStudio-portable.zip -Force

      - name: 上传待签名产物
        id: upload
        uses: actions/upload-artifact@v4
        with:
          name: unsigned-windows
          path: |
            dist/EAgentStudio/EAgentStudio.exe
            dist/EAgentStudio-portable.zip

      - name: 提交 SignPath 签名请求
        uses: signpath/github-action-submit-signing-request@v1
        with:
          api-token: ${{ secrets.SIGNPATH_API_TOKEN }}
          organization-id: ${{ secrets.SIGNPATH_ORGANIZATION_ID }}
          project-slug: "<项目 slug>"
          signing-policy-slug: "release-signing"
          artifact-configuration-slug: "windows-installer"
          github-artifact-id: ${{ steps.upload.outputs.artifact-id }}
          wait-for-completion: true
          output-artifact-directory: "signed"

      - name: 上传已签名产物
        uses: actions/upload-artifact@v4
        with:
          name: signed-windows
          path: signed/
```

## 3. 校验签名

下载已签名产物后执行：

```powershell
Get-AuthenticodeSignature .\EAgentStudio.exe
Get-AuthenticodeSignature .\EAgentStudio-Setup-0.2.0.exe
```

状态应为 `Valid`，签名者显示为 SignPath Foundation 或项目配置的发布者。

## 4. 与现有 Release 流程结合

当前 Release 由本地 `build.ps1` 构建并手工上传。启用 SignPath 后，可以改为：

1. CI 构建未签名安装包；
2. SignPath 云端签名；
3. 下载已签名产物，再创建 GitHub Release。

如果暂时不接入 SignPath，至少保留 Release 中的 `.sha256` 校验文件，并在 README 说明 SmartScreen 的“更多信息 → 仍要运行”操作。
