<template>
  <el-card id="task-desk" class="download-records">
    <template #header>
      <div class="card-header">
        <div>
          <p class="records-eyebrow">02 / TASK DESK</p>
          <h2 id="records-title">任务工作台</h2>
        </div>
        <div class="header-actions">
          <el-radio-group v-model="taskFilterStatus" size="small" aria-label="按任务状态筛选" @change="handleFilterChange">
            <el-radio-button label="">全部</el-radio-button>
            <el-radio-button label="downloading">下载中</el-radio-button>
            <el-radio-button label="completed">已完成</el-radio-button>
            <el-radio-button label="failed">失败</el-radio-button>
          </el-radio-group>
          <el-select v-model="taskFilterType" size="small" clearable placeholder="全部类型" aria-label="按任务类型筛选" class="task-type-filter" @change="handleAdvancedFilterChange">
            <el-option label="单视频" value="single" />
            <el-option label="合集" value="mix" />
            <el-option label="用户合集" value="mix_user" />
            <el-option label="用户作品" value="post" />
          </el-select>
          <el-input
            v-model="taskKeyword"
            class="task-search"
            size="small"
            clearable
            placeholder="搜索标题、作者或 ID"
            aria-label="搜索任务标题、作者或 ID"
            @keyup.enter="handleAdvancedFilterChange"
            @clear="handleAdvancedFilterChange"
          >
            <template #prefix><el-icon><Search /></el-icon></template>
          </el-input>
          <el-select v-model="taskSortBy" size="small" aria-label="任务排序字段" class="task-sort" @change="handleAdvancedFilterChange">
            <el-option label="按创建时间" value="created_at" />
            <el-option label="按完成时间" value="completed_at" />
            <el-option label="按标题" value="title" />
            <el-option label="按状态" value="status" />
          </el-select>
          <el-button size="small" plain aria-label="切换任务排序方向" @click="toggleSortOrder">
            {{ taskSortOrder === 'desc' ? '新→旧' : '旧→新' }}
          </el-button>
          
          <!-- WebSocket 实时开关 -->
          <div class="ws-switch">
            <el-switch
              :model-value="useWebSocket"
              size="small"
              aria-label="切换实时任务更新"
              @change="handleWsModeChange"
            />
            <span class="ws-label" :class="{ 'ws-off': !useWebSocket }">实时</span>
          </div>
          <el-tooltip :content="wsStatusTooltip" placement="top">
            <el-tag
              :type="wsStatusType"
              size="small"
              class="ws-status-tag"
            >
              {{ wsStatusLabel }}
            </el-tag>
          </el-tooltip>
          <el-button v-if="useWebSocket && ['disconnected', 'error'].includes(wsStatus)" size="small" type="warning" link @click="handleReconnect">
            点击重连
          </el-button>
          
          <el-button
            class="refresh-button"
            size="small"
            :loading="tasksLoading"
            aria-label="刷新下载记录"
            @click="refreshTasks"
          >
            <el-icon><Refresh /></el-icon>
            刷新
          </el-button>
        </div>
      </div>
    </template>
    
    <!-- 批量操作工具条 -->
    <div class="batch-bar">
      <el-button size="small" plain @click="toggleSelectAllVisible">
        {{ allCurrentPageSelected ? '取消全选本页' : '全选本页' }}
      </el-button>
      <el-button
        size="small"
        type="danger"
        plain
        :disabled="selectedTasks.length === 0"
        @click="handleBatchDelete"
      >
        删除选中 ({{ selectedTasks.length }})
      </el-button>
      <el-button size="small" :disabled="!selectedTasks.length || selectedTasks.some(row => row.type !== 'single' || row.is_batch_video)" @click="openAssignAuthor(selectedTasks)">选中归到作者</el-button>
      <el-button
        size="small"
        type="warning"
        plain
        @click="handleClearCompleted"
      >
        清空已完成
      </el-button>
    </div>

    <aside v-if="activeTaskSummary.length" class="floating-task-tray" aria-live="polite">
      <div class="floating-task-heading">
        <strong>当前任务进度</strong>
        <el-button link type="primary" size="small" @click="scrollToTaskDesk">查看任务工作台</el-button>
      </div>
      <div v-for="task in activeTaskSummary" :key="task.id" class="floating-task-item">
        <span class="floating-task-name">{{ truncate(task.video_name || '未知任务', 24) }}</span>
        <span class="floating-task-status">{{ getStatusLabel(task.status) }}<template v-if="task.status === 'downloading'"> {{ task.progress?.percentage || 0 }}%</template></span>
      </div>
    </aside>

    <div class="author-gallery-layout">
      <nav class="author-rail" aria-label="按作者筛选作品">
        <div class="author-rail-heading">作者 <span>{{ taskAuthors.length }}</span></div>
        <button class="author-option" :class="{ active: taskFilterAuthor === null }" :aria-current="taskFilterAuthor === null ? 'true' : undefined" @click="selectAuthor(null)">
          <span>全部作品</span><small>{{ authorTotal }}</small>
        </button>
        <button v-for="author in taskAuthors" :key="author.name" class="author-option" :class="{ active: taskFilterAuthor === author.name }" :aria-current="taskFilterAuthor === author.name ? 'true' : undefined" @click="selectAuthor(author.name)">
          <span :title="author.name || '未知作者'">{{ author.name || '未知作者' }}</span><small>{{ author.count }}</small>
        </button>
      </nav>
      <div class="gallery-content" v-loading="tasksLoading">
        <div class="gallery-heading">
          <div><span class="gallery-kicker">作品画廊</span><h3>{{ taskFilterAuthor === null ? '全部作品' : (taskFilterAuthor || '未知作者') }}</h3></div>
          <div class="gallery-heading-actions">
            <span>{{ displayTasks.length }} 项</span>
            <el-button type="success" size="small" :disabled="taskFilterAuthor === null || taskFilterAuthor === '' || taskTotal === 0 || authorBatchBusy" @click="startAuthorTranscript">
              <el-icon><Microphone /></el-icon>一键提取口播
            </el-button>
            <el-button type="primary" size="small" :disabled="taskFilterAuthor === null || taskFilterAuthor === '' || taskTotal === 0 || authorBatchBusy" @click="startAuthorSummary">
              <el-icon><DataAnalysis /></el-icon>一键视频总结
            </el-button>
            <el-button size="small" :disabled="!taskFilterAuthor" @click="exportAuthorSummaries"><el-icon><Download /></el-icon>导出作者总结 Markdown</el-button>
          </div>
        </div>
        <div v-if="authorBatch && authorBatch.author === taskFilterAuthor" class="author-summary-progress" role="status" aria-live="polite">
          <span>{{ authorBatch.status === 'running' ? '正在逐条生成独立总结' : authorBatch.status === 'completed' ? (authorBatch.total === 0 ? '没有需要新总结的视频' : '逐条总结完成') : '批量总结已中断' }}</span>
          <strong>{{ authorBatch.processed }}/{{ authorBatch.total }}</strong>
          <span>新生成 {{ authorBatch.completed }} · 失败 {{ authorBatch.failed }} · 跳过已总结 {{ authorBatch.skipped_summarized }} · 跳过处理中 {{ authorBatch.skipped_running }}</span>
        </div>
        <div v-if="authorTranscriptBatch && authorTranscriptBatch.author === taskFilterAuthor" class="author-summary-progress transcript-batch-progress" role="status" aria-live="polite">
          <span>{{ authorTranscriptBatch.status === 'running' ? '正在逐条提取独立口播' : authorTranscriptBatch.status === 'completed' ? (authorTranscriptBatch.total === 0 ? '没有需要新提取的视频' : '逐条口播提取完成') : '口播批次已中断' }}</span>
          <strong>{{ authorTranscriptBatch.processed }}/{{ authorTranscriptBatch.total }}</strong>
          <span>新提取 {{ authorTranscriptBatch.completed }} · 失败 {{ authorTranscriptBatch.failed }} · 跳过已有口播 {{ authorTranscriptBatch.skipped_transcribed }} · 跳过处理中 {{ authorTranscriptBatch.skipped_running }}</span>
        </div>
        <div v-if="tasks.length" class="task-gallery">
          <article v-for="row in displayTasks" :key="row.id" class="gallery-item">
            <div class="gallery-cover">
              <img v-if="row.status === 'completed' && row.type === 'single' && !failedCovers.has(row.id)" :src="getThumbnailLink(row)" :alt="`${row.video_name || '视频'}的封面`" loading="lazy" @error="markCoverFailed(row.id)" />
              <div v-else class="gallery-cover-empty"><el-icon><VideoCamera /></el-icon><span>{{ row.type === 'single' ? '暂无封面' : getTypeLabel(row.type) }}</span></div>
              <button v-if="getPreviewLink(row)" type="button" class="gallery-cover-play" :aria-label="`播放视频：${row.video_name || '未知视频'}`" title="播放视频" @click="openVideoPreview(row)">
                <span class="gallery-play-icon"><el-icon><VideoPlay /></el-icon></span>
              </button>
              <span v-if="row.type === 'single'" class="gallery-summary-status" :class="`is-${row.summary_status || 'none'}`">{{ getSummaryStatusLabel(row.summary_status) }}</span>
              <span class="gallery-status" :class="`is-${row.status}`">{{ getStatusLabel(row.status) }}</span>
              <el-checkbox class="gallery-select" :model-value="selectedTasks.some(item => item.id === row.id)" :aria-label="`选择任务：${row.video_name || '未知视频'}`" @change="(checked: boolean | string | number) => toggleTaskSelection(row, Boolean(checked))" />
            </div>
            <div class="gallery-details">
              <div class="gallery-meta"><span>{{ row.video_info?.nickname || (row.type === 'post' ? row.video_name : '未知作者') }}</span><span>{{ formatTime(row.created_at) }}</span></div>
              <h4 :title="row.video_name || '未知视频'">{{ row.video_name || '未知视频' }}</h4>
              <div class="gallery-result">
                <span>{{ getTypeLabel(row.type) }}</span>
                <span v-if="row.status === 'downloading'">{{ row.progress?.percentage || 0 }}%</span>
                <span v-else-if="row.status === 'completed'">{{ row.result?.downloaded || (row.type === 'single' ? 1 : 0) }} 个文件</span>
                <span v-else-if="row.status === 'failed'" :title="row.error?.reason || row.result?.message">{{ row.error?.reason || '任务失败' }}</span>
              </div>
              <p v-if="row.status === 'downloading' && row.progress?.current_video_name" class="gallery-progress-message" role="status">{{ row.progress.current_video_name }}</p>
              <p v-if="row.status === 'failed' && row.error?.action" class="gallery-progress-message">{{ row.error.action }}</p>
              <div class="gallery-actions">
                <el-button v-if="row.type === 'single'" size="small" @click="openContentReader({ task_id: row.id, aweme_id: row.video_info?.aweme_id })">阅读与收藏</el-button>
                <el-button v-if="row.type === 'single' && row.video_info?.aweme_id" size="small" @click="addToTopic({ aweme_id: row.video_info.aweme_id })">加入专题</el-button>
                <el-button v-if="row.type === 'single' && !row.is_batch_video" size="small" @click="openAssignAuthor([row])">归到作者</el-button>
                <el-button v-if="row.status === 'failed'" size="small" type="primary" @click="handleRetry(row.parent_task_id || row.id)"><el-icon><RefreshRight /></el-icon>重试失败项</el-button>
                <el-button v-if="(row.status === 'pending' || row.status === 'downloading') && !row.is_batch_video" size="small" @click="handleCancel(row.id)"><el-icon><Close /></el-icon>取消</el-button>
                <el-button v-if="row.status === 'cancelled' && !row.is_batch_video" size="small" @click="handleContinue(row.id)"><el-icon><VideoPlay /></el-icon>继续</el-button>
                <el-button v-if="getPreviewLink(row)" size="small" @click="openVideoPreview(row)"><el-icon><VideoPlay /></el-icon>播放</el-button>
                <a v-if="getDownloadLink(row)" class="gallery-download" :href="getDownloadLink(row)" :download="row.file_name" aria-label="下载视频"><el-icon><Download /></el-icon></a>
                <el-tooltip v-if="row.status === 'completed' && row.type === 'single' && row.file_name" content="视频总结" placement="top">
                  <el-button size="small" type="warning" plain aria-label="视频总结" title="视频总结" @click="openVideoSummary(row)"><el-icon><DataAnalysis /></el-icon>总结</el-button>
                </el-tooltip>
                <el-tooltip v-if="row.status === 'completed' && row.type === 'single' && row.file_name && row.is_batch_video" content="提取口播文案" placement="top">
                  <el-button size="small" type="success" plain aria-label="提取口播文案" title="提取口播文案" @click="openTranscript(row)"><el-icon><Microphone /></el-icon>口播</el-button>
                </el-tooltip>
                <el-tooltip v-if="canDeleteLocalVideos(row)" :content="localDeletionAvailable ? '删除记录和本地视频' : '本地服务重启加载新接口后可用'" placement="top">
                  <span class="local-delete-trigger">
                    <el-button size="small" type="danger" plain aria-label="删除记录和本地视频" :title="localDeletionAvailable ? '删除记录和本地视频' : '本地服务重启加载新接口后可用'" :disabled="!localDeletionAvailable" @click="handleDeleteWithFiles(row)"><el-icon><Delete /></el-icon>删记录与视频</el-button>
                  </span>
                </el-tooltip>
                <el-dropdown v-if="!row.is_batch_video" trigger="click" @command="(cmd: string) => handleGalleryCommand(cmd, row)">
                  <el-button size="small" aria-label="更多操作" title="更多操作"><el-icon><MoreFilled /></el-icon></el-button>
                  <template #dropdown><el-dropdown-menu>
                    <el-dropdown-item v-if="row.status === 'completed' && row.type === 'single' && row.file_name" command="transcript">提取口播</el-dropdown-item>
                    <el-dropdown-item command="log">任务日志</el-dropdown-item>
                    <el-dropdown-item command="open-url" :disabled="!row.url">打开原链接</el-dropdown-item>
                    <el-dropdown-item command="copy-id" :disabled="!getTaskId(row)">复制 ID</el-dropdown-item>
                    <el-dropdown-item command="delete" divided>仅删除记录</el-dropdown-item>
                  </el-dropdown-menu></template>
                </el-dropdown>
              </div>
            </div>
          </article>
        </div>
        <el-empty v-else-if="!tasksLoading && !error" description="当前筛选下没有作品" />
      </div>
    </div>

    <el-table v-if="false"
      class="desktop-task-table"
      :data="displayTasks"
      v-loading="tasksLoading"
      style="width: 100%"
      :max-height="500"
      row-key="id"
      :row-class-name="taskRowClassName"
      @selection-change="handleSelectionChange"
      empty-text="暂无数据"
    >
      <!-- 多选列 -->
      <el-table-column type="selection" width="42" />
      <!-- 视频信息列 -->
      <el-table-column label="视频" min-width="280">
        <template #default="{ row }">
          <div class="video-info">
            <el-tooltip :content="row.video_name || '未知视频'" placement="top" :show-after="500">
              <div class="video-name">
                {{ truncate(row.video_name || '未知视频', 35) }}
              </div>
            </el-tooltip>
            <div class="video-meta">
              <el-tag size="small" :type="getTypeTag(row.type)">
                {{ getTypeLabel(row.type) }}
              </el-tag>
              <el-tooltip
                v-if="getHistoryAttemptCount(row) > 0"
                placement="top"
                :content="getHistoryFailureCount(row) > 0
                  ? `同一视频已合并 ${getHistoryFailureCount(row)} 次失败尝试，当前展示最新有效结果`
                  : `同一视频已合并 ${getHistoryAttemptCount(row)} 条历史尝试，当前展示最新有效结果`"
              >
                <el-tag size="small" type="info" effect="plain" class="history-attempt-tag">
                  {{ getHistoryAttemptLabel(row) }}
                </el-tag>
              </el-tooltip>
            </div>
          </div>
        </template>
      </el-table-column>
      
      <!-- 状态列 -->
      <el-table-column label="状态" width="100" align="center">
        <template #default="{ row }">
          <el-tag :type="getStatusType(row.status)" size="small">
            {{ getStatusLabel(row.status) }}
          </el-tag>
          <span v-if="row.status === 'completed'" class="history-label">主记录</span>
          <span v-if="getHistoryAttemptCount(row) > 0" class="history-label">
            {{ getHistoryDescription(row) }}
          </span>
        </template>
      </el-table-column>
      
      <!-- 进度列 -->
      <el-table-column label="进度" width="140">
        <template #default="{ row }">
          <div v-if="row.status === 'downloading'" class="progress-wrapper">
            <el-progress 
              :percentage="row.progress?.percentage || 0"
              :status="getProgressStatus(row.status)"
              :stroke-width="8"
            />
            <div v-if="row.progress?.current_video_name" class="current-video">
              正在下载: {{ truncate(row.progress.current_video_name, 20) }}
            </div>
          </div>
          <div v-else-if="row.status === 'completed'" class="result-summary">
            <span class="success-count">
              ✓ {{ row.result?.downloaded || 0 }}/{{ getTotalCount(row) }}
            </span>
            <span v-if="row.progress?.percentage" class="percent-text">
              ({{ row.progress.percentage }}%)
            </span>
            <span v-if="row.result?.skipped > 0" class="skip-count">
              跳过 {{ row.result.skipped }}
            </span>
            <span v-if="row.result?.failed > 0" class="fail-count">
              ✗ {{ row.result.failed }}
            </span>
            <el-tooltip 
              v-if="row.result?.has_more" 
              :content="row.result?.batch_info?.message || '还有更多视频未下载'" 
              placement="top"
            >
              <el-tag size="small" type="warning">更多+</el-tag>
            </el-tooltip>
          </div>
          <div v-else-if="row.result?.downloaded !== undefined && row.status !== 'pending'" class="result-summary">
            <span v-if="row.result.downloaded > 0" class="success-count">
              {{ row.result.downloaded }}/{{ getTotalCount(row) }}
            </span>
            <span v-if="row.result.skipped > 0" class="skip-count">
              跳过 {{ row.result.skipped }}
            </span>
            <span v-if="row.result.failed > 0" class="fail-count">
              ✗ {{ row.result.failed }}
            </span>
          </div>
          <span v-else>-</span>
        </template>
      </el-table-column>
      
      <!-- 文件数列 -->
      <el-table-column label="文件数" width="60" align="center">
        <template #default="{ row }">
          <span v-if="row.status === 'completed' && row.type === 'single'">1</span>
          <span v-else-if="row.type !== 'single'">{{ row.result?.downloaded || 0 }}</span>
          <span v-else>-</span>
        </template>
      </el-table-column>

      <!-- 下载文件列 -->
      <el-table-column label="下载" width="100">
        <template #default="{ row }">
          <div v-if="row.status === 'completed'" class="download-cell">
            <!-- 单视频：直接下载 -->
            <a 
              v-if="row.type === 'single' && getDownloadLink(row)"
              :href="getDownloadLink(row)" 
              target="_blank"
              class="download-link"
              :download="row.file_name"
            >
              <el-icon><Download /></el-icon>
              <span class="link-text">下载</span>
            </a>
            <el-button
              v-if="row.type === 'single' && getPreviewLink(row)"
              link
              type="success"
              size="small"
              class="preview-link"
              aria-label="播放已下载视频"
              @click="openVideoPreview(row)"
            >
              <el-icon><VideoPlay /></el-icon>
              <span class="link-text">播放</span>
            </el-button>
            <span v-else class="no-download">-</span>
          </div>
          <span v-else class="no-download">-</span>
        </template>
      </el-table-column>
      
      <!-- 时间列 -->
      <el-table-column label="创建时间" width="130">
        <template #default="{ row }">
          {{ formatTime(row.created_at) }}
        </template>
      </el-table-column>
      
      <!-- 操作列 -->
      <el-table-column label="操作" width="180" fixed="right">
        <template #default="{ row }">
          <el-tooltip v-if="row.status === 'failed'" content="只重试失败项" placement="top">
            <el-button 
              type="primary" 
              size="small"
              circle
              aria-label="只重试失败项"
              @click="handleRetry(row.id)"
            >
              <el-icon><RefreshRight /></el-icon>
            </el-button>
          </el-tooltip>
          <el-tooltip v-if="row.status === 'pending' || row.status === 'downloading'" content="取消任务" placement="top">
            <el-button
              type="warning"
              size="small"
              circle
              aria-label="取消任务"
              @click="handleCancel(row.id)"
            >
              <el-icon><Close /></el-icon>
            </el-button>
          </el-tooltip>
          <el-tooltip v-if="row.status === 'cancelled'" content="继续未完成项" placement="top">
            <el-button
              type="success"
              size="small"
              circle
              aria-label="继续未完成项"
              @click="handleContinue(row.id)"
            >
              <el-icon><VideoPlay /></el-icon>
            </el-button>
          </el-tooltip>
          <el-dropdown trigger="click" @command="(cmd: string) => handleMoreCommand(cmd, row)">
            <el-button size="small" circle title="更多操作" aria-label="更多操作">
              <el-icon><MoreFilled /></el-icon>
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="open-url" :disabled="!row.url">
                  <el-icon><Position /></el-icon>打开原链接
                </el-dropdown-item>
                <el-dropdown-item command="copy-id" :disabled="!getTaskId(row)">
                  <el-icon><CopyDocument /></el-icon>复制 ID
                </el-dropdown-item>
                <el-dropdown-item command="log" divided>
                  <el-icon><Document /></el-icon>查看日志
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <el-tooltip content="删除" placement="top">
            <el-button 
              type="danger" 
              size="small"
              circle
              aria-label="删除任务记录"
              @click="handleDelete(row.id)"
            >
              <el-icon><Delete /></el-icon>
            </el-button>
          </el-tooltip>
          <el-tooltip v-if="row.status === 'completed' && row.type === 'single' && row.file_name" content="调用 Gemini 分析完整视频，可能需要较长时间或产生费用" placement="top">
            <el-button type="warning" size="small" plain aria-label="视频总结" @click="openVideoSummary(row)">
              <el-icon><DataAnalysis /></el-icon>
              <span>视频总结</span>
            </el-button>
          </el-tooltip>
          <el-tooltip v-if="row.status === 'completed' && row.type === 'single' && row.file_name" content="调用语音模型提取口播文案，可能需要较长时间或产生费用" placement="top">
            <el-button type="success" size="small" plain aria-label="提取口播文案" @click="openTranscript(row)">
              <el-icon><Microphone /></el-icon>
              <span>提取口播</span>
            </el-button>
          </el-tooltip>
        </template>
      </el-table-column>
    </el-table>

    <div v-if="tasks.length > 0" class="mobile-task-list" v-loading="tasksLoading" aria-label="移动端下载记录">
      <article v-for="row in displayTasks" :key="row.id" class="task-card" :class="`status-${row.status}`">
        <header class="task-card-header">
          <el-checkbox :model-value="selectedTasks.some(item => item.id === row.id)" :aria-label="`选择任务：${row.video_name || '未知视频'}`" @change="(checked: boolean | string | number) => toggleTaskSelection(row, Boolean(checked))" />
          <div class="task-card-title"><span class="task-type">{{ getTypeLabel(row.type) }}</span><h3>{{ row.video_name || '未知视频' }}</h3></div>
          <span class="task-status" :class="`is-${row.status}`"><span aria-hidden="true"></span>{{ getStatusLabel(row.status) }}</span>
        </header>
        <div v-if="row.status === 'downloading'" class="task-card-progress"><div><span>当前进度</span><strong>{{ row.progress?.percentage || 0 }}%</strong></div><el-progress :percentage="row.progress?.percentage || 0" :stroke-width="8" :show-text="false" /><p v-if="row.progress?.current_video_name">正在处理：{{ truncate(row.progress.current_video_name, 38) }}</p></div>
        <div v-else-if="row.status === 'completed'" class="task-result is-complete">已完成 {{ row.result?.downloaded || (row.type === 'single' ? 1 : 0) }} 个文件</div>
        <div v-else-if="row.status === 'failed'" class="task-result is-failed">失败：{{ row.error?.reason || row.result?.message || '任务执行失败' }}</div>
        <div v-else class="task-result">{{ getStatusLabel(row.status) }}，等待任务更新</div>
        <div v-if="getHistoryAttemptCount(row) > 0" class="task-history-note">
          {{ getHistoryDescription(row) }}
        </div>
        <dl class="task-facts"><div><dt>创建时间</dt><dd>{{ formatTime(row.created_at) }}</dd></div></dl>
        <footer class="task-card-actions">
          <el-button v-if="row.type === 'single'" size="small" @click="openContentReader({ task_id: row.id, aweme_id: row.video_info?.aweme_id })">阅读与收藏</el-button>
          <el-button v-if="row.type === 'single' && row.video_info?.aweme_id" size="small" @click="addToTopic({ aweme_id: row.video_info.aweme_id })">加入专题</el-button>
          <el-button v-if="row.type === 'single' && !row.is_batch_video" size="small" @click="openAssignAuthor([row])">归到作者</el-button>
          <el-button v-if="row.status === 'failed'" size="small" @click="handleRetry(row.parent_task_id || row.id)"><el-icon aria-hidden="true"><RefreshRight /></el-icon>重试失败项</el-button>
          <el-button v-if="(row.status === 'pending' || row.status === 'downloading') && !row.is_batch_video" size="small" type="warning" @click="handleCancel(row.id)"><el-icon aria-hidden="true"><Close /></el-icon>取消</el-button>
          <el-button v-if="row.status === 'cancelled' && !row.is_batch_video" size="small" type="success" @click="handleContinue(row.id)"><el-icon aria-hidden="true"><VideoPlay /></el-icon>继续未完成项</el-button>
          <el-button v-if="row.status === 'completed' && row.type === 'single' && getPreviewLink(row)" size="small" type="success" plain @click="openVideoPreview(row)"><el-icon aria-hidden="true"><VideoPlay /></el-icon>播放视频</el-button>
          <a v-if="row.status === 'completed' && row.type === 'single' && getDownloadLink(row)" :href="getDownloadLink(row)" class="mobile-download-link" :download="row.file_name"><el-icon aria-hidden="true"><Download /></el-icon>下载文件</a>
          <el-button v-if="row.status === 'completed' && row.type === 'single' && row.file_name" size="small" type="success" plain @click="openTranscript(row)"><el-icon aria-hidden="true"><Microphone /></el-icon>提取口播</el-button>
          <el-button v-if="row.status === 'completed' && row.type === 'single' && row.file_name" size="small" type="warning" plain @click="openVideoSummary(row)"><el-icon aria-hidden="true"><DataAnalysis /></el-icon>视频总结</el-button>
          <el-button v-if="!row.is_batch_video" size="small" @click="openTaskLog(row)"><el-icon aria-hidden="true"><Document /></el-icon>任务日志</el-button>
          <el-button v-if="!row.is_batch_video" size="small" type="danger" plain @click="handleDelete(row.id)"><el-icon aria-hidden="true"><Delete /></el-icon>仅删除记录</el-button>
          <el-tooltip v-if="canDeleteLocalVideos(row) && !row.is_batch_video" :content="localDeletionAvailable ? '删除记录和本地视频' : '本地服务重启加载新接口后可用'" placement="top">
            <span class="local-delete-trigger">
              <el-button size="small" type="danger" :disabled="!localDeletionAvailable" @click="handleDeleteWithFiles(row)"><el-icon aria-hidden="true"><Delete /></el-icon>删除记录和本地视频</el-button>
            </span>
          </el-tooltip>
        </footer>
      </article>
    </div>
    
    <!-- 分页控件 -->
    <div v-if="taskTotal > 0" class="pagination-wrapper">
      <el-pagination
        v-model:current-page="currentPage"
        v-model:page-size="currentPageSize"
        :page-sizes="[10, 20, 50, 100]"
        :total="taskTotal"
        layout="total, sizes, prev, pager, next, jumper"
        @size-change="handleSizeChange"
        @current-change="handlePageChange"
      />
    </div>
    
    <!-- 空状态 -->
    <el-empty v-if="tasks.length === 0 && !tasksLoading && !error" description="暂无下载记录" />
    
    <!-- 错误状态：与空列表区分，避免网络失败被误认为没有记录 -->
    <div v-if="error && !tasksLoading" class="records-error" role="alert">
      <el-alert type="error" title="任务记录加载失败" :description="error" show-icon :closable="false" />
      <el-button type="primary" @click="refreshTasks">重试加载</el-button>
    </div>

    <!-- 任务日志弹窗 -->
    <el-dialog v-model="logDialogVisible" title="任务详情与日志" width="720" destroy-on-close>
      <el-descriptions v-if="logTask" :column="1" border size="small">
        <el-descriptions-item label="任务ID">{{ logTask.id }}</el-descriptions-item>
        <el-descriptions-item label="类型">{{ getTypeLabel(logTask.type) }}</el-descriptions-item>
        <el-descriptions-item label="状态">{{ getStatusLabel(logTask.status) }}</el-descriptions-item>
        <el-descriptions-item label="视频名称">{{ logTask.video_name || '未知' }}</el-descriptions-item>
        <el-descriptions-item label="错误阶段">{{ logTask.error?.stage || '未记录' }}</el-descriptions-item>
        <el-descriptions-item label="错误码"><code>{{ logTask.error?.code || '未记录' }}</code></el-descriptions-item>
        <el-descriptions-item label="失败原因">{{ logTask.error?.reason || '未记录' }}</el-descriptions-item>
        <el-descriptions-item label="建议动作">{{ logTask.error?.action || '暂无建议' }}</el-descriptions-item>
        <el-descriptions-item label="结果消息">{{ logTask.result?.message || '暂无结果消息' }}</el-descriptions-item>
        <el-descriptions-item v-if="getHistoryAttemptCount(logTask) > 0" label="历史尝试">
          <div class="history-attempt-summary">
            <span>列表已将同一视频的重复任务合并为当前主记录。</span>
            <ul v-if="getHistoryAttempts(logTask).length" class="history-attempts">
              <li v-for="attempt in getHistoryAttempts(logTask)" :key="attempt.id">
                <strong>{{ getStatusLabel(attempt.status) }}</strong>
                <span>{{ formatTime(attempt.created_at || '') }}</span>
                <span v-if="attempt.error?.code">{{ attempt.error.code }}</span>
                <span v-if="attempt.error?.reason">{{ attempt.error.reason }}</span>
              </li>
            </ul>
            <span v-else>已合并 {{ getHistoryAttemptCount(logTask) }} 条历史记录（详情由服务端保留）。</span>
          </div>
        </el-descriptions-item>
        <el-descriptions-item label="失败项">
          <ul v-if="logTask.result?.failed_items?.length" class="failed-items">
            <li v-for="item in logTask.result.failed_items" :key="item.aweme_id">
              {{ item.name }}（{{ item.aweme_id }}）：{{ item.reason }}
            </li>
          </ul>
          <span v-else>暂无失败项</span>
        </el-descriptions-item>
        <el-descriptions-item label="下载选项">{{ getDownloadOptionsText(logTask) }}</el-descriptions-item>
        <el-descriptions-item label="创建时间">{{ formatTime(logTask.created_at) }}</el-descriptions-item>
        <el-descriptions-item label="完成时间">{{ logTask.completed_at ? formatTime(logTask.completed_at) : '-' }}</el-descriptions-item>
      </el-descriptions>
    </el-dialog>
    <el-dialog
      v-model="videoPreviewVisible"
      class="video-preview-dialog"
      title="视频预览"
      width="min(94vw, 1360px)"
      top="3vh"
      append-to-body
      destroy-on-close
      :close-on-click-modal="false"
      @close="stopVideoPreview"
    >
      <div class="video-preview-stage">
        <video
          v-if="previewTask"
          ref="previewVideo"
          :src="getPreviewLink(previewTask)"
          controls
          autoplay
          preload="auto"
          playsinline
          aria-label="视频画面预览"
        />
      </div>
    </el-dialog>
    <el-dialog v-model="assignAuthorVisible" title="归到作者" width="420px" append-to-body>
      <p>将 {{ assignAuthorTasks.length }} 条视频归入作者分类，已有口播和总结会保留。</p>
      <el-select v-model="assignAuthorName" filterable allow-create default-first-option placeholder="选择已有作者，或输入作者名" style="width: 100%">
        <el-option v-for="author in taskAuthors.filter(item => item.name)" :key="author.name" :label="author.name" :value="author.name" />
      </el-select>
      <template #footer><el-button @click="assignAuthorVisible = false">取消</el-button><el-button type="primary" :loading="assignAuthorBusy" :disabled="!assignAuthorName.trim()" @click="saveAssignedAuthor">确认归类</el-button></template>
    </el-dialog>
    <TranscriptPanel v-model="transcriptDialogVisible" :task-id="transcriptTaskId" />
    <VideoSummaryPanel v-model="summaryDialogVisible" :task-id="summaryTaskId" />
  </el-card>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, computed, nextTick, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { storeToRefs } from 'pinia'
