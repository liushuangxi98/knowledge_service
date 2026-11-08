"""按实际接收字节限制请求体大小。"""
from fastapi.responses import JSONResponse


class BodyLimitMiddleware:
    """按 ASGI（服务器与异步应用收发消息的约定）在后续应用解析请求体前检查大小。"""
    def __init__(self, app):
        """保存后续 ASGI 应用 app，供请求体检查通过后调用；无返回值。"""
        self.app = app

    async def __call__(self, scope, receive, send):
        """检查请求体并发送响应，无业务返回值；scope 保存请求信息，receive/send 异步收发消息。
        超限直接响应 413 且不调用后续应用；接收时断连则结束处理。
        """
        if scope['type'] != 'http' or scope['method'] not in {'POST', 'PUT', 'PATCH'}:
            return await self.app(scope, receive, send)
        maximum = 48 * 1024 * 1024
        chunks, size = [], 0
        # 长度头已经超限时可提前拒绝，不必接收完整请求体。
        header = dict(scope['headers']).get(b'content-length', b'0')
        try:
            too_large = int(header) > maximum
        except ValueError:
            too_large = False
        if not too_large:
            # 不只信 Content-Length；流式上传也累计实际收到的字节。
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                body = message.get('body', b'')
                # 表单分隔符和普通字段也计入请求体，因此这里的大小可能大于上传文件本身。
                size += len(body)
                if size > maximum:
                    too_large = True
                    break
                chunks.append(body)
                # more_body 为 False 才表示请求体收齐，不能仅凭当前块为空来判断结束。
                if not message.get('more_body', False):
                    break
        if too_large:
            # 此时尚未进入路由，直接发送与应用异常相同结构的错误响应。
            response = JSONResponse({'error': {
                'code': 'REQUEST_TOO_LARGE', 'message': '请求超过 48 MiB；本次操作失败，未自动重试',
            }}, status_code=413)
            return await response(scope, receive, send)
        # 接收通道已被检查过程消耗，需回放缓存正文，后续 JSON 或表单解析才能读到它。
        body, replayed = b''.join(chunks), False

        async def replay():
            """首次返回完整请求体，之后沿用原接收通道，让后续应用仍能接收到断连消息。"""
            nonlocal replayed
            if not replayed:
                replayed = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            return await receive()

        await self.app(scope, replay, send)
