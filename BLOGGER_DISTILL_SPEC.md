# 博主内容蒸馏：需求说明书 + 交给 AI 的提示词

> 用途：把「解析某位博主主页全部作品 → 下载 → 逐条口播提取 + 画面理解 → 跨内容归纳 → 产出 Markdown」这条流水线定清楚，先给 AI 复述确认，再动手实现。
> 结论性选择（已确认）：交付物 = 一份蒸馏总纲 + 逐条附录，且**每条附录必须包含完整口播文案**；范围 = 先扫清单再人工挑；深度 = 每条都做口播 + 画面理解；容错 = 逐条记账、可断点续跑。

## 1. 一句话目标

粘贴一个抖音主页链接，我能在结果清单里挑出想看的作品，点一次「开始蒸馏」，之后机器自己完成下载、逐条分析和最终归纳；中途关页面、重启服务、模型限流都不丢进度；最后得到一个 Markdown，读它就像把这个博主的内容压缩过一遍，而且每条结论都能回溯到具体视频和原话。

## 2. 这不是一个按钮，而是六个阶段

| 阶段 | 输入 | 输出 | 现状 |
| --- | --- | --- | --- |
| S1 扫元数据 | 主页 URL | 全部作品清单（标题/时间/类型/时长/封面/是否已下载），不下载媒体 | 已有：`POST /api/user-post-scans` + `GET .../items`（`scan_manager.py`） |
| S2 挑选 | 清单 + 筛选/勾选 | 确认的 aweme_id 集合 | 已有：`POST .../scans/{id}/downloads`，含 `confirm` 校验、按 aweme_id 去重、单次上限 200 条 |
| S3 批量下载 | task_ids | 本地视频文件 + `tasks`/`task_videos` 记录 | 已有，但只有逐条触发，没有「一批跑完继续下一步」的编排 |
| S4 逐条口播提取 | 本地视频 | `original_transcript` + `polished_text` | 已有：`transcription_service.py`（ffmpeg 抽 16k 单声道 WAV → DashScope ASR → 整理模型） |
| S5 逐条画面理解 | 本地视频 | 结构化 summary（title / one_sentence / overview / key_points / timeline / visual_content / spoken_content） | 已有：`video_summary_service.py`（压到 14MB 目标，可分段合并，并发 2） |
| S6 博主级归纳 + 落盘 Markdown | 全部 S4/S5 结果 | 一个 `.md`（或总纲 + 分片目录） | **完全没有，这是新功能的核心** |

关键缺口是 S6 和「把 S3→S5 串起来并不丢状态」的那层编排。已有的 S1–S5 单独都能用，但没有一条链路能让它们顺序跑完并记住跑到哪。

## 3. 由代码现状决定的硬约束（写进设计，不能忽略）

1. **想拿文案就必须下载视频**：S4 从本地文件抽音频（`extract_audio`），没有「不下载只转写」的路径。
2. **ASR 有约 11 分钟硬上限**：提取出的 16kHz 单声道 PCM 超过 `MAX_AUDIO_BYTES = 20MB` 直接失败（20971520 / 32000 ≈ 655 秒）。流水线必须预检时长并把超长条目标记为 `skipped`，而不是整批失败。
3. **画面理解有体积与分段上限**：目标 14MB、分段 12MB、最多 12 段；超出的条目要降级为「只做口播」并记录降级原因。
4. **一条任务只能存一份 AI 结果**：`transcript_jobs.task_id` 与 `video_summary_jobs.task_id` 都是 `UNIQUE`，重跑即覆盖。蒸馏需要「同一视频可能重跑、需要保留每次结果用于对比」的能力时，必须新建表，不要改这两个 UNIQUE 语义（改了会牵连既有端点和测试）。
5. **扫描会话是内存态 + 7200s TTL**：跑几小时的流水线期间，`scan_manager.sessions` 一旦被清理，`scan_id` 就 404。蒸馏任务一旦提交，必须把「选了哪些作品、元数据快照」落到自己的表里，之后不再依赖扫描会话存活。
6. **删任务会连带删 AI 结果**：`delete_task` 现在会级联清理两张 job 表。所以蒸馏产物必须**先落盘成文件**，DB 只当索引；否则用户清一次下载记录，蒸馏成果就没了。
7. **转写没有并发上限**（只有总结有 `Semaphore(2)`）：编排层必须自己限制，否则十几条并发 base64 会把本机内存和网关都打爆。
8. **429 目前只映射错误、不自动退避**：长流水线一定要加退避重试，否则一次限流就留下一堆 failed 条目。