import { Refresh, RefreshRight, Delete, CopyDocument, Download, MoreFilled, Position, Document, Close, VideoPlay, VideoCamera, Microphone, DataAnalysis, Search } from '@element-plus/icons-vue'
import { useDownloadStore, type DownloadTask } from '@/stores/download'
import { getHistoryAttemptCount, getHistoryAttempts, getHistoryFailureCount } from '@/utils/downloadTaskHistory'
import { douyinApi } from '@/api'
import { wsService } from '@/services/websocket'
import TranscriptPanel from '@/components/TranscriptPanel.vue'
import VideoSummaryPanel from '@/components/VideoSummaryPanel.vue'
import { openContentReader, addToTopic } from '@/api/content'

const store = useDownloadStore()
// 使用storeToRefs解构响应式状态
const { taskFilterStatus, taskFilterType, taskFilterAuthor, taskAuthors, taskKeyword, taskSortBy, taskSortOrder, wsStatus, useWebSocket, taskPage, taskPageSize, taskTotal } = storeToRefs(store)
// 使用方法直接解构
const { fetchTasks, goToPage, changePageSize, retryTask, continueTask, cancelTask, deleteTask, checkServiceStatus, startWebSocket, stopWebSocket, reconnectWebSocket, toggleWebSocketMode, setTaskFilters } = store
// 计算属性保持响应性
const tasks = computed(() => store.tasks || [])
const tasksLoading = computed(() => store.tasksLoading)
const error = computed(() => store.error)
const authorTotal = computed(() => taskAuthors.value.reduce((total, author) => total + author.count, 0))
const failedCovers = ref(new Set<string>())
const localDeletionAvailable = ref(false)
const markCoverFailed = (id: string) => { failedCovers.value = new Set([...failedCovers.value, id]) }
const selectAuthor = (author: string | null) => { void setTaskFilters({ author }) }
const exportAuthorSummaries = async () => {
  if (!taskFilterAuthor.value) return
  try {
    const response = await fetch(`/api/author-summaries/export?author=${encodeURIComponent(taskFilterAuthor.value)}`)
    if (!response.ok) {
      const error = await response.json()
      throw new Error(typeof error.detail === 'string' ? error.detail : '导出失败')
    }
    const disposition = response.headers.get('Content-Disposition') || ''
    const encodedName = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1]
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = encodedName ? decodeURIComponent(encodedName) : `${taskFilterAuthor.value}.md`
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (error: any) { ElMessage.error(error.message || '导出作者总结失败') }
}
const prioritizedTasks = computed(() => [...tasks.value].sort((a, b) => {
  const rank = (status: string) => ['downloading', 'pending', 'queued', 'failed', 'cancelled', 'paused', 'completed'].indexOf(status)
  return rank(a.status) - rank(b.status)
}))
const displayTasks = computed(() => prioritizedTasks.value.flatMap((task) => {
  if (task.type === 'single' || !task.videos?.length) return [task]
  return task.videos.map((video, index): DownloadTask => {
    const rawStatus = video.status || 'pending'
    const status: DownloadTask['status'] = rawStatus === 'downloaded' || rawStatus === 'skipped'
      ? 'completed'
      : rawStatus === 'failed'
        ? 'failed'
        : task.status === 'downloading'
          ? 'downloading'
          : 'pending'
    const fileName = video.file_path ? video.file_path.split(/[\\/]/).pop() || '' : ''
    const awemeId = video.aweme_id || `${task.id}-${index}`
    return {
      ...task,
      id: `${task.id}::${awemeId}`,
      parent_task_id: task.id,
      is_batch_video: true,
      type: 'single',
      status,
      video_name: video.desc || `视频 ${awemeId}`,
      original_desc: video.desc || '',
      file_name: fileName,
      video_info: {
        aweme_id: video.aweme_id,
        nickname: video.nickname || task.mix_name || '',
        sec_user_id: video.sec_user_id || task.mix_id || '',
        cover: video.cover || null,
        create_time: video.create_time,
      },
      summary_status: video.summary_status || 'none',
      url: `https://www.douyin.com/video/${encodeURIComponent(awemeId)}`,
      progress: {
        current: status === 'completed' ? 1 : 0,
        total: 1,
        percentage: status === 'completed' ? 100 : status === 'downloading' ? task.progress?.percentage || 0 : 0,
        current_video_name: status === 'downloading' ? task.progress?.current_video_name : undefined,
      },
      result: {
        success: status === 'completed',
        downloaded: status === 'completed' ? 1 : 0,
        skipped: rawStatus === 'skipped' ? 1 : 0,
        failed: status === 'failed' ? 1 : 0,
        message: video.error_message || '',
      },
      error: status === 'failed'
        ? { stage: 'download', code: 'VIDEO_DOWNLOAD_FAILED', reason: video.error_message || '视频下载失败', action: '稍后重试该批次' }
        : undefined,
    }
  })
}))
const activeTaskSummary = computed(() => prioritizedTasks.value
  .filter(task => ['downloading', 'pending', 'queued', 'failed'].includes(task.status))
  .slice(0, 4))
