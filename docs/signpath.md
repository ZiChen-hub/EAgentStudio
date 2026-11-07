# SignPath 免费开源签名操作指南

本项目使用 MIT License、仓库公开、已有 v0.2.0 Release，符合 SignPath Foundation
免费开源签名的基本条件。签名在 SignPath 的云端 HSM 中完成，不需要购买证书或 USB Token。

> 开源计划需要人工审核，额度和审核标准以 SignPath 官方邮件为准。
> 本文按 2026 年的 SignPath 控制台整理，界面文案可能随版本调整。

## 0. 你会得到什么

- 安装包和主程序带 Authenticode 签名，`Get-AuthenticodeSignature` 显示 `Valid`
- 签名者显示为 SignPath Foundation（而不是“未知发布者”）
- 签名带可信时间戳，证书过期后已发布的版本仍然有效
- 私钥保存在云端 HSM，构建机拿不到私钥
- 源码不上传，SignPath 只接收 GitHub Actions 产出的构建产物和来源元数据

注意：免费签名不会立刻消除 SmartScreen 的“此应用不常见”提示。SmartScreen 还取决于
下载量和信誉积累，但签名后不会再显示“未知发布者”。

## 1. 申请前自查

SignPath 审核时会检查以下内容，本项目均已满足：

- [x] 使用 OSI 认可的许可证（本项目为 MIT，见 [LICENSE](../LICENSE)）
- [x] 仓库公开可访问（https://github.com/ZiChen-hub/EAgentStudio）
- [x] 项目处于活跃维护状态，有提交记录
- [x] 已有可下载的正式发布（v0.2.0 Release）
- [x] 不含专有组件、试用版组件或捆绑推广
- [x] 有基本文档（README、构建与安装说明）
- [x] 构建流程可复现（`build.ps1` + `packaging/eagent.spec`）

## 2. 提交申请

1. 打开申请页：https://signpath.org/apply
2. 下载并填写 `OSSRequestForm-v4.xlsx`（文件名带版本号，以页面实际提供的为准）
3. 准备以下信息（表单里会用到）：

   | 项目 | 本项目填写示例 |
   | --- | --- |
   | 项目名称 | EAgent Studio |
   | 仓库地址 | https://github.com/ZiChen-hub/EAgentStudio |
   | 许可证 | MIT |
   | 项目简介 | 本地桌面 AI 编码 Agent，通过 CDP 驱动网页版 AI |
   | 发布页 | https://github.com/ZiChen-hub/EAgentStudio/releases |
   | 维护者 | GitHub 账号 ZiChen-hub 及联系邮箱 |
   | 构建方式 | GitHub Actions（windows-latest）+ PyInstaller + Inno Setup |
   | 需要签名的文件 | `EAgentStudio.exe`、`EAgentStudio-Setup-*.exe` |

4. 把填好的表格发送到 `oss-support@signpath.org`
5. 等待审核邮件。审核期间可能会被要求补充项目说明或构建流程

审核通过后，SignPath 会为你开通组织（Organization），并邀请你登录
https://app.signpath.io/。

## 3. 审核通过后的控制台配置

以下步骤都在 https://app.signpath.io/ 中完成。建议按顺序操作，因为后面的配置会引用前面的对象。

### 3.1 确认组织

- 登录后确认左上角已选中你的组织（例如 `EAgent Studio`）
- 在 Organization 设置页找到 **Organization ID**（一串 GUID），后面要写入 GitHub Secrets

### 3.2 添加 GitHub.com Trusted Build System

SignPath 需要确认产物确实由 GitHub Actions 构建，而不是别人用 API Token 上传的。

1. 进入 Organization → **Trusted Build Systems**
2. 添加预定义系统 **GitHub.com**（不要选 GitHub Enterprise Server）
3. 打开要用于签名的 Project，在 Project 的 Trusted Build Systems 里关联 GitHub.com
4. 确认所有构建 job 都在 GitHub 托管的 runner 上执行（本项目用 `windows-latest`，满足要求）

### 3.3 创建 Project

1. Organization → **Projects** → **Create Project**
2. 名称填 `EAgent Studio`，slug 填 `eagent-studio`
3. 记下 slug，工作流里的 `project-slug` 要用

