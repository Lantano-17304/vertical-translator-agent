# Whisper 本地模型配置指南（给用户）

Whisper 是**听写**（从音频猜字），YouTube 官方/CC 字幕是**人工或平台字幕**，多数游戏实况 **YouTube 字幕质量明显更好**。推荐用法：

- **默认（两个都不勾选）**：只用 YouTube 字幕 → 翻译质量最好  
- **勾选「无字幕时用 Whisper」**：有 CC 仍用 YouTube；只有抓不到字幕才听写  
- **「强制仅用 Whisper」**：不要用，除非确认视频完全没有字幕  

听写仍不理想时：把 `WHISPER_MODEL` 改为 `medium` 并下载对应目录；可选设置 `WHISPER_INITIAL_PROMPT`（见 `.env.example`）。

使用前需完成 **两步**：安装依赖 + 配置模型路径。

页面打开后，YouTube 输入框下方会显示 **Whisper 配置状态**（绿色=可用，红色=需处理）。

---

## 第一步：安装 faster-whisper（仅一次）

在项目根目录打开 PowerShell：

```powershell
cd backend
.\.venv\Scripts\pip install -r requirements-whisper.txt
```

---

## 第二步：准备模型（推荐本地，避免联网下载失败）

### 方式 A：手动下载（国内推荐）

1. 打开镜像页：https://hf-mirror.com/Systran/faster-whisper-small  
2. 下载 **整个文件夹** 里的这些文件到同一目录，例如 `C:\models\faster-whisper-small`：

   - `model.bin`（约 484MB）
   - `config.json`
   - `tokenizer.json`
   - `vocabulary.txt`

3. 编辑项目根目录 `.env`，增加一行：

   ```env
   WHISPER_MODEL_PATH=C:\models\faster-whisper-small
   ```

4. **重启后端**（关闭 Translator Backend 窗口后重新 `start.bat`）。

### 方式 B：自动配置脚本

在项目根目录执行：

```powershell
.\scripts\setup-whisper-model.ps1
```

按提示输入模型文件夹路径，脚本会检查文件是否齐全并写入 `.env`。

### 方式 C：在线下载（需稳定访问 HuggingFace）

不设置 `WHISPER_MODEL_PATH` 时，首次识别会自动下载模型。国内建议在 `.env` 增加：

```env
HF_ENDPOINT=https://hf-mirror.com
HF_PROXY=http://127.0.0.1:7890
```

（`HF_PROXY` 使用 HTTP 代理端口，不要用 socks5。）

---

## 如何确认配置成功

1. 重启 `start.bat`  
2. 打开 http://localhost:3000  
3. 看 YouTube 区域下方状态框：

   - **本地 Whisper 模型已就绪** → 可按需勾选「无字幕时用 Whisper」  
   - 红色提示 → 按列表说明补全文件或路径  

也可访问：http://127.0.0.1:8000/whisper_status 查看 JSON 状态。

---

## 常见问题

| 现象 | 处理 |
|------|------|
| 只下载了 `model.bin` | 必须下载完整 4 个文件 |
| `ConnectTimeout` / HuggingFace | 改用方式 A 本地路径，或配置 `HF_ENDPOINT` |
| 勾选后仍失败 | 确认已重启后端；路径用英文目录更稳 |
| 识别很慢 | 正常，CPU 上 10 分钟视频可能要几分钟 |

---

## 交付 / 验收说明（给助教）

Whisper 为**可选功能**，默认不安装不影响字幕翻译。验收者若不用 Whisper，无需下载模型。