const taskRowClassName = ({ row }: { row: DownloadTask }) => row.status === 'completed' ? 'completed-history-row' : ''

// 批量选择与日志弹窗状态
const selectedTasks = ref<DownloadTask[]>([])
const assignAuthorVisible = ref(false)
const assignAuthorTasks = ref<DownloadTask[]>([])
const assignAuthorName = ref('')
const assignAuthorBusy = ref(false)
const openAssignAuthor = (rows: DownloadTask[]) => {
  assignAuthorTasks.value = [...rows]
  assignAuthorName.value = ''
  assignAuthorVisible.value = true
}
const saveAssignedAuthor = async () => {
  assignAuthorBusy.value = true
  try {
    const response = await fetch('/api/task-authors/assign', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_ids: assignAuthorTasks.value.map(row => row.id), author: assignAuthorName.value.trim() }),
    })
    const data = await response.json()
    if (!response.ok || !data.success) throw new Error(typeof data.detail === 'string' ? data.detail : data.message || '作者归类失败')
    assignAuthorVisible.value = false
    selectedTasks.value = []
    await fetchTasks()
    ElMessage.success(data.message)
  } catch (error: any) { ElMessage.error(error.message || '作者归类失败') }
  finally { assignAuthorBusy.value = false }
}
const logDialogVisible = ref(false)
const logTask = ref<DownloadTask | null>(null)
const videoPreviewVisible = ref(false)
const previewTask = ref<DownloadTask | null>(null)
const previewVideo = ref<HTMLVideoElement | null>(null)
const stopVideoPreview = () => {
  const video = previewVideo.value
  if (video) {
    video.pause()
    video.removeAttribute('src')
    video.load()
  }
  previewTask.value = null
}
const transcriptDialogVisible = ref(false)
const transcriptTaskId = ref('')
const summaryDialogVisible = ref(false)
const summaryTaskId = ref('')
interface AuthorSummaryBatch {
  batch_id: string
  author: string
  status: 'running' | 'completed' | 'interrupted'
  total: number
  processed: number
  completed: number
  reused: number
  failed: number
  skipped_summarized: number
  skipped_running: number
}
interface AuthorTranscriptBatch {
  batch_id: string
  author: string
  status: 'running' | 'completed' | 'interrupted'
  total: number
  processed: number
  completed: number
  reused: number
  failed: number
  skipped_transcribed: number
  skipped_running: number
}
const authorBatch = ref<AuthorSummaryBatch | null>(null)
const authorTranscriptBatch = ref<AuthorTranscriptBatch | null>(null)
const authorBatchBusy = computed(() => authorBatch.value?.status === 'running' || authorTranscriptBatch.value?.status === 'running')
let authorBatchTimer: ReturnType<typeof setInterval> | null = null
let authorTranscriptBatchTimer: ReturnType<typeof setInterval> | null = null
const stopAuthorBatchPolling = () => {
  if (authorBatchTimer) clearInterval(authorBatchTimer)
  authorBatchTimer = null
}
const stopAuthorTranscriptBatchPolling = () => {
  if (authorTranscriptBatchTimer) clearInterval(authorTranscriptBatchTimer)
  authorTranscriptBatchTimer = null
}
const loadAuthorBatch = async (author: string) => {
  try {
    const response = await douyinApi.getAuthorSummaryBatch(author) as any
    if (taskFilterAuthor.value !== author) return
    authorBatch.value = response?.data || null
    if (authorBatch.value) void fetchTasks({ silent: true })
    if (authorBatch.value?.status !== 'running') stopAuthorBatchPolling()
  } catch {
    stopAuthorBatchPolling()
  }
}
const startAuthorBatchPolling = (author: string) => {
  stopAuthorBatchPolling()
  authorBatchTimer = setInterval(() => { void loadAuthorBatch(author) }, 3000)
}
const loadAuthorTranscriptBatch = async (author: string) => {
  try {
    const response = await douyinApi.getAuthorTranscriptBatch(author) as any
    if (taskFilterAuthor.value !== author) return
    authorTranscriptBatch.value = response?.data || null
    if (authorTranscriptBatch.value) void fetchTasks({ silent: true })
    if (authorTranscriptBatch.value?.status !== 'running') stopAuthorTranscriptBatchPolling()
  } catch {
    stopAuthorTranscriptBatchPolling()
  }
}
const startAuthorTranscriptBatchPolling = (author: string) => {
  stopAuthorTranscriptBatchPolling()
  authorTranscriptBatchTimer = setInterval(() => { void loadAuthorTranscriptBatch(author) }, 3000)
}
const startAuthorSummary = async () => {
  const author = taskFilterAuthor.value
  if (!author) return
  try {
    const previewResponse = await douyinApi.previewAuthorSummaryBatch(author) as any
    const preview = previewResponse?.data || {}
    if (!preview.total) {
      ElMessage.warning('该作者没有已下载的单视频可总结')
      return
    }
    let requestLimit: number | null = null
    if (preview.to_summarize > 0) {
      const budget = await ElMessageBox.prompt(
        `待处理 ${preview.to_summarize} 个单视频；跳过已有总结 ${preview.already_summarized} 个、正在总结 ${preview.already_running} 个。每条分别生成，视频发送至已配置模型。最少约 ${preview.to_summarize} 次新增请求；分段、合并、重试和备用通道会增加次数，真实费用取决于上游用量与单价，当前无法完整预估。填写本批累计请求上限（留空不限）；达到上限暂停后续请求，在途请求仍可能完成并计费。${preview.previous_failures ? `其中 ${preview.previous_failures} 个是上次失败，将重新尝试。` : ''}`,
        '确认逐条总结',
        { confirmButtonText: '开始总结', cancelButtonText: '取消', type: 'warning', inputPlaceholder: '可选：请求次数上限', inputValidator: value => !value?.trim() || /^[1-9]\d*$/.test(value.trim()) || '请填写正整数，或留空不限' }
      )
      if (typeof budget === 'string') return
      requestLimit = budget.value?.trim() ? Number(budget.value) : null
    }
    const response = await douyinApi.createAuthorSummaryBatch(author, requestLimit) as any
    authorBatch.value = response.data as AuthorSummaryBatch
    if (authorBatch.value.status === 'running') {
      startAuthorBatchPolling(author)
      ElMessage.success('已开始逐条总结，可在作者画廊查看进度')
    } else {
      ElMessage.info(`没有新任务；已跳过已有总结 ${authorBatch.value.skipped_summarized} 个、正在总结 ${authorBatch.value.skipped_running} 个`)
    }
  } catch (error: any) {
    if (error !== 'cancel') ElMessage.error(error?.message || '批量总结启动失败')
  }
}
const startAuthorTranscript = async () => {
  const author = taskFilterAuthor.value
  if (!author) return
  try {
    const previewResponse = await douyinApi.previewAuthorTranscriptBatch(author) as any
    const preview = previewResponse?.data || {}
    if (!preview.total) {
      ElMessage.warning('该作者没有已下载的单视频可提取口播')
      return
    }
    let requestLimit: number | null = null
    if (preview.to_transcribe > 0) {
      const budget = await ElMessageBox.prompt(
        `待处理 ${preview.to_transcribe} 个单视频；跳过已有口播 ${preview.already_transcribed} 个、正在提取 ${preview.already_running} 个。每条独立保存；通常每条需要语音转写和文案整理各 1 次请求，已有检查点可减少调用，重试会增加次数。上游用量或价格缺失时费用未知。填写本批累计请求上限（留空不限）；达到上限暂停后续请求，已完成结果保留，在途请求仍可能完成并计费。${preview.previous_failures ? `其中 ${preview.previous_failures} 个是上次失败，将重新尝试。` : ''}`,
        '确认逐条提取口播',
        { confirmButtonText: '开始提取', cancelButtonText: '取消', type: 'warning', inputPlaceholder: '可选：请求次数上限', inputValidator: value => !value?.trim() || /^[1-9]\d*$/.test(value.trim()) || '请填写正整数，或留空不限' }
      )
      if (typeof budget === 'string') return
      requestLimit = budget.value?.trim() ? Number(budget.value) : null
    }
    const response = await douyinApi.createAuthorTranscriptBatch(author, requestLimit) as any
    authorTranscriptBatch.value = response.data as AuthorTranscriptBatch
    if (authorTranscriptBatch.value.status === 'running') {
      startAuthorTranscriptBatchPolling(author)
      ElMessage.success('已开始逐条提取口播，可在作者画廊查看进度')
    } else {
      ElMessage.info(`没有新任务；已跳过已有口播 ${authorTranscriptBatch.value.skipped_transcribed} 个、正在提取 ${authorTranscriptBatch.value.skipped_running} 个`)
    }
  } catch (error: any) {
    if (error !== 'cancel') ElMessage.error(error?.message || '批量提取口播启动失败')
  }
}
watch(taskFilterAuthor, (author) => {
  stopAuthorBatchPolling()
  stopAuthorTranscriptBatchPolling()
  authorBatch.value = null
  authorTranscriptBatch.value = null
  if (author) void Promise.all([loadAuthorBatch(author), loadAuthorTranscriptBatch(author)]).then(() => {
    if (taskFilterAuthor.value !== author) return
    if (authorBatch.value?.status === 'running') startAuthorBatchPolling(author)
    if (authorTranscriptBatch.value?.status === 'running') startAuthorTranscriptBatchPolling(author)
  })
})
watch(summaryDialogVisible, (open) => {
  if (!open) void fetchTasks({ silent: true })
})