## 4. 交付物的 Markdown 规范

文件名：`distill/<博主昵称>-<sec_user_id 前 8 位>/<YYYYMMDD-HHmm>-蒸馏总纲.md`，落在下载根目录下（与视频文件同级，独立可备份）。

结构（顺序固定）：

1. `# <博主名> 内容蒸馏` — 元信息块：生成时间、作品总数、成功分析数、跳过/失败数、覆盖时间范围、模型名、总字数。
2. `## 一句话画像` — 这个博主在做什么、给谁看、靠什么立论。
3. `## 主题地图` — 按主题聚类，每个主题给：占比、代表作品（链到附录锚点）、他反复输出的核心判断。
4. `## 观点与方法论` — 跨视频归纳出的可执行结论；**每条结论后面必须跟来源标注**，形如 `（来源：007《标题》、023《标题》）`，无来源的观点不许写。
5. `## 演变与矛盾` — 前后立场变化、自相矛盾之处、被反复纠正的说法。
6. `## 值得复用的表达` — 金句/开场/收尾范式，直接从口播文案里摘，标条号。
7. `## 逐条附录` — 每条作品一节：
   - `### NN. <标题>` + 发布时间 / 类型 / 时长 / 原链接 / 本地文件名
   - **要点**（来自 summary 的 key_points）
   - **画面信息**（visual_content，若有）
   - **时间线**（timeline，若有）
   - **口播文案（完整）** — 用 `polished_text`，一条不删；`original_transcript` 是否附在 `<details>` 里可选
   - 失败/降级时写明 `status` 与原因（如「音频 13 分钟，超出 ASR 上限，已跳过口播提取」）
8. `## 本次未纳入` — 被跳过、失败、超长、重复的条目清单及原因。

**体积保护规则（必须实现，不要临场判断）**：逐条附录的正文预计超过 5 万字时，自动改为「总纲 + 分片」——总纲里每条只保留要点与 300 字内文案摘要并内链到 `口播文案/NN-<aweme_id>.md`（完整全文），保证 100+ 条时文件仍能打开和检索。低于阈值就输出单文件。

**防幻觉规则**：归纳阶段只允许使用已入库的口播文案与 summary 字段，不得补写外部知识、不得推测未看过的内容；不确定就写「未在原片中找到依据」。

## 5. 需要新增的东西（建议形态，细节由实现方决定）

- 两张表：`distill_runs`（一次蒸馏：博主信息、范围、状态、时间、产物路径、统计数）与 `distill_items`（run_id + aweme_id 唯一，记录 task_id / download / transcript / summary / degrade_reason / retry_count / markdown 章节序号）。这张表就是断点续跑的唯一依据。
- 一个编排服务 `DistillService`：按条目推进状态机 `queued → downloading → transcribing → summarizing → ready → written`，全局并发自己限（建议下载 1–2、转写 1、总结沿用 2），带 429/5xx 指数退避与最大重试次数。
- 归纳层：当「全部要点 + 全部文案」超过模型上下文时，先分组归纳（map）再合并（reduce）；合并阶段的输入只放各组结构化结论 + 抽样原文，避免二次幻觉。
- 端点（最小集合）：`POST /api/distill-runs`（提交）、`GET /api/distill-runs/{id}`（进度与逐条状态）、`POST /api/distill-runs/{id}/resume`、`POST /api/distill-runs/{id}/cancel`、`GET /api/distill-runs/{id}/markdown`（下载产物）、`GET /api/distill-runs`（历史列表）。
- 前端：扫描结果页新增一个主按钮「开始蒸馏」，点击前展示粗估（条数 / 预计耗时区间 / 每条模型调用次数 / 将被跳过的超长条数）并要求二次确认；运行期一个常驻进度面板（按阶段显示当前第 N/M 条，失败条目可单条重试）；完成后可直接打开或下载 Markdown。
- 顺带复用：`/api/ai/diagnostics` 已存在但界面无入口，蒸馏开始前用它做一次 ffmpeg + Keychain 自检，避免跑三十分钟后发现没配 key。

## 6. 明确不做（避免范围被顺手扩大）

