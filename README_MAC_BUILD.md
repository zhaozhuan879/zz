# macOS 本机助手构建与验收

此目录的 `build_macos_companion.sh` 必须在 macOS 上执行。构建机会下载对应架构的 Codex 原生执行器并封装 Python 运行时；使用安装包的学生不需要安装 Python、Node.js 或 Codex CLI。

## 构建

在 Apple Silicon Mac 上生成 `dist/ResearchCompanionMac-arm64.pkg`；在 Intel Mac 上生成 `dist/ResearchCompanionMac-x86_64.pkg`：

```bash
bash build_macos_companion.sh
```

构建机需要 `python3`、`npm`、Xcode Command Line Tools。脚本在项目的 `build/` 下创建隔离 Python 环境，不修改全局 Python。也可将本目录放入私有 GitHub 仓库，手动触发 `.github/workflows/macos-companion.yml`，分别取得两个架构的构建产物。

没有 Mac 时，先在 GitHub 创建一个**空的私有仓库**，将 `dist/ResearchCompanionMac-build-kit.zip` 解压后的全部内容（包括 `.github/workflows/macos-companion.yml`）放入该仓库并推送到默认分支。打开仓库的 Actions → Build macOS research companion → Run workflow。工作流会分别在 Apple Silicon 和 Intel macOS 运行器上构建、检查 `.pkg` 结构，并上传两个独立构建产物。下载产物后，还需在对应架构的真实 Mac 上按下文进行安装和任务验收，才能放到平台的下载目录。

## 安装与使用

学生双击对应架构的 `.pkg` 安装后，回到网页点击“连接本机”。浏览器打开 `researchworkbench://` 链接时，安装的 `.app` 会兑换一次性绑定码、在当前用户下建立 `launchd` LaunchAgent，并启动后台助手。项目文件保存在 `~/ResearchWorkbench/projects/<项目ID>`；后台日志在 `~/Library/Application Support/ResearchWorkbench/Companion/companion.log`。

## 实机验收

在 Mac 上完成以下检查后，再把 `.pkg` 放入 Web 项目的 `dist/` 目录，网页才会显示下载入口：

1. 安装后点击网页“连接本机”，网页显示“本机客户端已连接”。
2. 提交创建简单网页的任务，确认 Codex 实际写入本机文件、运行命令，并将源文件同步回网页。
3. 注销并重新登录 Mac，确认 `launchd` 自动恢复助手，且无终端窗口。
4. 分别验证 Apple Silicon 与 Intel 包；两种架构的安装包不能互换。

目前构建脚本默认进行临时签名。要让其他学生正常双击安装，需配置 Apple Developer ID 对 `.app` 和 `.pkg` 签名并完成公证；脚本预留了 `MAC_CODESIGN_IDENTITY`、`MAC_INSTALLER_IDENTITY`、`MAC_NOTARY_PROFILE`，其中公证配置是 `notarytool` 已保存的钥匙串配置名。未签名或未公证包可能被 macOS 拦截，不能以此作为正式交付包。