const allCurrentPageSelected = computed(() => displayTasks.value.length > 0 && displayTasks.value.every(task => selectedTasks.value.some(item => item.id === task.id)))

// 分页相关
const currentPage = computed({
  get: () => taskPage.value,
  set: (val) => goToPage(val)
})

const currentPageSize = computed({
  get: () => taskPageSize.value,
  set: (val) => changePageSize(val)
})

// 分页事件处理
const handlePageChange = (page: number) => {
  goToPage(page)
}

const handleSizeChange = (size: number) => {
  changePageSize(size)
}

// WebSocket状态显示
const wsStatusType = computed(() => {
  const map: Record<string, string> = {
    'connected': 'success',
    'connecting': 'warning',
    'disconnected': 'info',
    'error': 'danger'
  }
  return map[wsStatus.value] || 'info'
})

const wsStatusLabel = computed(() => {
  const map: Record<string, string> = {
    'connected': '实时',
    'connecting': '连接中',
    'disconnected': useWebSocket.value ? '未连接' : '轮询',
    'error': '错误'
  }
  return map[wsStatus.value] || wsStatus.value
})

const wsStatusTooltip = computed(() => {
  if (useWebSocket.value && wsService.getLastError()) return wsService.getLastError()
  const map: Record<string, string> = {
    'connected': 'WebSocket已连接，实时接收更新',
    'connecting': '正在连接WebSocket...',
    'disconnected': useWebSocket.value ? 'WebSocket已断开，使用轮询模式' : '使用轮询模式（每10秒静默刷新）',
    'error': 'WebSocket连接错误，已回退到轮询模式'
  }
  return map[wsStatus.value] || wsStatus.value
})