不做多博主对比、不做定时自动蒸馏、不做云端/多用户、不做视频剪辑或封面提取、不引入向量库或 RAG（文案已是文本，直接 Markdown + 全文搜索够用）、不改 `transcript_jobs` / `video_summary_jobs` 的现有 UNIQUE 语义、不把 Markdown 写进数据库当唯一存储。

## 7. 验收标准

端到端：挑 3 条视频（含 1 条已知超短的）走完 S1→S6，产物 Markdown 打开后每条附录里能看到完整口播文案，观点段落每条都带来源条号。

断点：跑到第 2 条时 kill 服务并重启，`resume` 后已完成的第 1 条不重复调用模型、不重复下载，产物最终一致。

容错：手工把某条的音频做成超过 20MB 触发上限，该条应记为 skipped/degraded，其余继续，Markdown 的「本次未纳入」列出原因。

分片：把阈值临时调低到 100 字，确认自动生成分片目录且总纲内链可点。

回归：`.venv/bin/python -m pytest -q` 全绿，且新增流水线自身的单测（状态机、续跑判定、退避计数、Markdown 渲染、分片阈值）；前端 `npm --prefix frontend run build` 后在 `http://127.0.0.1:8080/app/` 验收。

安全：全程无 API Key 出现在代码/前端/DB/日志；只监听 127.0.0.1；Markdown 与视频文件都落在下载根目录内且不越界。

## 8. 成本与耗时（决定你怎么开第一次跑）

每条视频至少产生 3 次模型调用：1 次 ASR、1 次文案整理、1–3 次画面理解（分段合并时更多），外加本机 ffmpeg 两遍转码。按现状估：单条端到端 3–6 分钟，30 条约 1.5–3 小时，100 条约 5–10 小时。所以第一次请只挑 3 条校准真实耗时和费用，再决定批量档位；金额按你的 DashScope 与网关单价自算，不在本文件里给数字。

## 9. 直接把下面这段发给 AI

````text
# 任务：在 douyin-DL-skills 项目里实现「博主内容蒸馏」流水线

先读 IMPROVEMENTS.md、README.md、skills/douyin_downloader/{scan_manager.py, transcription_service.py, video_summary_service.py, web.py, db.py} 和 frontend/src/components/{UserPostScanner.vue, TranscriptPanel.vue, VideoSummaryPanel.vue, DownloadRecords.vue}，然后【先复述你的理解 + 列出你打算新增的文件、表、端点 + 提出不超过 5 个必须我确认的问题】，等我回答后再写代码。不要先动手。

## 我要的东西
一条流水线，不是一个按钮：粘贴抖音主页链接 → 扫描全部作品元数据 → 我在清单里挑（支持时间/关键词/类型/是否已下载筛选，也支持全选筛选结果）→ 批量下载 → 每条都做「口播文案提取」和「视频画面理解」→ 跨全部条目做博主级归纳 → 产出 Markdown。中途关页面、重启服务、模型限流都不能丢进度，能续跑。

## 交付物（这是重点，结构照抄）
一份「蒸馏总纲.md」放在 <下载根目录>/distill/<博主名>-<sec_user_id前8位>/ 下，依次为：元信息块（生成时间/总数/成功数/跳过数/覆盖时间范围/模型/总字数）、一句话画像、主题地图、观点与方法论、演变与矛盾、值得复用的表达、逐条附录、本次未纳入。
硬要求：
1) 逐条附录每条必须包含【完整口播文案全文】，禁止节选、禁止用摘要替代。允许的是现有整理稿 `polished_text`（只做断句标点与口头语清理，不扩写不改事实），但绝对原文 `original_transcript` 要同时附在该条的 `<details>` 折叠块里，便于核对。
2) 「观点与方法论」「演变与矛盾」里每一条结论都要标来源条号，例如（来源：007《标题》、023《标题》）；找不到依据就明写「未在原片中找到依据」，禁止编造。
3) 「值得复用的表达」只能从口播原文里摘。
4) 预计正文超 5 万字时自动改成「总纲 + 分片」：总纲内每条保留要点 + 300 字内摘要并内链到 口播文案/NN-<aweme_id>.md 的全文；低于阈值就单文件。这个阈值写成可配置项。
5) 产物必须先落盘成文件，数据库只当索引，不能只存在 DB 里。

