import { describe, expect, it } from 'vitest'
import { generationFailureFeedback } from '../src/generationFailure'
import type { GenerationJob } from '../src/api/generations'

const failed = (code: string, message = 'raw-secret-from-upstream'): Pick<GenerationJob, 'status' | 'error'> => ({
  status: 'failed', error: { code, message, retryable: true },
})

describe('safe generation failure feedback', () => {
  it.each([
    ['provider_capacity_exhausted', '上游算力不足'],
    ['rate_limited', '云端请求限流'],
    ['provider_balance_insufficient', '云端账户余额不足或欠费'],
    ['provider_quota_exceeded', '云端额度或预算已用尽'],
    ['provider_authentication_failed', '云端密钥验证失败'],
    ['provider_access_denied', '未获得模型调用权限'],
    ['content_rejected', '内容审核未通过'],
    ['invalid_generation_inputs', '生成参数或参考图不受支持'],
    ['provider_network_error', '云端网络连接失败'],
    ['provider_timeout', '云端响应超时'],
    ['generation_timeout', '生成等待超时'],
    ['output_contract_mismatch', '生成结果规格不符'],
  ])('describes %s specifically without exposing the upstream response', (code, reason) => {
    const feedback = generationFailureFeedback(failed(code))!
    expect(feedback.title).toBe(`生成失败：${reason}`)
    expect(feedback.tone).toBe('error')
    expect(JSON.stringify(feedback)).not.toContain('raw-secret')
  })

  it('does not infer capacity shortage from an unknown error or render raw code/message', () => {
    for (const code of ['constructor', '__proto__', 'https://private.example?token=secret', 'arbitrary-error']) {
      const result = generationFailureFeedback(failed(code))!
      expect(result.code).toBe('provider_failed')
      expect(result.title).toBe('生成失败：云端未能完成生成')
      expect(JSON.stringify(result)).not.toContain(code)
      expect(JSON.stringify(result)).not.toContain('上游算力不足')
    }
  })

  it('keeps cancellation/interruption distinct and does not call other states failures', () => {
    expect(generationFailureFeedback({ status: 'cancelled', error: null })).toMatchObject({ title: '生成已取消', tone: 'neutral' })
    expect(generationFailureFeedback({ status: 'interrupted', error: null })).toMatchObject({ title: '生成已中断', tone: 'warning' })
    for (const status of ['blocked', 'queued', 'submitting', 'running', 'finalizing', 'succeeded'] as const) {
      expect(generationFailureFeedback({ status, error: null })).toBeNull()
    }
  })

  it('asks users to check unknown remote status before trying another submission', () => {
    const result = generationFailureFeedback(failed('remote_status_unknown'))!
    expect(result.hint).toContain('先检查原云端任务')
    expect(result.hint).toContain('避免重复提交')
  })
})