// 状态标签映射
const getStatusType = (status: string) => {
  const map: Record<string, string> = {
    pending: 'info',
    queued: 'info',
    downloading: 'warning',
    paused: 'warning',
    completed: 'success',
    failed: 'danger',
    cancelled: 'info'
  }
  return map[status] || 'info'
}

const getStatusLabel = (status: string) => {
  const map: Record<string, string> = {
    pending: '待执行',
    queued: '队列中',
    downloading: '下载中',
    paused: '已暂停',
    completed: '已完成',
    failed: '失败',
    cancelled: '已取消'
  }
  return map[status] || status
}

const getSummaryStatusLabel = (status?: DownloadTask['summary_status']) => {
  const labels = { completed: '已总结', running: '总结中', failed: '总结失败', none: '未总结' }
  return labels[status || 'none']
}

const canDeleteLocalVideos = (row: DownloadTask) =>
  !row.is_batch_video &&
  !['pending', 'queued', 'downloading'].includes(row.status) &&
  (row.status === 'completed' || Number(row.result?.downloaded || 0) > 0)

const getHistoryAttemptLabel = (row: DownloadTask | null) => {
  const failedCount = getHistoryFailureCount(row)
  return failedCount > 0 ? `历史失败 ${failedCount}` : `历史尝试 ${getHistoryAttemptCount(row)}`
}

const getHistoryDescription = (row: DownloadTask | null) => {
  const failedCount = getHistoryFailureCount(row)
  return failedCount > 0
    ? `已合并 ${failedCount} 次失败尝试`
    : `已合并 ${getHistoryAttemptCount(row)} 条历史尝试`
}

// 类型标签映射
const getTypeTag = (type: string) => {
  const map: Record<string, string> = {
    single: 'primary',
    mix: 'success',
    mix_user: 'warning'
  }
  return map[type] || 'info'
}

const getTypeLabel = (type: string) => {
  const map: Record<string, string> = {
    single: '单视频',
    mix: '合集',
    mix_user: '用户合集',
    post: '用户作品'
  }
  return map[type] || type
}

/**
 * The API owns canonicalization and pagination. These helpers only render the
 * metadata returned with a canonical row; they intentionally never merge the
 * current page in the browser, which would make `taskTotal` lie at page
 * boundaries.
 */