### 3.4 创建 Signing Policy

1. 进入 Project → **Signing Policies** → **Create Signing Policy**
2. 名称填 `Release signing`，slug 填 `release-signing`
3. 指定允许提交签名请求的用户或 API Token
4. 如果控制台提供 **Pipeline Policies**，建议启用：
   - `runners.require_github_hosted: true`，强制所有 job 使用 GitHub 托管 runner
   - 需要防止旧构建被重跑后签名时，可设置 `disallow_reruns: true`

### 3.5 创建 Artifact Configuration

本项目需要**两个**构件配置：主程序和安装包。原因是安装包会把主程序打包进去，
必须先签主程序，再生成安装包，最后签安装包；如果反过来，安装包里的程序仍然是未签名状态。

#### 3.5.1 主程序 `app-executable`

1. Project → **Artifact Configurations** → **Create**
2. 名称填 `App executable`，slug 填 `app-executable`
3. 选择 **Custom XML**，填入：

```xml
<artifact-configuration xmlns="http://signpath.io/artifact-configuration/v1">
  <zip-file>
    <pe-file path="EAgentStudio.exe">
      <authenticode-sign/>
    </pe-file>
  </zip-file>
</artifact-configuration>
```

#### 3.5.2 安装包 `windows-installer`

slug 填 `windows-installer`，XML：

```xml
<artifact-configuration xmlns="http://signpath.io/artifact-configuration/v1">
  <zip-file>
    <pe-file path="EAgentStudio-Setup-*.exe">
      <authenticode-sign/>
    </pe-file>
  </zip-file>
</artifact-configuration>
```

说明：

- 根元素必须是 `<zip-file>`，因为 `actions/upload-artifact` 默认把上传内容打包成 ZIP
- `path` 是 ZIP 内的相对路径，不是构建机上的路径
- 通配符 `*` 可用于版本号变化。如果控制台校验不接受通配符，就改成当前版本的实际文件名，
  例如 `EAgentStudio-Setup-0.2.0.exe`
- 只签名 PE 文件，不要对 ZIP 本身调用 `authenticode-sign`

## 4. 安装 SignPath GitHub App

1. 打开 https://github.com/apps/signpath
2. 点击 **Install**
3. 选择账号 `ZiChen-hub`
4. 选择 **Only select repositories**，勾选 `EAgentStudio`
5. 确认安装

这个 App 用于读取 workflow 和产物信息（审计日志评估）。公开仓库即使不装也能签名，
但装上可以避免后续权限问题。

## 5. 创建 API Token 并获取 Organization ID

### 5.1 API Token

1. 在 SignPath 右上角用户菜单进入 **API Tokens**（部分版本在 Settings 下）
2. **Create API Token**
3. 选择你的 Organization
4. 权限选择 **Submitter**（允许提交签名请求），不要给管理员权限
5. 创建后立即复制 Token，页面关闭后无法再次查看

### 5.2 Organization ID

在 Organization 设置页复制 **Organization ID**；也可以从浏览器地址
`https://app.signpath.io/Web/<organization-id>/...` 中取出那段 GUID。

## 6. 配置 GitHub Secrets

仓库 → Settings → Secrets and variables → Actions → **New repository secret**，
添加两个：

| Secret | 值 |
| --- | --- |
| `SIGNPATH_API_TOKEN` | 第 5.1 步创建的 Token |
| `SIGNPATH_ORGANIZATION_ID` | 第 5.2 步复制的 Organization ID |

也可以用 `gh` 命令：

```powershell
gh secret set SIGNPATH_API_TOKEN --repo ZiChen-hub/EAgentStudio
gh secret set SIGNPATH_ORGANIZATION_ID --repo ZiChen-hub/EAgentStudio
```

命令会提示粘贴值，不会回显到终端历史。

## 7. 添加签名工作流

新建 `.github/workflows/signpath.yml`，把下面两个 slug 换成你自己的：

- `<项目 slug>` → `eagent-studio`
- `release-signing` / `app-executable` / `windows-installer` 如果建的对象同名可保持不变

