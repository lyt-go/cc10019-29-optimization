<template>
  <section class="page" data-module="retention_calc">
    <header class="page-head">
      <div><h2>留存计算</h2><p class="page-desc">按批次合并读取留存事件、登记各组入组分母与回访区间；支持续做、重试、补报更正与已确认结果复用。</p></div>
    </header>

    <div class="analysis-summary">
      <article v-for="m in summaryMetrics" :key="m.label" class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">{{ m.label }}</span>
        <strong style="font-size:22px;display:block">{{ m.value }}</strong>
        <span class="trend-up" style="font-size:12px">{{ m.hint }}</span>
      </article>
    </div>

    <form class="analysis-form" @submit.prevent="runAnalysis">
      <label><span>配置编号（逗号分隔）</span><input v-model="params.configIds" placeholder="RC-001,RC-002" /></label>
      <label><span>本次只算前几组</span><input v-model="params.computeLimit" placeholder="留空=全部" /></label>
      <label><span>补报配置编号</span><input v-model="params.correctConfig" placeholder="仅可更正已算完的组" /></label>
      <label><span>补报入组人数</span><input v-model="params.cohortDenominator" placeholder="新的入组分母" /></label>
      <button class="btn primary" type="submit" data-testid="run-analysis">运行分析</button>
      <button class="btn" type="button" @click="resumeBatch" :disabled="!currentBatch">续做</button>
      <button class="btn" type="button" @click="retryBatch" :disabled="!currentBatch">重试</button>
      <button class="btn" type="button" @click="supplement" :disabled="!currentBatch">补报</button>
      <button class="btn" type="button" @click="confirmBatch" :disabled="!currentBatch">确认</button>
    </form>

    <p v-if="message" style="font-size:13px" data-testid="batch-message">{{ message }}</p>
    <p v-if="retrySummary" style="font-size:12px;color:var(--muted)" data-testid="retry-summary">
      补报影响：{{ retrySummary.affected_by_correction.length ? retrySummary.affected_by_correction.map((a: any) => `${a.config_id}(v${a.previous_version}→v${a.result_version})`).join('、') : '无' }}
      ；重算 {{ retrySummary.recomputed.length }} 组，补算 {{ retrySummary.computed.length }} 组，
      复用 {{ retrySummary.reused.length }} 组，保留 {{ retrySummary.kept.length }} 组。
    </p>

    <div v-if="chartData.length" class="analysis-chart">
      <div v-for="(bar, i) in chartData" :key="i" class="analysis-bar" :style="{ height: barHeight(bar.value) + 'px' }"><span>D{{ bar.label }}</span></div>
    </div>

    <table class="data-table" v-if="groups.length">
      <thead><tr><th v-for="c in resultColumns" :key="c">{{ c }}</th></tr></thead>
      <tbody>
        <tr v-for="g in groups" :key="g.group_key" :data-status="g.status">
          <td>{{ g.config_id }}</td>
          <td>{{ g.status }}</td>
          <td>{{ g.cohort_denominator }}</td>
          <td>{{ g.return_window.start }} ~ {{ g.return_window.end }}</td>
          <td>{{ g.result_version ? `v${g.result_version}` : '—' }}</td>
          <td>{{ g.reused ? `复用 ${g.reused_from?.batch_id} / v${g.reused_from?.result_version}` : '本批计算' }}</td>
        </tr>
      </tbody>
    </table>
    <p v-if="!groups.length && !loading" style="text-align:center;color:var(--muted)">提交留存批次后查看批次明细</p>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { request } from '@/api/client'

const ENDPOINT = '/api/retention-batch'
const batches = ref<any[]>([])
const currentBatch = ref<any>(null)
const message = ref('')
const loading = ref(false)
const params = ref<Record<string, string>>({ configIds: 'RC-001,RC-002', computeLimit: '', correctConfig: '', cohortDenominator: '' })

const summaryMetrics = computed(() => [
  { label: '留存批次', value: batches.value.length, hint: '全部登记批次' },
  { label: '待算分组', value: batches.value.reduce((n, b) => n + (b.pending_count || 0), 0), hint: '可续做' },
  { label: '已确认批次', value: batches.value.filter(b => b.confirmed).length, hint: '可供预测校准' },
  { label: '复用结果', value: batches.value.reduce((n, b) => n + (b.reused_count || 0), 0), hint: '已确认结果复用' },
])

const groups = computed<any[]>(() => currentBatch.value?.groups || [])
const resultColumns = ['配置编号', '状态', '入组分母', '回访区间', '结果版本', '来源']
const chartData = computed(() => {
  const group = groups.value.find((g: any) => (g.calibration_curve || []).length)
  return (group?.calibration_curve || []).map((value: number, index: number) => ({ label: String(index), value }))
})
const retrySummary = computed(() => currentBatch.value?.retry_summary || null)

function barHeight(v: number) { const max = Math.max(...chartData.value.map((d: { value: number }) => d.value), 1); return Math.max(10, (v / max) * 140) }
function parseConfigIds() { return params.value.configIds.split(',').map(s => s.trim()).filter(Boolean) }
function parseLimit() { return params.value.computeLimit ? Number(params.value.computeLimit) : null }

async function json(path: string, init?: RequestInit) {
  const res = await request(`${ENDPOINT}${path}`, init)
  const data = await res.json()
  if (!res.ok) throw new Error(data?.detail || '请求失败')
  return data
}
async function reload() {
  try { const data = await request(ENDPOINT).then(r => r.json()); batches.value = data.items || [] } catch (e) { batches.value = [] }
}
async function refreshBatch(batchId?: string) {
  const id = batchId || currentBatch.value?.batch_id
  if (!id) return
  currentBatch.value = await json(`/${id}`)
  await reload()
}
async function runAnalysis() {
  loading.value = true
  try {
    const payload: Record<string, unknown> = { config_ids: parseConfigIds() }
    if (parseLimit() !== null) payload.compute_limit = parseLimit()
    const data = await json('/submit', { method: 'POST', body: JSON.stringify(payload) })
    message.value = data.message
    await refreshBatch(data.entry.batch_id)
  } catch (e) { message.value = (e as Error).message }
  loading.value = false
}
async function resumeBatch() {
  try { const data = await json(`/${currentBatch.value.batch_id}/resume`, { method: 'POST', body: '{}' }); message.value = data.message; await refreshBatch() } catch (e) { message.value = (e as Error).message }
}
async function retryBatch() {
  try { const data = await json(`/${currentBatch.value.batch_id}/retry`, { method: 'POST', body: '{}' }); message.value = data.message; await refreshBatch() } catch (e) { message.value = (e as Error).message }
}
async function supplement() {
  const body: Record<string, unknown> = { config_id: params.value.correctConfig || undefined, cohort_denominator: Number(params.value.cohortDenominator) || undefined }
  try { const data = await json(`/${currentBatch.value.batch_id}/supplement`, { method: 'POST', body: JSON.stringify(body) }); message.value = data.message; await refreshBatch() } catch (e) { message.value = (e as Error).message }
}
async function confirmBatch() {
  try { const data = await json(`/${currentBatch.value.batch_id}/confirm`, { method: 'POST' }); message.value = data.message; await refreshBatch() } catch (e) { message.value = (e as Error).message }
}

onMounted(reload)
</script>