// 进度条状态
const getProgressStatus = (status: string) => {
  if (status === 'failed') return 'exception'
  if (status === 'completed') return 'success'
  return ''
}

// 刷新任务列表
const refreshTasks = async () => {
  await fetchTasks({ status: taskFilterStatus.value || undefined, type: taskFilterType.value || undefined })
}

const checkLocalDeletionCapability = async () => {
  const status = await checkServiceStatus()
  localDeletionAvailable.value = status?.capabilities?.task_file_deletion === true
}

// 筛选变化
const handleFilterChange = () => {
  void setTaskFilters({ status: taskFilterStatus.value })
}

const handleAdvancedFilterChange = () => {
  void setTaskFilters({
    status: taskFilterStatus.value,
    type: taskFilterType.value,
    keyword: taskKeyword.value,
    sortBy: taskSortBy.value,
    sortOrder: taskSortOrder.value,
  })
}

const toggleSortOrder = () => {
  void setTaskFilters({ sortOrder: taskSortOrder.value === 'desc' ? 'asc' : 'desc' })
}

const handleReconnect = () => {
  reconnectWebSocket()
  ElMessage.info('正在重新连接实时任务更新')
}

// 重试任务
const handleRetry = async (taskId: string) => {
  try {
    await ElMessageBox.confirm('只重试失败或未完成项；已下载文件会保留并跳过。', '重试失败项', {
      confirmButtonText: '重试失败项',
      cancelButtonText: '取消',
      type: 'warning'
    })
    
    await retryTask(taskId)
    ElMessage.success('任务已添加到重试队列')
  } catch (error: any) {
    if (error !== 'cancel') {
      ElMessage.error(error.message || '重试失败')
    }
  }
}

const handleContinue = async (taskId: string) => {
  try {
    await ElMessageBox.confirm(
      '继续未完成项；已下载文件会保留并跳过。',
      '继续任务',
      { confirmButtonText: '继续未完成项', cancelButtonText: '取消', type: 'info' }
    )
    await continueTask(taskId)
    ElMessage.success('任务已继续执行')
  } catch (error: any) {
    if (error !== 'cancel') ElMessage.error(error.message || '继续任务失败')
  }
}

const handleCancel = async (taskId: string) => {
  try {
    await ElMessageBox.confirm(
      '取消后已完成文件会保留，可稍后继续未完成项。',
      '取消任务',
      { confirmButtonText: '确认取消', cancelButtonText: '返回', type: 'warning' }
    )
    await cancelTask(taskId)
    ElMessage.success('任务已取消')
  } catch (error: any) {
    if (error !== 'cancel') ElMessage.error(error.message || '取消任务失败')
  }
}

// 删除任务
const handleDelete = async (taskId: string) => {
  try {
    await ElMessageBox.confirm('仅删除任务记录，已下载文件不会删除；对应的口播文案和视频总结入口也会一并删除。', '删除任务记录', {
      confirmButtonText: '删除记录',
      cancelButtonText: '取消',
      type: 'error'
    })
    
    await deleteTask(taskId)
    ElMessage.success('任务记录已删除，下载文件已保留')
  } catch (error: any) {
    if (error !== 'cancel') {
      ElMessage.error(error.message || '删除失败')
    }
  }
}

const handleDeleteWithFiles = async (row: DownloadTask) => {
  if (!localDeletionAvailable.value) {
    ElMessage.warning('本地服务更新尚未加载，请待当前任务结束后重启服务再试')
    return
  }
  const title = row.video_name || '此任务'
  const fileCount = Number(row.result?.downloaded || (row.type === 'single' && row.status === 'completed' ? 1 : 0))
  try {
    await ElMessageBox.confirm(
      `将删除“${title}”的任务记录${fileCount ? `及 ${fileCount} 个` : '及关联的'}本地视频文件。视频文件不可恢复；对应的口播与总结作业记录也会删除。`,
      '删除记录和本地视频',
      { confirmButtonText: '删除记录和文件', cancelButtonText: '取消', type: 'error' }
    )
    const response = await deleteTask(row.id, true)
    ElMessage.success(response.message || '任务记录和本地视频已删除')
  } catch (error: any) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(error.message || '删除失败')
  }
}

// 复制ID
const copyId = async (id: string) => {
  try {
    await navigator.clipboard.writeText(id)
    ElMessage.success('ID已复制到剪贴板')
  } catch (err) {
    ElMessage.error('复制失败')
  }
}

// 获取本地视频链接（仅单视频）
const getMediaLink = (row: DownloadTask, preview = false): string | undefined => {
  // 只处理已完成的单视频任务
  if (row.status !== 'completed' || row.type !== 'single') return undefined
  
  const awemeId = row.video_info?.aweme_id || ''
  
  // 单视频类型
  if (row.file_name) {
    const suffix = preview ? '&preview=1' : ''
    return `/api/download/file/${encodeURIComponent(row.file_name)}?aweme_id=${encodeURIComponent(awemeId)}${suffix}`
  }
  
  return undefined
}

const getDownloadLink = (row: DownloadTask): string | undefined => getMediaLink(row)
const getPreviewLink = (row: DownloadTask): string | undefined => getMediaLink(row, true)
const getThumbnailLink = (row: DownloadTask): string => {
  const taskId = row.parent_task_id || row.id
  const awemeId = row.video_info?.aweme_id
  return awemeId && row.parent_task_id
    ? `/api/tasks/${encodeURIComponent(taskId)}/thumbnail?aweme_id=${encodeURIComponent(awemeId)}`
    : `/api/tasks/${encodeURIComponent(taskId)}/thumbnail`
}

// 获取任务ID（单视频用aweme_id，合集用mix_id，用户作品用sec_user_id）
const getTaskId = (row: DownloadTask): string => {
  if (row.type === 'single') return row.video_info?.aweme_id || ''
  if (row.type === 'post') return row.video_info?.sec_user_id || row.mix_id || ''
  return row.mix_id || row.video_info?.mix_id || ''
}

// 获取任务总数（用于进度 X/Y 展示）
const getTotalCount = (row: DownloadTask): number => {
  if (row.progress?.total) return row.progress.total
  const r = row.result || {}
  return (r.downloaded || 0) + (r.skipped || 0) + (r.failed || 0)
}

// 下载选项文本（日志弹窗用）
const getDownloadOptionsText = (row: DownloadTask | null): string => {
  if (!row?.download_options) return '无'
  const parts: string[] = []
  if (row.download_options.max_counts) parts.push(`限制 ${row.download_options.max_counts} 个`)
  if (row.download_options.interval && row.download_options.interval !== 'all') {
    parts.push(`日期 ${row.download_options.interval}`)
  }
  return parts.length ? parts.join('；') : '无'
}

// WebSocket 实时开关切换
const handleWsModeChange = (val: boolean | string | number) => {
  toggleWebSocketMode(Boolean(val))
  ElMessage.info(val ? '已启用实时推送' : '已切换为轮询模式')
}

// 表格多选变化
const handleSelectionChange = (rows: DownloadTask[]) => {
  selectedTasks.value = rows
}

const toggleTaskSelection = (task: DownloadTask, checked: boolean) => {
  if (checked && !selectedTasks.value.some(item => item.id === task.id)) {
    selectedTasks.value = [...selectedTasks.value, task]
  } else if (!checked) {
    selectedTasks.value = selectedTasks.value.filter(item => item.id !== task.id)
  }
}

const toggleSelectAllVisible = () => {
  if (allCurrentPageSelected.value) {
    const visibleIds = new Set(displayTasks.value.map(task => task.id))
    selectedTasks.value = selectedTasks.value.filter(task => !visibleIds.has(task.id))
  } else {
    const selectedById = new Map(selectedTasks.value.map(task => [task.id, task]))
    displayTasks.value.forEach(task => selectedById.set(task.id, task))
    selectedTasks.value = [...selectedById.values()]
  }
}

// 批量删除选中任务
const handleBatchDelete = async () => {
  if (selectedTasks.value.length === 0) return
  try {
    await ElMessageBox.confirm(
      `仅删除选中的 ${selectedTasks.value.length} 条任务记录，已下载文件不会删除；对应的口播文案和视频总结入口也会一并删除。`,
      '批量删除记录',
      { confirmButtonText: '删除记录', cancelButtonText: '取消', type: 'warning' }
    )
    let successCount = 0
    let failCount = 0
    for (const task of selectedTasks.value) {
      try {
        await deleteTask(task.id)
        successCount++
      } catch (err) {
        failCount++
        console.error(`删除任务 ${task.id} 失败:`, err)
      }
    }
    if (failCount === 0) {
      ElMessage.success(`已删除 ${successCount} 个任务`)
    } else {
      ElMessage.warning(`已删除 ${successCount} 个，失败 ${failCount} 个`)
    }
    selectedTasks.value = []
    refreshTasks()
  } catch (error: any) {
    if (error !== 'cancel') {
      ElMessage.error(error.message || '批量删除失败')
    }
  }
}

// 清空所有已完成任务
const handleClearCompleted = async () => {
  try {
    const resp = await douyinApi.getTasks({ status: 'completed', limit: 100000 }) as any
    const ids: string[] = (resp?.data?.tasks || []).map((t: any) => t.id)
    if (ids.length === 0) {
      ElMessage.info('没有已完成任务')
      return
    }
    await ElMessageBox.confirm(
      `即将清空 ${ids.length} 条已完成任务记录。已下载文件不会删除，但口播文案和视频总结入口会一并删除。`,
      '清空已完成记录',
      { confirmButtonText: `清空 ${ids.length} 条记录`, cancelButtonText: '取消', type: 'warning' }
    )
    let successCount = 0
    let failCount = 0
    for (const id of ids) {
      try {
        await deleteTask(id)
        successCount++
      } catch (err) {
        failCount++
        console.error(`删除任务 ${id} 失败:`, err)
      }
    }
    if (failCount === 0) {
      ElMessage.success(`已清空 ${successCount} 个任务`)
    } else {
      ElMessage.warning(`已清空 ${successCount} 个，失败 ${failCount} 个`)
    }
    refreshTasks()
  } catch (error: any) {
    if (error !== 'cancel') {
      ElMessage.error(error.message || '清空失败')
    }
  }
}

