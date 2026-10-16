# 变更日志

所有重要变更都将记录在此文件中。

## [1.2.0] - 2026-08-08

### 修复

- **扫码登录流程卡住** - 修复新电脑扫码登录成功后流程持续等待的问题
  - 新增自动点击"保存登录状态"提示浮层按钮（DOM 浮层，原生 dialog 仍由 `page.on("dialog")` 自动 accept 兜底）
  - 登录 cookie 读取改为全量 + `douyin.com` 域名过滤（原实现只按 `https://www.douyin.com/` 过滤，可能遗漏子域 cookie）
  - 新增"验证码中间页"（滑块/行为验证）风控检测，命中时输出明确警告
  - 新增 localStorage `HasUserLogin` 登录信号作为辅助诊断
  - 超时后输出诊断信息（页面标题、当前 cookie 集合、是否命中风控页、是否点击过浮层），便于定位原因
  - 移除 `page.content()` 无意义轮询死代码

### 文档

- `SKILL_INSTALL.md` / `SKILL.md`：补充"保存登录状态"提示需点击确认、验证码中间页风控提示的说明

## [1.1.0] - 2026-02-17

### 修复

- **修复编码问题** - 解决 Windows 控制台特殊字符导致的 `'gbk' codec can't encode` 错误
  - 修复 `web.py` 中的 `✓` 字符 → `[OK]`
  - 修复 `cli.py` 中的 `✓`、`✗`、`⏭`、`○` 字符 → `[OK]`、`[FAIL]`、`[SKIP]`、`[PENDING]`
  - Web API 和 CLI 现在可以正常响应，不再返回 500 错误

### 改进

- **优化启动脚本** - 自动关闭占用端口的进程
  - `start_douyin_skill.bat` (Windows)：新增 `kill_port` 函数，自动清理 8080/3000 端口
  - `start_douyin_skill.sh` (Linux/Mac)：新增 `kill_port` 函数，使用 `lsof`/`fuser` 强制关闭进程
  - 启动服务前自动检测并释放端口，无需手动干预

### 验证

- ✅ Web API 下载功能测试通过
  - 测试 URL: `https://v.douyin.com/-uix9dCj42c/`
  - 返回结果: `{"success":true,"data":{"task_id":"...","status":"completed"}}`
- ✅ CLI 下载功能测试通过
  - 命令: `python -m skills.douyin_downloader.cli video "https://v.douyin.com/-uix9dCj42c/"`
  - 结果: 视频已存在，正确跳过

## [1.0.0] - 2026-02-16

### 重大改进

- **Task Manager V3** - 完全异步化架构
  - 所有数据库操作使用 `aiosqlite` 异步执行
  - WebSocket 连接管理异步化
  - 后台下载任务使用 `asyncio.create_task()`
  - 任务状态回调机制支持实时推送

### 修复

- ✅ 修复异步调用问题
  - 修复 `web.py` 中 `task_manager.get_tasks()` 和 `get_task()` 缺少 `await` 的问题
  - 修复 WebSocket 处理中的异步调用问题（3处）
  - 添加 `restart_task` 方法支持任务重启

- ✅ 修复数据库表结构
  - 统一 `task_videos` 表的列定义
  - 移除不兼容的 `desc_raw` 和 `nickname_raw` 列
  - 重建数据库确保所有列都存在

- ✅ 修复任务状态更新
  - `_download_video_task` 现在正确更新数据库状态
  - 使用 `task_manager.update_task_status()` 替代直接操作内存缓存
  - 确保下载完成后状态正确持久化

### 验证

- ✅ 视频文件实际下载成功
  - 测试文件：`7604606097898409279_Claude_Code_Agent_Teams..._video.mp4`
  - 文件大小：48.05 MB
  - 任务状态：`completed`

### 文档更新

- 更新主 README.md - 添加系统要求、启动方式说明
- 更新 Skill README.md - 更新架构说明（V3）
- 更新所有 Agent SKILL.md（.claude、.opencode、.agents）
  - 添加 Web 界面使用说明
  - 添加技术架构说明
  - 添加数据库结构说明
  - 添加状态流转图
- 更新 WebSocket 优化文档 - 添加 2026-02-16 修复记录

## [0.9.0] - 2026-02-14

### 新增

- **WebSocket 实时推送** - 替代轮询模式
  - 后端添加 WebSocket 端点 `/ws`
  - 前端添加 WebSocket 服务模块
  - 自动降级策略（WebSocket 失败时回退到轮询）
  - WebSocket 状态指示器（UI 标签）

- **任务管理器 V2** - 基础异步支持
  - SQLite 数据库持久化
  - 任务状态管理
  - 进度追踪

### 技术栈

- FastAPI + WebSocket
- Vue 3 + TypeScript
- Element Plus UI
- Pinia 状态管理
- F2 框架（抖音 SDK）

## [0.1.0] - 2025-XX-XX

### 初始版本

- 基础下载功能（单视频、合集）
- CLI 命令行接口
- Cookie 管理
- 基础 Web UI

---

## 版本说明

**版本号格式**: 主版本号.次版本号.修订号

- **主版本号**: 重大架构变更或不兼容修改
- **次版本号**: 功能新增或重要改进
- **修订号**: Bug 修复和小改进

## 如何更新

```bash
# 拉取最新代码
git pull origin main

# 更新依赖
pip install -r requirements.txt
cd frontend && npm install

# 重启服务
python -m skills.douyin_downloader.web --port 8080
cd frontend && npm run dev
```

## 兼容性说明

- **V1.x**: 基础功能版本
- **V2.x**: 添加 WebSocket 和异步支持（向后兼容）
- **V3.x**: 完全异步化架构（数据库结构变更，需要重新初始化）

**从 V2 升级到 V3**: 
- 需要删除旧的数据库文件（`douyin_downloader.db`、`douyin_videos.db`）
- 重新运行服务会自动创建新的数据库结构
- 历史任务数据会丢失，但已下载的视频文件保留