```yaml
name: signpath

on:
  workflow_dispatch:

permissions:
  contents: read
  actions: read

jobs:
  sign:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: 读取版本号
        shell: pwsh
        run: |
          $version = (Select-String -Path pyproject.toml -Pattern '^version = "([^"]+)"').Matches[0].Groups[1].Value
          "VERSION=$version" | Out-File -FilePath $env:GITHUB_ENV -Append

      - name: 安装依赖
        run: python -m pip install -r requirements.txt pyinstaller

      - name: 构建未签名主程序
        run: python -m PyInstaller --noconfirm --clean packaging/eagent.spec

      - name: 上传待签名主程序
        id: upload-app
        uses: actions/upload-artifact@v4
        with:
          name: unsigned-app
          path: dist/EAgentStudio/EAgentStudio.exe

      - name: 签名主程序
        uses: signpath/github-action-submit-signing-request@v3
        with:
          api-token: ${{ secrets.SIGNPATH_API_TOKEN }}
          organization-id: ${{ secrets.SIGNPATH_ORGANIZATION_ID }}
          project-slug: "eagent-studio"
          signing-policy-slug: "release-signing"
          artifact-configuration-slug: "app-executable"
          github-artifact-id: ${{ steps.upload-app.outputs.artifact-id }}
          wait-for-completion: true
          output-artifact-directory: signed-app

      - name: 用已签名主程序构建安装包
        shell: pwsh
        run: |
          Copy-Item signed-app\EAgentStudio.exe dist\EAgentStudio\EAgentStudio.exe -Force

          $candidates = @(
            "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
            "C:\Program Files\Inno Setup 6\ISCC.exe"
          )
          $iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
          if (-not $iscc) {
            choco install innosetup --no-progress -y
            $iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
          }
          if (-not $iscc) { throw "未找到 Inno Setup 6（ISCC.exe）" }

          & $iscc "packaging\eagent.iss"
          if ($LASTEXITCODE -ne 0) { throw "Inno Setup 编译失败" }

      - name: 上传待签名安装包
        id: upload-installer
        uses: actions/upload-artifact@v4
        with:
          name: unsigned-installer
          path: dist/installer/EAgentStudio-Setup-*.exe

      - name: 签名安装包
        uses: signpath/github-action-submit-signing-request@v3
        with:
          api-token: ${{ secrets.SIGNPATH_API_TOKEN }}
          organization-id: ${{ secrets.SIGNPATH_ORGANIZATION_ID }}
          project-slug: "eagent-studio"
          signing-policy-slug: "release-signing"
          artifact-configuration-slug: "windows-installer"
          github-artifact-id: ${{ steps.upload-installer.outputs.artifact-id }}
          wait-for-completion: true
          output-artifact-directory: signed-installer

      - name: 整理产物并生成校验和
        shell: pwsh
        run: |
          New-Item -ItemType Directory -Force release | Out-Null

          Copy-Item signed-app\EAgentStudio.exe release\EAgentStudio.exe -Force
          Copy-Item (Get-ChildItem signed-installer\EAgentStudio-Setup-*.exe | Select-Object -First 1).FullName release\

          Compress-Archive -Path dist\EAgentStudio\* `
            -DestinationPath "release\EAgentStudio-$env:VERSION-portable.zip" -Force

          Get-ChildItem release -File | Where-Object { $_.Extension -ne ".sha256" } | ForEach-Object {
            $hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower()
            "$hash  $($_.Name)" | Set-Content "$($_.FullName).sha256" -Encoding ascii
          }

      - name: 上传已签名产物
        uses: actions/upload-artifact@v4
        with:
          name: signed-windows
          path: release/