// 更多操作下拉
const handleMoreCommand = async (cmd: string, row: DownloadTask) => {
  switch (cmd) {
    case 'open-url':
      if (row.url) window.open(row.url, '_blank')
      break
    case 'copy-id': {
      const id = getTaskId(row)
      if (id) await copyId(id)
      break
    }
    case 'log':
      logTask.value = row
      logDialogVisible.value = true
      break
  }
}

const handleGalleryCommand = async (cmd: string, row: DownloadTask) => {
  if (cmd === 'transcript') openTranscript(row)
  else if (cmd === 'delete') await handleDelete(row.id)
  else await handleMoreCommand(cmd, row)
}

const openTaskLog = (row: DownloadTask) => {
  logTask.value = row
  logDialogVisible.value = true
}

const openVideoPreview = async (row: DownloadTask) => {
  previewTask.value = row
  videoPreviewVisible.value = true
  await nextTick()
  try {
    await previewVideo.value?.play()
  } catch {
    // 浏览器拒绝自动播放时，保留原生播放控件供用户启动。
  }
}

const openTranscript = (row: DownloadTask) => {
  transcriptTaskId.value = row.id
  transcriptDialogVisible.value = true
}

const openVideoSummary = (row: DownloadTask) => {
  summaryTaskId.value = row.id
  summaryDialogVisible.value = true
}

const scrollToTaskDesk = () => document.getElementById('task-desk')?.scrollIntoView({ behavior: 'smooth', block: 'start' })

// 格式化时间
const formatTime = (timeStr: string) => {
  if (!timeStr) return '-'
  const date = new Date(timeStr)
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  })
}

// 截断文本
const truncate = (str: string, maxLength: number) => {
  if (!str || str.length <= maxLength) return str
  return str.substring(0, maxLength) + '...'
}

// 组件挂载时启动WebSocket
onMounted(() => {
  fetchTasks()
  void checkLocalDeletionCapability()
  startWebSocket()
})

// 组件卸载时停止WebSocket
onUnmounted(() => {
  stopVideoPreview()
  stopAuthorBatchPolling()
  stopAuthorTranscriptBatchPolling()
  stopWebSocket()
})
</script>

