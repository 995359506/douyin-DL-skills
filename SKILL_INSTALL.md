# Douyin Downloader Skill 安装指南

> **本文档供 AI Agent 读取并执行**：当用户提供本仓库地址并要求"安装为 skill / 参照本文档安装"时，请严格按照以下步骤完成获取、注册、配置与验证。所有命令均应在项目根目录（克隆后的仓库目录）下执行。

## 1. 仓库信息

| 项 | 值 |
|----|-----|
| GitHub 仓库 | `https://github.com/amibaren/douyin-DL-skills` |
| Skill 源码目录 | `skills/douyin_downloader/` |
| Skill 名称 | `douyin-downloader` |
| 能力概述 | 下载抖音单视频、合集、用户主页作品（完整能力见 `skills/douyin_downloader/SKILL.md`） |
| 系统要求 | Python 3.9+；Windows / macOS / Linux |

## 2. 获取源码

```bash
git clone https://github.com/amibaren/douyin-DL-skills.git
cd douyin-DL-skills
```

## 3. 注册 Skill（选择安装模式）

### 模式 A：仓库内使用（推荐）

在仓库根目录运行 Agent 时，直接让 Agent 读取 `skills/douyin_downloader/SKILL.md` 即可，**无需复制**（此时 `python -m skills.douyin_downloader.cli` 命令与 `skills.douyin_downloader.*` 导入路径直接可用）。

若希望 Agent 每次自动发现该 Skill，在项目说明文件（`AGENTS.md` 或 `CLAUDE.md`）中追加一行提示：

```markdown
- 下载抖音视频/合集时，请读取 skills/douyin_downloader/SKILL.md 并按其说明执行
```

### 模式 B：注册为 Agent Skill（自动发现）

将 `skills/douyin_downloader/` 复制（或软链）到目标 AI 工具约定的 skills 目录，**目标目录名必须为 `douyin-downloader`**（与 `SKILL.md` frontmatter 的 `name` 一致，符合 OpenCode 命名校验）。

> ⚠️ **避免路径嵌套**：本仓库的 Skill 源码位于 `skills/douyin_downloader/`——其中 `skills/` 是**源码根目录**，与安装目标的 skills 目录（如 `.claude/skills/`）目录名相同（都叫 `skills`，但是不同的目录）。请只复制 `douyin_downloader` 这一个子目录，**切勿复制整个 `skills/` 目录**：
>
> - ✅ 正确：`cp -r skills/douyin_downloader .claude/skills/douyin-downloader`
> - ❌ 错误：`cp -r skills .claude/skills/` → 会产生 `.claude/skills/skills/douyin_downloader/`，嵌套两层，Agent 无法发现
>
> 安装后的正确结构应为 `<skills目录>/douyin-downloader/SKILL.md`。

根据用户实际使用的 AI 工具选择注册位置（不确定时询问用户，或同时注册多处）：

**Claude Code**

```bash
# 项目级（仅当前仓库生效）
mkdir -p .claude/skills
cp -r skills/douyin_downloader .claude/skills/douyin-downloader

# 个人全局（所有项目生效）
mkdir -p ~/.claude/skills
cp -r skills/douyin_downloader ~/.claude/skills/douyin-downloader

# 软链方式（避免双份维护，推荐全局使用）
ln -s "$(pwd)/skills/douyin_downloader" ~/.claude/skills/douyin-downloader
```

**OpenCode**

```bash
# 项目级
mkdir -p .opencode/skills
cp -r skills/douyin_downloader .opencode/skills/douyin-downloader

# 全局
mkdir -p ~/.config/opencode/skills
cp -r skills/douyin_downloader ~/.config/opencode/skills/douyin-downloader
```

**其他兼容客户端**：复制到该客户端约定的 skills 目录（如 `.agents/skills/`、`~/.agents/skills/` 等，均遵循 Agent Skills 规范）。

> **注意（模式 B 的重要限制）**：全局注册只提供"能力描述"（SKILL.md）。SKILL.md 中的 CLI 命令（`python -m skills.douyin_downloader.cli`）与 Python 导入（`skills.douyin_downloader.*`）仍依赖本仓库的 `skills/` 目录在 Python 搜索路径中。因此 Agent 执行下载时，应 `cd` 到克隆的仓库目录，或通过 `PYTHONPATH=<仓库路径>` 运行（`skills` 为命名空间包，需其父目录即仓库根在 Python 搜索路径中）；依赖安装见第 4 步。

