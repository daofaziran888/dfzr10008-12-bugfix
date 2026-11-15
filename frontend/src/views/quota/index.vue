<template>
  <section class="page" data-module="quota">
    <header class="page-head">
      <div>
        <h2>调用配额控制管理</h2>
        <p class="page-desc">维护调用配额的上限设置、超限策略与有效期管理，为调用配额控制提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记配额配置</button>
        <button class="btn" type="button" @click="exportRows">导出调用配额控制清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <form v-if="showCreate" class="create-panel" @submit.prevent="submitCreate">
      <h3>登记配额配置</h3>
      <div class="create-grid">
        <label v-for="field in createFields" :key="field" class="filter-item">
          <span>{{ field }}</span>
          <input v-model="createForm[field]" :placeholder="`请输入${field}`" />
        </label>
      </div>
      <div class="create-actions">
        <button class="btn primary" type="submit">保存配额配置</button>
        <button class="btn ghost" type="button" @click="showCreate = false">取消</button>
      </div>
    </form>

    <p v-if="receiptMessage" class="receipt-text">{{ receiptMessage }}</p>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="openDetail(row)">详情</button>
            <button class="link" type="button" @click="admitCall(row)">模拟调用</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无调用配额控制数据，可先登记配额配置</td>
        </tr>
      </tbody>
    </table>

    <aside v-if="detail" class="detail-panel">
      <header class="detail-head">
        <h3>配额配置详情：{{ detail.配置编号 }}</h3>
        <button class="btn ghost" type="button" @click="detail = null">关闭</button>
      </header>
      <dl class="detail-grid">
        <template v-for="field in detailFields" :key="field">
          <dt>{{ field }}</dt>
          <dd>{{ detail[field] ?? '—' }}</dd>
        </template>
        <dt>审计单号</dt>
        <dd>{{ detail.审计单号 ?? '—' }}</dd>
        <dt>审计轨迹</dt>
        <dd>
          <span v-if="!auditTrail.length">—</span>
          <span v-for="item in auditTrail" :key="item.event" class="audit-item">
            {{ item.event }}：{{ item.time }}
          </span>
        </dd>
        <dt>并发窗口</dt>
        <dd>
          <span v-if="!windowStates.length">暂无调用准入状态</span>
          <span v-for="state in windowStates" :key="state.调用编号" class="window-item">
            {{ state.调用编号 }} · {{ state.结果 }}<template v-if="state.拒绝原因">（{{ state.拒绝原因 }}）</template>
          </span>
        </dd>
      </dl>
    </aside>

    <footer class="page-foot">
      <span>共 {{ total }} 条调用配额控制记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/quota'
const columns = ["配置编号", "调用方名称", "每秒上限", "日调用上限", "并发限制", "超限策略", "有效期至", "配额状态"]
const actions = ["生效配额", "调整配额", "取消配额"]
const createFields = ["配置编号", "调用方名称", "每秒上限", "日调用上限", "并发限制", "超限策略", "有效期至"]
const detailFields = ["配置编号", "调用方名称", "每秒上限", "日调用上限", "并发限制", "超限策略", "有效期至", "配额状态"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const receiptMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const createForm = ref<Record<string, string>>({})
const detail = ref<Row | null>(null)

const stats = computed(() => [
  { label: '配额配置总数', value: total.value },
  { label: '已生效配额', value: rows.value.filter((row) => row.配额状态 === '已生效').length },
  { label: '已超额配额', value: rows.value.filter((row) => row.配额状态 === '已超额').length },
])

const auditTrail = computed(() => {
  const trail = (detail.value?.审计轨迹 ?? {}) as Record<string, string>
  return Object.entries(trail).map(([event, time]) => ({ event, time }))
})

const windowStates = computed(() => (detail.value?.并发窗口 ?? []) as Row[])

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  createForm.value = {}
  showCreate.value = true
}

async function submitCreate() {
  errorMessage.value = ''
  receiptMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: { ...createForm.value } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '配额配置登记失败')
    }
    receiptMessage.value = payload.message || '配额配置已登记'
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '配额配置登记失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  receiptMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '调用配额控制动作未生效，请稍后重试')
    }
    receiptMessage.value = payload.message || `配额配置已${action}`
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '调用配额控制操作失败'
  }
}

async function openDetail(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}`)
    if (!response.ok) {
      throw new Error('配额配置详情读取失败')
    }
    detail.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '配额配置详情读取失败'
  }
}

async function admitCall(row: Row) {
  errorMessage.value = ''
  receiptMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/admit`, {
      method: 'POST',
      body: JSON.stringify({ values: {} }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      errorMessage.value = payload.message || '调用被拒绝'
    } else {
      receiptMessage.value = payload.message || '调用已放行'
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '调用准入判断失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('配额配置列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '调用配额控制列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.create-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 16px;
  margin-bottom: 12px;
}
.create-panel h3 {
  margin: 0 0 10px;
  font-size: 14px;
}
.create-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 10px;
}
.create-actions {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}
.receipt-text {
  color: #067647;
  font-size: 13px;
  margin: 8px 0;
}
.detail-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 16px;
  margin-top: 12px;
}
.detail-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.detail-head h3 {
  margin: 0;
  font-size: 14px;
}
.detail-grid {
  display: grid;
  grid-template-columns: 120px 1fr;
  gap: 6px 12px;
  font-size: 13px;
  margin: 12px 0 0;
}
.detail-grid dt {
  color: var(--muted);
}
.detail-grid dd {
  margin: 0;
}
.audit-item,
.window-item {
  display: block;
}
</style>
