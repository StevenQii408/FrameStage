"""Safe, stable public failures shared by cloud transport and job execution.

Upstream codes are classified separately; upstream messages, URLs, credentials
and local exception details never enter these user-facing descriptions.
"""

PUBLIC_FAILURES = {
    "provider_capacity_exhausted": "上游算力不足，请稍后显式重试。",
    "provider_balance_insufficient": "云端账户余额不足或欠费，请检查百炼账户账单。",
    "provider_quota_exceeded": "云端额度或预算已用尽，请检查免费额度与预算限制。",
    "rate_limited": "生成服务请求频率或并发受限，请稍后显式重试。",
    "content_rejected": "生成服务拒绝了当前内容，请修改输入后提交新任务。",
    "provider_timeout": "生成服务响应超时；状态与费用可能未知，请检查后显式重试。",
    "provider_network_error": "云端网络连接失败；状态与费用可能未知，请检查网络后核查原任务。",
    "provider_unavailable": "生成服务暂不可用，请稍后显式重试。",
    "provider_failed": "生成服务未能完成任务，未交付生成结果。",
    "output_contract_mismatch": "生成产物的画幅、分辨率或时长与任务合同不一致，未交付结果。",
    "invalid_image": "生成图片未通过本地媒体校验，未交付结果。",
    "invalid_video_output": "生成视频未通过本地媒体校验，未交付结果。",
    "asset_size_limit": "生成产物超过本机资产容量限制，未交付结果。",
    "invalid_provider_progress": "生成服务返回无效进度，任务已停止。",
    "generation_provider_error": "生成服务发生异常，任务已停止；状态与费用可能未知，请检查后显式重试。",
    "provider_response_invalid": "生成服务返回的协议无效；未自动重新提交，请核查原任务。",
    "unsafe_asset_url": "生成结果地址未通过安全检查，未下载或交付结果。",
    "provider_unconfigured": "该生成接口尚未启用或密钥未配置。",
    "provider_authentication_failed": "百炼生成 API Key 验证失败，请检查后端密钥及所属区域。",
    "provider_access_denied": "百炼拒绝该生成模型的访问，请检查工作区、区域及模型开通权限。",
    "remote_status_unknown": "云端任务状态未知，仍保留并发名额并继续核对；请勿直接重复提交。",
    "invalid_generation_inputs": "生成输入未通过模型校验，请修改后提交新任务。",
}

# These require a configuration/content change or investigation before a new
# request; a provider's generic retryable flag must not override the policy.
NON_RETRYABLE_FAILURES = frozenset({
    "provider_balance_insufficient", "provider_quota_exceeded", "provider_authentication_failed",
    "provider_access_denied", "content_rejected", "invalid_generation_inputs", "provider_unconfigured",
    "remote_status_unknown", "provider_response_invalid", "unsafe_asset_url", "invalid_provider_progress",
})

TRANSIENT_QUERY_FAILURES = frozenset({
    "rate_limited", "provider_timeout", "provider_network_error", "provider_unavailable", "provider_capacity_exhausted",
})

# Only explicit rejections prove a POST was not accepted. Network failures and
# generic 5xx responses remain ambiguous and retain their remote capacity slot.
SUBMISSION_REJECTION_FAILURES = frozenset({
    "provider_capacity_exhausted", "provider_balance_insufficient", "provider_quota_exceeded", "rate_limited",
    "content_rejected", "invalid_generation_inputs", "provider_authentication_failed",
    "provider_access_denied", "provider_unconfigured",
})


def public_failure(code, *, fallback="generation_provider_error", retryable=True):
    code = code if isinstance(code, str) and code in PUBLIC_FAILURES else fallback
    return {"code": code, "message": PUBLIC_FAILURES[code],
            "retryable": bool(retryable) and code not in NON_RETRYABLE_FAILURES}