```

要点：

- 两个签名请求不能合并，顺序必须是：签主程序 → 构建安装包 → 签安装包
- `output-artifact-directory` 是签名后产物的解压目录
- 先签名再算 SHA256，签名会改变文件内容
- 如果仓库是私有的且未安装 GitHub App，需要保留 `permissions: actions: read` 和 `contents: read`

## 8. 触发与验证

### 8.1 触发

仓库 → Actions → **signpath** → Run workflow。首次运行重点看：

- 两个 `SignPath` 步骤是否显示 signing request 链接
- SignPath 控制台里请求状态是否为 **Completed**
- `signed-app` / `signed-installer` 目录是否非空

### 8.2 验证 Authenticode 签名

下载 `signed-windows` 产物后执行：

```powershell
Get-AuthenticodeSignature .\EAgentStudio.exe | Format-List Status, SignerCertificate, TimeStamperCertificate
Get-AuthenticodeSignature .\EAgentStudio-Setup-0.2.0.exe | Format-List Status, SignerCertificate, TimeStamperCertificate
```

预期：

- `Status` 为 `Valid`
- `SignerCertificate` 的 Subject 包含 `SignPath Foundation`
- `TimeStamperCertificate` 不为空

### 8.3 用 signtool 再次确认

如果装了 Windows SDK：

```powershell
signtool verify /pa /v .\EAgentStudio-Setup-0.2.0.exe
```

输出应包含 `Successfully verified`。`/pa` 表示按 Authenticode 策略验证，不要省略。

### 8.4 校验和

```powershell
Get-FileHash .\EAgentStudio-Setup-0.2.0.exe -Algorithm SHA256
```

结果应与工作流生成的 `.sha256` 文件一致。

## 9. 接到现有 Release 流程

签名工作流跑通后，可以在最后增加一步发布（需要 `permissions: contents: write`）：

```yaml
      - name: 创建 Release
        shell: pwsh
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          gh release create "v$env:VERSION" release/* `
            --repo "${{ github.repository }}" `
            --title "EAgent Studio $env:VERSION" `
            --notes "签名版本：主程序与安装包均由 SignPath 签名。"
```

或者保留手工流程：下载 `signed-windows` 产物，再在 GitHub 网页上传。

发布后建议在 README 补充：

- 安装包已签名，签名者为 SignPath Foundation
- 仍可提供 `.sha256` 供二次校验

## 10. 常见问题

**签名请求报 `artifact configuration does not match`**

ZIP 内的实际路径和 XML 里的 `path` 不一致。先解压 `unsigned-*` 产物看一眼目录结构，
再修正 XML。注意 `upload-artifact` 上传单个文件时，文件在 ZIP 根目录。

**报 `no GitHub-hosted runners`**

workflow 里有 job 用了 self-hosted runner，或 job 被拆到了非 GitHub 托管的 runner 上。
开源计划的签名前置 job 必须全部在 GitHub 托管 runner 执行。

**API Token 返回 401/403**

Token 没有 Submitter 权限，或 `SIGNPATH_ORGANIZATION_ID` 填错。重新生成 Token 并核对
Organization ID；Token 只在创建时显示一次。

**`artifact-id` 为空**

`upload-artifact` 必须是 v4 及以上，并且上传步骤要设置 `id`，例如
`id: upload-app`，再引用 `${{ steps.upload-app.outputs.artifact-id }}`。

**签名后安装包哈希和原来不一样**

这是正常现象。签名会写入签名数据，必须用签名后的文件重新生成 `.sha256`。

**SmartScreen 仍然提示**

先确认签名 `Status` 为 `Valid` 且存在时间戳。签名解决的是“未知发布者”，
“不常见的应用”需要下载量积累信誉，属于 SmartScreen 的另一套机制。

**重跑 workflow 后签名失败**

如果启用了 `disallow_reruns`，重跑会被拒绝；同时 SignPath 对启用策略的构建最多评估 3 次重跑。
重新提交一次新的 workflow 运行即可。

## 11. 本项目落地清单

- [ ] `docs/signpath.md` 随仓库提交（已完成）
- [ ] 申请 SignPath Foundation 开源计划并等待审核
- [ ] 创建 Project `eagent-studio`
- [ ] 创建 Signing Policy `release-signing`
- [ ] 创建 Artifact Configuration `app-executable`、`windows-installer`
- [ ] 关联 GitHub.com Trusted Build System
- [ ] 安装 SignPath GitHub App 到 `EAgentStudio`
- [ ] 配置 Secrets `SIGNPATH_API_TOKEN`、`SIGNPATH_ORGANIZATION_ID`
- [ ] 添加 `.github/workflows/signpath.yml` 并手动运行一次
- [ ] 校验签名与时间戳，重新生成 `.sha256`
- [ ] 用签名产物替换 Release 中的未签名文件
