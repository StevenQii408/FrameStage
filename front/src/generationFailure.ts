import type { GenerationJob } from './api/generations'

export interface GenerationFailureFeedback {
  title: string
  reason: string
  hint: string
  tone: 'error' | 'warning' | 'neutral'
  code: string
}

const failures: Record<string, { reason: string; hint: string }> = {
  provider_capacity_exhausted: { reason: '上游算力不足', hint: '请稍后在节点菜单中手动重试。' },
  rate_limited: { reason: '云端请求限流', hint: '请等待一会儿，再在节点菜单中手动重试。' },
  provider_balance_insufficient: { reason: '云端账户余额不足或欠费', hint: '请在云端控制台补足余额，再创建新任务。' },
  provider_quota_exceeded: { reason: '云端额度或预算已用尽', hint: '请检查云端配额或预算，再创建新任务。' },
  provider_authentication_failed: { reason: '云端密钥验证失败', hint: '请检查后端 API Key 及所属区域，再创建新任务。' },
  provider_access_denied: { reason: '未获得模型调用权限', hint: '请检查模型开通状态和工作区权限，再创建新任务。' },
  content_rejected: { reason: '内容审核未通过', hint: '请修改 Prompt 或参考图片，再提交新任务。' },
  invalid_generation_inputs: { reason: '生成参数或参考图不受支持', hint: '请检查模型规格与参考图片，再提交新任务。' },
  provider_network_error: { reason: '云端网络连接失败', hint: '请检查网络及原云端任务状态，再手动重试。' },
  provider_timeout: { reason: '云端响应超时', hint: '原任务状态可能未知，请先核查，再手动重试。' },
  generation_timeout: { reason: '生成等待超时', hint: '原任务状态可能未知，请先核查，再手动重试。' },
  provider_unavailable: { reason: '云端生成服务暂不可用', hint: '请稍后核查原任务状态，再手动重试。' },
  remote_status_unknown: { reason: '云端任务状态待核对', hint: '请先检查原云端任务，避免重复提交。' },
  provider_response_invalid: { reason: '云端返回格式异常', hint: '请先核查原云端任务，未自动重新提交。' },
  output_contract_mismatch: { reason: '生成结果规格不符', hint: '画幅、分辨率或时长未通过校验，未交付新结果。' },
  invalid_image: { reason: '生成图片无法读取', hint: '图片未通过媒体校验，未交付新结果。' },
  invalid_video_output: { reason: '生成视频无法读取', hint: '视频未通过媒体校验，未交付新结果。' },
  asset_size_limit: { reason: '生成文件超过容量限制', hint: '请降低生成规格，再提交新任务。' },
  unsafe_asset_url: { reason: '生成结果地址未通过安全检查', hint: '结果未下载或交付，请检查原云端任务。' },
  invalid_provider_progress: { reason: '云端返回无效进度', hint: '任务已停止，请先核查原云端任务。' },
  provider_failed: { reason: '云端未能完成生成', hint: '请核查原任务状态，再决定是否重试。' },
  generation_provider_error: { reason: '生成服务发生异常', hint: '原任务状态可能未知，请先核查，未自动重试。' },
}

export function cloudCancellationLabel(task?: Pick<GenerationJob, 'status' | 'error'> | null): string {
  return task?.status === 'cancelled' && task.error?.code === 'provider_cancelled' ? '云端已取消' : ''
}

/** Never render raw upstream messages: they may contain credentials or URLs. */
export function generationFailureFeedback(task: Pick<GenerationJob, 'status' | 'error'>): GenerationFailureFeedback | null {
  if (task.status === 'cancelled') {
    const cloudLabel = cloudCancellationLabel(task)
    return { title: cloudLabel || '生成已取消', reason: '', hint: '未自动重新提交。', tone: 'neutral', code: cloudLabel ? 'provider_cancelled' : 'cancelled' }
  }
  if (task.status === 'interrupted') return { title: '生成已中断', reason: '', hint: '请先核查原云端任务，再决定是否重试。', tone: 'warning', code: 'interrupted' }
  if (task.status !== 'failed') return null
  const rawCode = task.error?.code || ''
  const code = Object.hasOwn(failures, rawCode) ? rawCode : 'provider_failed'
  const { reason, hint } = failures[code]!
  return { title: `生成失败：${reason}`, reason, hint, tone: 'error', code }
}