## 4. 安装运行依赖（Skill 必需）

**安装 Skill 必须安装依赖包**——仅把 `SKILL.md` 复制到 skills 目录无法运行。在仓库根目录执行：

```bash
pip install -r requirements.txt
playwright install chromium   # 用于 Cookie 扫码获取，必须安装
```

依赖明细（`requirements.txt`）：

| 依赖 | 用途 | 是否必需 |
|------|------|---------|
| `f2` | 抖音下载核心框架 | ✅ Skill 必需 |
| `playwright` | 浏览器扫码获取 Cookie（配合 `playwright install chromium`） | ✅ Skill 必需 |
| `aiosqlite` | 任务持久化（SQLite 异步驱动） | ✅ Skill 必需 |
| `httpx` | 网络请求 | ✅ Skill 必需 |
| `fastapi` / `uvicorn[standard]` / `pydantic` / `aiofiles` | Web 服务（仅 Web UI / Web API 使用方式需要） | ⭕ 按需 |

> 若仅通过 CLI / Python API / Agent Skill 使用，可只安装 `f2 playwright aiosqlite httpx`；使用 Web UI / Web API 时需安装全部依赖。

## 5. 初始化 F2 配置

```bash
# 生成抖音应用配置
f2 dy --init-config my_douyin.yaml

# 创建全局配置目录，并显式禁用 Bark 通知（避免请求超时；f2 默认即为 false）
mkdir -p conf
cat > conf/conf.yaml << 'EOF'
f2:
  enable_bark: false
EOF
```

可选：编辑 `my_douyin.yaml` 设置下载路径、并发数等（默认下载到 `Download/`）。

## 6. 初始化 Cookie（首次使用必需）

抖音下载必须使用已登录的 Cookie。Cookie 文件 `douyin_cookie.txt` 位于项目根目录，所有使用方式共享。

- 本地已有有效 `douyin_cookie.txt`：跳过本步，所有方式自动加载；
- 否则**必须先主动初始化一次**（任一入口均可，人工扫码需显示浏览器窗口）：

```bash
# CLI 方式（默认显示浏览器窗口，120 秒内扫码）
python -m skills.douyin_downloader.cli refresh-cookie
```

或让用户通过 Web UI 的"刷新 Cookie"按钮完成。登录成功后 Cookie 自动保存到 `douyin_cookie.txt`。

> 提示：
> - 登录成功后若弹出"保存登录状态"提示，**请点击确认**（系统也会尝试自动点击该浮层按钮，但人工确认最稳妥）；
> - 若浏览器进入"验证码中间页"（滑块/行为验证），说明当前网络/IP 被抖音风控，请完成验证或更换网络环境后重试。

> 注意：下载过程中的自动获取为无头模式（无窗口、无法人工扫码），不能替代首次主动初始化。

## 7. 验证安装

依次执行以下验证，全部通过即安装成功：

```bash
# 1) Python 包可导入
python -c "from skills.douyin_downloader.core import DouyinDownloadSkill; print('import OK')"

# 2) CLI 可用（应显示 usage 帮助）
python -m skills.douyin_downloader.cli --help

# 3) Cookie 有效（应输出 cookie valid: True/False，而非报错）
python -c "from skills.douyin_downloader.cookie_manager import load_local_cookie, is_cookie_logged_in; c = load_local_cookie(); print('cookie valid:', bool(c) and is_cookie_logged_in(c))"
```

Skill 被 AI 工具识别的验证：

- **Claude Code**：会话中输入 `/douyin-downloader`，或询问 "What skills are available?"，应能看到 `douyin-downloader`；
- **OpenCode**：技能列表中应出现 `douyin-downloader`；
- 若未出现：检查目标目录名是否为 `douyin-downloader`、`SKILL.md` 是否位于 `<skills目录>/douyin-downloader/SKILL.md`、frontmatter 是否包含 `name` 与 `description`。

## 8. 使用

读取 `skills/douyin_downloader/SKILL.md` 获取完整能力说明（CLI 命令、Python API、数据格式与风控说明）。下载任务由 Agent 按 SKILL.md 描述执行。