## 复用现有能力，不要重写
S1/S2 用现成的 /api/user-post-scans 与 /api/user-post-scans/{scan_id}/downloads（它已返回 task_ids，含 confirm 校验、按 aweme_id 去重、单次上限 200）。S4 用 transcription_service，S5 用 video_summary_service。开始前调一次已有的 /api/ai/diagnostics 做 ffmpeg + Keychain 自检。

## 必须新增
1) 两张表：distill_runs 与 distill_items（run_id + aweme_id 唯一；字段含 task_id、各阶段 status、degrade_reason、retry_count、附录序号）。这张表是断点续跑的唯一依据。不要改 transcript_jobs / video_summary_jobs 的 task_id UNIQUE 语义。
2) DistillService 编排层：状态机 queued → downloading → transcribing → summarizing → ready → written；自己控制并发（下载 1-2、转写 1、总结沿用现有 2），因为转写目前是无限流的、且会整体 base64；对 429/5xx 做指数退避 + 最大重试，耗尽只标记该条失败且不阻塞其它条目。
3) 扫描会话是内存态且 7200s 过期，提交蒸馏时必须把所选作品的元数据快照写进自己的表，之后不得再依赖 scan_id 存活。
4) 归纳层做 map-reduce：全部文案/要点超过模型上下文时先分组归纳再合并，合并输入用各组结构化结论 + 抽样原文。
5) 端点最小集：POST /api/distill-runs、GET /api/distill-runs、GET /api/distill-runs/{id}、POST /api/distill-runs/{id}/resume、POST /api/distill-runs/{id}/cancel、GET /api/distill-runs/{id}/markdown。
6) 前端：清单页主按钮「开始蒸馏」，点击前展示粗估（条数、预计耗时区间、每条模型调用次数、将被跳过的超长条数）并二次确认；运行期常驻进度面板（当前第 N/M 条 + 各阶段），失败条目可单条重试；完成后可打开/下载 Markdown。

## 已知硬约束，必须处理并单测
- 口播提取依赖本地文件抽音频，不下载就没文案。
- 提取出的 16kHz 单声道 PCM 超 20MB 即失败，约等于 11 分钟，超长的必须在提交前预检并标 skipped，不要整批失败。
- 画面理解目标 14MB、分段 12MB、最多 12 段；超出降级为「只做口播」并写降级原因。
- delete_task 会级联删掉两张 AI 作业表，所以已生成的蒸馏产物文件不能被这条删除路径连带影响（说明你的处理方式）。

## 明确不做
多博主对比、定时自动蒸馏、云端/多用户、视频剪辑、向量库/RAG、改现有 UNIQUE 语义、把 Markdown 只存 DB。

## 工程红线（项目既有约定，违反即返工）
注释与文档中文；只允许在主目录开发，前端改完必须 npm --prefix frontend run build 并在 http://127.0.0.1:8080/app/ 验收，不接受 5173/3001/worktree 的结论；后端只绑 127.0.0.1；API Key 只走 macOS Keychain（douyin-downloader-llm / douyin-downloader-dashscope / douyin-downloader-text168 / douyin-downloader-summary-fallback），绝不能出现在代码、前端、数据库、日志里；用项目 logger 而不是新增 print；[OK]/[FAIL] 只用于 Windows 控制台输出，浏览器界面里一律用中文；.venv/bin/python -m pytest -q 必须全绿。

## 实施顺序（每步做完停下给我看，不要一口气写完）
1. 先给我 distill_runs / distill_items 表设计与状态机代码，配单测（含续跑判定：已完成条目不得重复调用模型）。
2. 打通 3 条视频的端到端（S3→S6），产出真实 Markdown 给我看结构。
3. 加归纳层的 map-reduce 与防幻觉校验。
4. 加前端按钮、预估、常驻进度、失败单条重试。
5. 最后处理分片阈值与超长文件保护，并跑一次 30 条的实盘验证。
````

## 10. 使用这段提示词的两点提醒

第一次实盘请只放 3 条，用它校准真实耗时与费用后再决定档位；跑通后把 `distill_runs` 的统计字段留成可查询的（第 8 节的耗时估算要靠它反推真实值，而不是靠我这个先验数字）。

这份文件同时是设计基线：如果后面哪个决定变了（比如改成默认只跑口播），直接改第 4、5 节并通知实现方，别在提示词里散落着改。
