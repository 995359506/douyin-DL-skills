# 抖音视频下载器 - Web 前端

基于 Vue 3 + TypeScript + Element Plus 的 Web 管理界面。

## 快速开始

### 环境准备

确保已安装后端服务：
```bash
# 在项目根目录启动后端
python -m skills.douyin_downloader.web --host 127.0.0.1 --port 8080
```

### 安装依赖

```bash
npm install
```

### 开发模式

```bash
npm run dev
```

开发模式默认只用于前端调试，Vite 会监听 `127.0.0.1:5173` 并通过代理访问 8080 后端。
它不是交付页面，也不代表另一套应用。验收和日常使用统一访问 `http://127.0.0.1:8080/app/`。

> 协作约定：主目录 `/Users/smq/Documents/ChatGPT/agent专家/douyin-DL-skills` 是唯一源目录。
> 不要在 Codex worktree 或 3001 端口创建未同步的前端版本；前端改动完成后必须回到主目录构建，并重启 8080 服务验证。

### 生产构建

```bash
npm run build
```

构建后的文件位于 `dist/`，由 FastAPI 在 `/app/` 同源托管。

## 功能特性

### 1. Cookie 管理
- **状态检查**: 显示当前 Cookie 是否有效
- **刷新 Cookie**: 打开浏览器窗口扫码登录
- **自动保存**: 登录成功后自动保存到本地文件

### 2. 下载管理
- **分享文案自动识别**: 单视频、合集视频和用户主页入口均可直接粘贴完整分享文案；只向后端发送校验后的唯一抖音 URL
- **单视频下载**: 支持完整分享文案、Markdown 链接、分享短链和标准 URL
- **合集下载**: 三种方式获取合集
  - 通过视频链接自动识别合集
  - 通过用户主页获取所有合集
  - 直接输入合集 ID
- **实时进度**: WebSocket 推送下载进度
- **任务列表**: 查看所有下载任务状态

### 3. 任务操作
- **失败项重试/继续**: 保留已下载文件，只处理失败或未完成内容
- **删除任务**: 只清理任务记录，不删除已下载文件
- **查看详情**: 查看任务下的所有视频列表

## 配置

### 代理配置

在 `vite.config.ts` 中配置后端地址：

```typescript
server: {
  proxy: {
    '/api': {
      target: 'http://127.0.0.1:8080',  // Skill后端地址
      changeOrigin: true,
    },
    '/ws': {
      target: 'ws://127.0.0.1:8080',    // WebSocket地址
      ws: true,
    },
  },
}
```

## 项目结构

```
frontend/
├── src/
│   ├── api/           # API 客户端
│   │   └── index.ts
│   ├── components/    # 组件
│   │   └── DownloadRecords.vue
│   ├── stores/        # Pinia 状态管理
│   │   └── download.ts
│   ├── App.vue        # 主应用组件
│   ├── main.ts        # 入口文件
│   └── env.d.ts       # TypeScript 声明
├── index.html
├── package.json
├── tsconfig.json
├── tsconfig.node.json
└── vite.config.ts
```

## 技术栈

- **Vue 3** - 前端框架（Composition API）
- **TypeScript** - 类型安全
- **Element Plus** - UI 组件库
- **Pinia** - 状态管理
- **Axios** - HTTP 客户端
- **Vite** - 构建工具

## API 接口

### Cookie 管理
```typescript
// 检查 Cookie 状态
GET /api/cookie/status

// 刷新 Cookie（打开浏览器）
POST /api/cookie/refresh
{ "headless": false }
```

### 下载任务
```typescript
// 下载单个视频
POST /api/download
{ "url": "https://v.douyin.com/xxxxx" }

// 下载合集
POST /api/download-mix
{
  "mix_id": "123456789",
  "mix_name": "合集名称",
  "max_counts": 20,    // 安全默认值；明确确认后才用 null 全量下载
  "interval": "all"    // 或 "2024-01-01|2024-12-31"
}

// 获取任务列表
GET /api/tasks

// 继续未完成项（保留已下载文件）
POST /api/tasks/{task_id}/continue

// 删除任务记录（不会删除文件）
DELETE /api/tasks/{task_id}

// 删除任务
DELETE /api/tasks/{task_id}
```

### WebSocket
```typescript
// 连接 WebSocket 获取实时进度
const ws = new WebSocket('ws://127.0.0.1:8080/ws')

ws.onmessage = (event) => {
  const data = JSON.parse(event.data)
  // 处理进度更新
}
```

## 使用流程

### 首次使用
1. 检查 Cookie 状态（如无效则刷新）
2. 点击"刷新 Cookie"打开浏览器
3. 扫码登录抖音
4. 等待 Cookie 保存成功提示

### 下载合集
1. 在"合集下载"标签页
2. 选择获取方式（视频链接/用户主页/直接输入）
3. 填写信息并点击"获取"
4. 选择要下载的合集
5. 点击"下载选中合集"
6. 在"下载记录"页面查看进度

### 下载单个视频
1. 在"单视频下载"标签页
2. 直接粘贴完整抖音分享文案或视频链接，确认页面显示“已识别视频链接”
3. 点击"加入下载队列"，此时只暂存链接，不会开始下载
4. 在待下载项下确认后点击"开始下载"
5. 等待下载完成

## 常见问题

**Q: 刷新 Cookie 后浏览器窗口卡住？**
A: 这是正常的，浏览器窗口需要手动关闭。程序会在 120 秒后自动超时。

**Q: 下载进度不更新？**
A: 检查 WebSocket 连接是否正常。可以在浏览器开发者工具中查看 Network > WS 标签。

**Q: 如何查看已下载的视频文件？**
A: 在"下载记录"页面点击"查看详情"，会显示该任务下所有视频的本地文件路径。

## 相关文档

- [项目根目录 README](../README.md) - 项目概览
- [CLI 详细文档](../skills/douyin_downloader/SKILL.md) - 命令行使用

## 许可证

MIT
