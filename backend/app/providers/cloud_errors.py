"""Safe, typed cloud transport failures; never store proxy URLs or credentials."""


class CloudTransportError(RuntimeError):
    def __init__(self, category: str, exception_type: str):
        self.category = category
        self.exception_type = exception_type
        self.retryable = True
        explanation = {
            "connect_timeout": "建立连接超时，尚未收到模型响应",
            "read_timeout": "等待响应超时，本次用量暂不能确认",
            "write_timeout": "上传请求超时，本次用量暂不能确认",
            "pool_timeout": "等待连接池超时",
            "network_connection": "网络连接中断，尚未取得有效响应",
        }.get(category, "网络请求失败，尚未取得有效响应")
        super().__init__(f"百炼网络请求失败：{exception_type}（{explanation}）")