<style scoped>
.author-gallery-layout { display: grid; grid-template-columns: 210px minmax(0, 1fr); gap: 22px; align-items: start; }
.author-rail { position: sticky; top: 16px; max-height: 640px; overflow-y: auto; border-right: 1px solid var(--color-line); padding-right: 14px; }
.author-rail-heading { display: flex; justify-content: space-between; padding: 5px 10px 12px; font-size: 12px; font-weight: 700; color: var(--color-text-muted); }
.author-option { display: flex; width: 100%; align-items: center; justify-content: space-between; gap: 10px; min-height: 40px; padding: 8px 10px; border: 0; border-radius: 4px; background: transparent; color: var(--color-ink-soft); font: inherit; text-align: left; cursor: pointer; }
.author-option span { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.author-option small { flex: 0 0 auto; color: var(--color-text-muted); }
.author-option:hover, .author-option:focus-visible { background: var(--color-surface-muted); }
.author-option.active { background: var(--color-ink); color: #fff; }
.author-option.active small { color: #d9e7e3; }
.gallery-content { min-width: 0; min-height: 220px; }
.gallery-heading { display: flex; align-items: end; justify-content: space-between; gap: 16px; margin-bottom: 15px; border-bottom: 1px solid var(--color-line); padding-bottom: 12px; color: var(--color-text-muted); font-size: 12px; }
.gallery-kicker { font-size: 11px; color: var(--color-text-muted); }
.gallery-heading h3 { margin: 3px 0 0; color: var(--color-ink); font-size: 18px; font-weight: 700; }
.gallery-heading-actions { display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: 8px 12px; }
.gallery-heading-actions :deep(.el-button) { margin: 0; }
.author-summary-progress { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; margin: 0 0 14px; padding: 10px 12px; border-left: 3px solid var(--color-accent); background: var(--color-surface-muted); color: var(--color-ink-soft); font-size: 12px; }
.author-summary-progress strong { color: var(--color-ink); }
.transcript-batch-progress { border-left-color: var(--color-success, #2f855a); }
.task-gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 18px; }
.gallery-item { min-width: 0; border: 1px solid var(--color-line); border-radius: 4px; background: var(--color-white); overflow: hidden; }
.gallery-cover { position: relative; aspect-ratio: 9 / 12; background: #e8eeeb; overflow: hidden; }
.gallery-cover img { display: block; width: 100%; height: 100%; object-fit: cover; }
.gallery-cover-play { position: absolute; inset: 0; z-index: 1; display: flex; width: 100%; height: 100%; align-items: center; justify-content: center; border: 0; padding: 0; background: transparent; cursor: pointer; }
.gallery-play-icon { display: flex; width: 48px; height: 48px; align-items: center; justify-content: center; border: 1px solid rgba(255,255,255,.8); border-radius: 50%; background: rgba(12,22,21,.72); color: #fff; font-size: 22px; transition: background .16s ease, transform .16s ease; }
.gallery-cover-play:hover .gallery-play-icon { transform: scale(1.08); background: rgba(12,22,21,.9); }
.gallery-cover-play:focus-visible { outline: 3px solid #fff; outline-offset: -6px; }
.gallery-cover-play:focus-visible .gallery-play-icon { box-shadow: 0 0 0 3px #173f39; }
.gallery-cover-empty { display: flex; height: 100%; flex-direction: column; align-items: center; justify-content: center; gap: 8px; color: #5d7771; font-size: 12px; }
.gallery-cover-empty .el-icon { font-size: 34px; }
.gallery-status { position: absolute; z-index: 2; bottom: 10px; left: 10px; padding: 4px 7px; border-radius: 3px; background: #fff; color: var(--color-ink); font-size: 11px; font-weight: 700; }
.gallery-status.is-failed { color: var(--color-danger); }
.gallery-status.is-downloading { color: var(--color-cyan-deep); }
.gallery-summary-status { position: absolute; z-index: 2; top: 10px; left: 10px; padding: 5px 8px; border-radius: 3px; background: rgba(255,255,255,.94); color: #51605e; font-size: 11px; font-weight: 700; }
.gallery-summary-status.is-completed { background: #d9f1e5; color: #17654b; }
.gallery-summary-status.is-running { background: #dcecf2; color: #175c72; }
.gallery-summary-status.is-failed { background: #fbe5e2; color: #9c342b; }
.gallery-select { position: absolute; z-index: 2; top: 9px; right: 9px; padding: 3px 5px; border-radius: 3px; background: rgba(255,255,255,.92); }
.gallery-details { padding: 11px; }
.gallery-meta { display: flex; justify-content: space-between; gap: 8px; color: var(--color-text-muted); font-size: 11px; }
.gallery-meta span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.gallery-details h4 { display: -webkit-box; min-height: 40px; margin: 7px 0; overflow: hidden; -webkit-line-clamp: 2; -webkit-box-orient: vertical; color: var(--color-ink); font-size: 13px; line-height: 20px; }
.gallery-result { display: flex; justify-content: space-between; gap: 8px; min-height: 18px; color: var(--color-text-muted); font-size: 11px; }
.gallery-result span:last-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.gallery-progress-message { margin: 7px 0 0; color: var(--color-text-muted); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.gallery-result > span:first-child { flex-shrink: 0; }
.gallery-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; min-height: 33px; margin-top: 10px; border-top: 1px solid var(--color-line); padding-top: 9px; }
.gallery-actions :deep(.el-button) { margin-left: 0; }
.local-delete-trigger { display: inline-flex; }
.gallery-download { display: inline-flex; width: 28px; height: 24px; align-items: center; justify-content: center; color: var(--color-ink); }
@media (max-width: 900px) { .author-gallery-layout { display: none; } }
.download-records {
  margin-bottom: 20px;
}

.floating-task-tray {
  position: fixed;
  right: 24px;
  bottom: 24px;
  z-index: 20;
  width: min(340px, calc(100vw - 32px));
  padding: 14px 16px;
  border: 1px solid rgba(25, 196, 189, .45);
  border-radius: 12px;
  background: rgba(11, 27, 42, .96);
  box-shadow: 0 12px 34px rgba(11, 27, 42, .24);
  color: #fff;
}

.floating-task-heading,
.floating-task-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.floating-task-heading { margin-bottom: 8px; font-size: 13px; }
.floating-task-heading .el-button { min-height: 30px; color: #74dfc1; }
.floating-task-item { padding-top: 7px; border-top: 1px solid rgba(255,255,255,.12); font-size: 12px; }
.floating-task-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #d9e2eb; }
.floating-task-status { flex: 0 0 auto; color: #74dfc1; font-size: 11px; }

/* 批量操作工具条 */
.batch-bar {
  display: flex;
  gap: 10px;
  margin-bottom: 12px;
}

/* WebSocket 实时开关 */
.ws-switch {
  display: flex;
  align-items: center;
  gap: 6px;
}

.ws-label {
  font-size: 13px;
  color: var(--color-accent-deep);
  font-weight: 500;
}

.ws-label.ws-off {
  color: #909399;
}

/* 进度百分比文本 */
.percent-text {
  color: #606266;
  font-size: 12px;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-weight: bold;
  font-size: 16px;
}

.header-actions {
  display: flex;
  gap: 10px;
  align-items: center;
}

.video-info {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.download-records :deep(.completed-history-row) {
  opacity: .72;
  background: #fbfbf8;
}

.history-label {
  display: block;
  margin-top: 3px;
  color: #9aa3ad;
  font-size: 10px;
}

.video-name {
  font-weight: 500;
  color: #303133;
  line-height: 1.4;
  /* 单行显示，超出部分省略 */
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 100%;
}

.video-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 12px;
  color: #909399;
}

.history-attempt-tag {
  max-width: 150px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.aweme-id {
  font-family: monospace;
}

.progress-wrapper {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.current-video {
  font-size: 11px;
  color: #909399;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.result-summary {
  display: flex;
  gap: 10px;
  font-size: 12px;
}

.success-count {
  color: #67c23a;
}

.skip-count {
  color: #e6a23c;
}

.fail-count {
  color: #f56c6c;
}

.file-name {
  font-size: 12px;
  color: #606266;
  background: #f5f7fa;
  padding: 2px 6px;
  border-radius: 4px;
}

.mix-id-cell {
  display: flex;
  align-items: center;
  gap: 4px;
}

.mix-id-text {
  font-family: monospace;
  font-size: 11px;
  color: #606266;
  max-width: 100px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.copy-mix-btn {
  padding: 2px;
  min-height: 20px;
}

.download-link-cell {
  display: flex;
  align-items: center;
}

.download-link {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  background: var(--color-primary);
  color: #fff;
  border-radius: 4px;
  text-decoration: none;
  font-size: 12px;
  transition: background 0.3s;
}

.download-link:hover {
  background: var(--color-accent);
}

.link-text {
  font-size: 12px;
}

.no-download {
  font-size: 12px;
  color: #909399;
}

.ws-status-tag {
  display: flex;
  align-items: center;
  gap: 4px;
}

.ws-status-tag .el-icon {
  font-size: 14px;
}

.pagination-wrapper {
  display: flex;
  justify-content: flex-end;
  padding: 16px 0 0 0;
  margin-top: 16px;
  border-top: 1px solid #ebeef5;
}

/* 文件单元格样式 */
.file-cell {
  display: flex;
  align-items: center;
}

/* 过滤条件单元格样式 */
.filter-cell {
  display: flex;
  align-items: center;
}

.filter-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

/* 下载按钮单元格样式 */
.download-cell {
  display: flex;
  align-items: center;
}

.failed-items {
  margin: 0;
  padding-left: 18px;
}

.history-attempt-summary {
  display: flex;
  flex-direction: column;
  gap: 8px;
  color: #606266;
}

.history-attempts {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0;
  padding-left: 18px;
}

.history-attempts li {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  line-height: 1.45;
}

.history-attempts li span:last-child {
  color: #909399;
}

.task-history-note {
  margin-top: 8px;
  color: #909399;
  font-size: 12px;
}

.task-cards {
  display: none;
}

@media (max-width: 760px) {
  .download-records :deep(.el-table) {
    display: none;
  }

  .card-header,
  .header-actions,
  .batch-bar {
    align-items: stretch;
    flex-direction: column;
  }

  .header-actions {
    width: 100%;
    margin-top: 10px;
  }

  .header-actions :deep(.el-radio-group) {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
  }

  .task-cards {
    display: grid;
    gap: 12px;
  }

  .task-card {
    padding: 14px;
    background: #fff;
    border: 1px solid #d8dee5;
    border-left: 4px solid var(--color-ink);
  }

  .task-card__header {
    display: flex;
    justify-content: space-between;
    gap: 12px;
  }

  .task-card__header h3 {
    margin: 4px 0 10px;
    font-size: 15px;
    line-height: 1.45;
  }

  .task-card__type {
    color: #65727f;
    font-size: 12px;
  }

  .task-card__stats {
    display: flex;
    gap: 14px;
    margin-bottom: 10px;
    color: #52606c;
    font-size: 13px;
  }

  .task-card__error {
    padding: 9px 10px;
    color: var(--color-ink);
    background: var(--color-surface);
    border-left: 3px solid var(--color-danger);
  }

  .task-card__path {
    width: 100%;
    min-height: 44px;
    margin: 10px 0;
    padding: 8px 10px;
    color: var(--color-ink-soft);
    text-align: left;
    overflow-wrap: anywhere;
    background: var(--color-surface);
    border: 1px solid var(--color-line);
    cursor: copy;
  }

  .task-card__actions {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }

  .task-card__actions :deep(.el-button) {
    min-height: 44px;
    margin: 0;
  }

  .pagination-wrapper {
    overflow-x: auto;
    justify-content: flex-start;
  }
}

/* Workbench task desk visual layer; business actions above remain unchanged. */
.download-records { margin-top: 20px; margin-bottom: 0; border: 1px solid var(--color-line); border-radius: var(--radius-lg); box-shadow: var(--shadow-soft); background: var(--color-surface); }
.download-records :deep(.el-card__header) { padding: 24px 28px 18px; border-bottom: 1px solid var(--color-line); }
.download-records :deep(.el-card__body) { padding: 16px 28px 24px; }
.records-eyebrow { margin: 0 0 6px; color: var(--color-accent); font: 500 11px var(--font-mono); letter-spacing: .12em; text-transform: uppercase; }
.card-header h2 { margin: 0; color: var(--color-ink); font: 700 25px/1 var(--font-display); letter-spacing: -.02em; }
.header-actions { flex-wrap: wrap; align-items: center; gap: 10px; }
.task-search { width: 220px; }
.task-type-filter, .task-sort { width: 120px; }
.refresh-button { min-height: 38px; border-radius: 9px; }
.batch-bar { padding: 12px 0; border-bottom: 1px solid var(--color-line); }
.desktop-task-table { margin-top: 14px; }
.desktop-task-table :deep(th.el-table__cell) { color: var(--color-text-muted); background: var(--color-surface-muted); font: 700 11px var(--font-mono); letter-spacing: .05em; text-transform: uppercase; }
.desktop-task-table :deep(td.el-table__cell) { padding: 12px 0; border-bottom-color: var(--color-line); }
.copy-mix-btn { min-width: 36px; min-height: 36px; }
.desktop-task-table :deep(.el-table-fixed-column--right .el-button.is-circle) { width: 38px; min-width: 38px; height: 38px; }
.mobile-task-list { display: none; }
.task-card { padding: 16px; border: 1px solid var(--color-line); border-left: 4px solid var(--color-info); border-radius: var(--radius-md); background: var(--color-surface); }
.task-card + .task-card { margin-top: 12px; }.task-card.status-downloading { border-left-color: var(--color-accent); }.task-card.status-completed { border-left-color: var(--color-ink); }.task-card.status-failed { border-left-color: var(--color-danger); }
.task-card-header { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: start; gap: 10px; }.task-card-title { min-width: 0; }.task-card-title h3 { margin: 4px 0 0; overflow: hidden; color: var(--color-ink); font-size: 14px; line-height: 1.4; text-overflow: ellipsis; white-space: nowrap; }.task-type { color: var(--color-text-muted); font: 700 10px var(--font-mono); letter-spacing: .06em; }.task-status { display: inline-flex; align-items: center; gap: 6px; padding: 5px 8px; border: 1px solid currentColor; border-radius: 999px; color: var(--color-info); font-size: 11px; font-weight: 700; white-space: nowrap; }.task-status span { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }.task-status.is-downloading { color: var(--color-cyan-deep); }.task-status.is-completed { color: var(--color-success); }.task-status.is-failed { color: var(--color-danger); }.task-status.is-pending, .task-status.is-queued { color: var(--color-warning); }
.task-card-progress { margin-top: 16px; padding: 12px; border-radius: var(--radius-sm); background: var(--color-surface-muted); }.task-card-progress > div { display: flex; justify-content: space-between; margin-bottom: 8px; font-size: 12px; }.task-card-progress p { margin: 8px 0 0; color: var(--color-text-muted); font-size: 11px; }.task-result { margin-top: 14px; padding: 9px 11px; color: var(--color-text-muted); border-radius: var(--radius-sm); background: var(--color-surface-muted); font-size: 12px; }.task-result.is-complete { color: var(--color-ink); }.task-result.is-failed { color: var(--color-danger); }.task-facts { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin: 14px 0 0; }.task-facts div { min-width: 0; }.task-facts dt { color: var(--color-text-muted); font-size: 10px; }.task-facts dd { margin: 4px 0 0; overflow: hidden; color: var(--color-ink-soft); font: 11px var(--font-mono); text-overflow: ellipsis; white-space: nowrap; }.task-card-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; padding-top: 13px; border-top: 1px solid var(--color-line); }.task-card-actions .el-button { min-height: 44px; margin-left: 0; }.mobile-download-link { min-height: 44px; display: inline-flex; align-items: center; gap: 5px; padding: 0 12px; color: var(--color-white); background: var(--color-ink); border-radius: var(--radius-sm); font-size: 12px; font-weight: 700; text-decoration: none; }
@media (max-width: 900px) { .download-records :deep(.el-card__header) { padding: 20px 18px 16px; }.download-records :deep(.el-card__body) { padding: 14px 14px 18px; }.desktop-task-table { display: none; }.mobile-task-list { display: block; }.header-actions { width: 100%; }.header-actions :deep(.el-radio-group) { width: 100%; display: grid; grid-template-columns: repeat(4, 1fr); }.header-actions :deep(.el-radio-button), .header-actions :deep(.el-radio-button__inner) { width: 100%; }.header-actions :deep(.el-radio-button__inner) { min-height: 44px; }.ws-switch, .refresh-button { min-height: 44px; }.task-facts { grid-template-columns: 1fr 1fr; } }
@media (max-width: 520px) { .floating-task-tray { right: 16px; bottom: 16px; width: calc(100vw - 32px); } }
.records-error { display: grid; gap: 12px; margin: 18px 0; }
.records-error .el-button { justify-self: start; min-height: 44px; }
:deep(.video-preview-dialog) { overflow: hidden; border-radius: 6px; background: #090d0e; }
:deep(.video-preview-dialog .el-dialog__header) { margin-right: 0; padding: 12px 16px; background: #090d0e; color: #f3f7f5; }
:deep(.video-preview-dialog .el-dialog__title) { color: #f3f7f5; font-size: 14px; }
:deep(.video-preview-dialog .el-dialog__headerbtn .el-dialog__close) { color: #f3f7f5; }
:deep(.video-preview-dialog .el-dialog__body) { padding: 0 12px 12px; background: #090d0e; }
.video-preview-stage { display: flex; width: 100%; height: min(82vh, 900px); align-items: center; justify-content: center; overflow: hidden; background: #000; }
.video-preview-stage video { display: block; width: auto; height: auto; max-width: 100%; max-height: 100%; object-fit: contain; }
.download-cell { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.preview-link { min-height: 32px; padding: 0 4px; }
@media (max-width: 900px) { .task-search, .task-type-filter, .task-sort { width: 100%; }.header-actions > .el-button, .header-actions > .el-select, .header-actions > .el-input { width: 100%; }.header-actions > .el-button { justify-content: center; } }
@media (max-width: 420px) { .task-card-header { grid-template-columns: auto minmax(0, 1fr); }.task-status { grid-column: 2; justify-self: start; }.task-facts { grid-template-columns: 1fr; }.task-card-actions .el-button, .mobile-download-link { flex: 1; justify-content: center; min-width: calc(50% - 4px); } }

</style>
