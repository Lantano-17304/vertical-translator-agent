# Git / GitHub 交付指南

本文说明如何把本项目提交到 GitHub，供课程验收或协作使用。

## 交付前自检

- [ ] 根目录存在 `.env.example`，**不要**提交 `.env`（含真实 API Key）
- [ ] 未提交 `backend/.venv`、`node_modules`、`chroma_data`、`cookies/`
- [ ] `README.md` 含运行方式；架构见 `docs/architecture.md`，报告见 `docs/REPORT.md`
- [ ] 克隆后可按 README 或 `start.bat` 启动

## 1. 本地初始化（仅首次）

在项目根目录执行：

```powershell
cd C:\path\to\project1
git init
git add .
git status
```

确认 `git status` 中**没有** `.env`、`backend\.venv`、`node_modules` 等（已被 `.gitignore` 忽略）。

```powershell
git commit -m "feat: initial release of vertical-domain translator agent"
```

## 2. 在 GitHub 创建仓库

1. 打开 https://github.com/new  
2. Repository name 建议：`vertical-translator-agent`（或课程要求名称）  
3. **不要**勾选 “Add a README”（本地已有）  
4. 创建后记下仓库 URL，例如：  
   `https://github.com/<你的用户名>/vertical-translator-agent.git`

## 3. 关联远程并推送

```powershell
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

若使用 SSH：

```powershell
git remote add origin git@github.com:<你的用户名>/<仓库名>.git
git push -u origin main
```

首次推送若提示登录，在浏览器完成 GitHub 认证，或使用 [Personal Access Token](https://github.com/settings/tokens) 作为密码。

## 4. 仓库设置建议（可选）

| 项 | 建议 |
| --- | --- |
| Description | 垂直领域智能翻译 Agent（LangChain + RAG + YouTube/SRT） |
| Topics | `langchain`, `fastapi`, `rag`, `translation`, `agent` |
| Visibility | 课程要求 Public 则选 Public |

## 5. 验收者克隆与运行

```powershell
git clone https://github.com/<你的用户名>/<仓库名>.git
cd <仓库名>
copy .env.example .env
# 编辑 .env 填入 OPENAI_API_KEY、YOUTUBE_PROXY 等
.\start.bat
```

浏览器访问：http://localhost:3000

Docker 方式见根目录 `README.md` 中 `docker compose up --build`。

## 6. 常见问题

| 问题 | 处理 |
| --- | --- |
| `git push` 被拒绝 | 先 `git pull origin main --rebase` 再 push |
| 误提交 `.env` | 立即在 GitHub 轮换 API Key；`git rm --cached .env` 后重新提交 |
| 仓库过大 | 确认未提交 `chroma_data/`、`.venv`、`node_modules` |
| 仅本机有提交 | 必须执行 `git push` 后老师才能看到 |

## 7. 提交信息规范（建议）

- `feat:` 新功能  
- `fix:` 修复  
- `docs:` 文档  
- `chore:` 构建/脚本/依赖  

示例：`fix: strip agent reasoning from SRT export`
