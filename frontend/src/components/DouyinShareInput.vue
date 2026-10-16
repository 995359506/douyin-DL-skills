<template>
  <div class="share-input">
    <el-input
      :id="inputId"
      :model-value="modelValue"
      type="textarea"
      :autosize="{ minRows: 2, maxRows: 4 }"
      :placeholder="placeholder"
      :aria-label="placeholder"
      resize="vertical"
      clearable
      @update:model-value="updateValue"
      @keydown.meta.enter.prevent="submit"
      @keydown.ctrl.enter.prevent="submit"
    />

    <div
      v-if="result.status === 'single'"
      class="recognition recognition--success"
      role="status"
      aria-live="polite"
    >
      <el-icon aria-hidden="true"><CircleCheckFilled /></el-icon>
      <div class="recognition-copy">
        <strong>{{ result.message }}</strong>
        <code :title="result.url">{{ result.url }}</code>
      </div>
      <el-button
        link
        type="primary"
        class="clear-link"
        aria-label="清除已粘贴的分享内容"
        @click="clear"
      >
        清除重贴
      </el-button>
    </div>

    <div
      v-else-if="result.status === 'multiple'"
      class="recognition recognition--multiple"
      role="alert"
      aria-live="polite"
    >
      <el-icon aria-hidden="true"><WarningFilled /></el-icon>
      <div class="multiple-copy">
        <strong>{{ result.message }}</strong>
        <div class="multiple-url-list">
          <el-button
            v-for="url in result.urls"
            :key="url"
            link
            type="primary"
            class="multiple-url"
            :aria-label="`选择链接：${url}`"
            @click="choose(url)"
          >{{ url }}</el-button>
        </div>
      </div>
      <el-button link type="danger" class="clear-link" aria-label="清除多个链接并重新粘贴" @click="clear">清除重贴</el-button>
    </div>

    <div
      v-else-if="result.status !== 'empty'"
      class="recognition recognition--error"
      role="alert"
      aria-live="polite"
    >
      <el-icon aria-hidden="true"><WarningFilled /></el-icon>
      <span>{{ result.message }}</span>
      <el-button
        link
        type="danger"
        class="clear-link"
        aria-label="清除未识别的分享内容"
        @click="clear"
      >
        清除重贴
      </el-button>
    </div>

    <div class="share-action-row">
      <span class="keyboard-hint">粘贴后自动识别 · ⌘/Ctrl + Enter 可提交</span>
      <el-button
        type="primary"
        :loading="loading"
        :disabled="result.status !== 'single' || disabled"
        :aria-label="actionLabel"
        @click="submit"
      >
        {{ actionLabel }}
      </el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { parseDouyinShareText, type DouyinUrlKind } from '@/utils/douyinShareText'

const props = withDefaults(defineProps<{
  modelValue: string
  inputId?: string
  kind: DouyinUrlKind
  placeholder: string
  actionLabel: string
  loading?: boolean
  disabled?: boolean
}>(), {
  inputId: undefined,
  loading: false,
  disabled: false
})

const emit = defineEmits<{
  'update:modelValue': [value: string]
  submit: [url: string, shareText: string]
}>()

const result = computed(() => parseDouyinShareText(props.modelValue, props.kind))

const updateValue = (value: string) => emit('update:modelValue', value)
const clear = () => emit('update:modelValue', '')
const choose = (url: string) => emit('update:modelValue', url)
const submit = () => {
  if (result.value.status === 'single' && result.value.url && !props.disabled) {
    emit('submit', result.value.url, props.modelValue)
  }
}
</script>

<style scoped>
.share-input {
  display: grid;
  gap: 10px;
  margin-bottom: 12px;
}

.share-input :deep(.el-textarea__inner) {
  min-height: 68px !important;
  border-radius: var(--radius-md);
  font-family: var(--font-body);
  line-height: 1.55;
}

.recognition {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  min-height: 46px;
  padding: 8px 10px;
  border: 1px solid var(--color-line);
  border-left-width: 4px;
  background: var(--color-white);
  color: var(--color-ink-soft);
}

.recognition--success {
  border-left-color: var(--color-accent);
  background: var(--color-surface);
}

.recognition--success > .el-icon {
  color: var(--color-accent);
}

.recognition--error {
  border-left-color: var(--color-danger);
  background: var(--color-surface);
  color: var(--color-danger);
}

.recognition--error > .el-icon {
  color: var(--color-danger);
}

.recognition--multiple {
  border-left-color: var(--color-ink);
  background: var(--color-surface);
  color: var(--color-warning);
}

.recognition--multiple > .el-icon {
  color: var(--color-ink);
}

.multiple-copy {
  min-width: 0;
  display: grid;
  gap: 4px;
}

.multiple-copy strong {
  font-size: 13px;
}

.multiple-url-list {
  display: grid;
  gap: 2px;
}

.multiple-url {
  justify-content: flex-start;
  min-height: 28px;
  max-width: 100%;
  padding: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.recognition-copy {
  min-width: 0;
  display: grid;
  gap: 2px;
}

.recognition-copy strong {
  font-size: 13px;
}

.recognition-copy code {
  overflow: hidden;
  color: var(--color-text-muted);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.clear-link {
  min-height: 36px;
  padding-inline: 8px;
}

.share-action-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.keyboard-hint {
  color: var(--color-text-muted);
  font-size: 12px;
}

@media (max-width: 720px) {
  .recognition {
    grid-template-columns: auto minmax(0, 1fr);
  }

  .recognition .clear-link {
    grid-column: 1 / -1;
    justify-self: stretch;
    min-height: 44px;
  }

  .share-action-row {
    align-items: stretch;
    flex-direction: column;
  }

  .share-action-row .el-button {
    width: 100%;
    min-height: 44px;
  }
}
</style>
