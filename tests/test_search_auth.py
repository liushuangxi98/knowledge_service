"""检索鉴权通过实际 HTTP 路由验证，查询与数据库连接使用替身。"""
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from backend import app as app_module
from backend.config import Settings


# 内容仅差一位的密钥也应被拒绝，同时覆盖缺失请求头与服务端未配置。
@pytest.mark.parametrize('expected,provided,status', [
    ('test-secret-A123', 'test-secret-A123', 200),
    ('test-secret-A123', 'test-secret-A124', 401),
    ('test-secret-A123', 'other-format-key', 401),
    ('test-secret-A123', None, 401),
    ('test-secret-A123', '', 401),
    # 服务端未配置时，即使客户端携带非空密钥也必须关闭检索。
    ('', 'test-secret-A123', 503),
    # 双方都为空同样不能因为字符串相等而放行。
    ('', None, 503),
])
def test_search_auth(tmp_path, monkeypatch, expected, provided, status):
    # 显式配置测试值，不读取本机 .env 中的数据库与密钥。
    settings = Settings(_env_file=None, knowledge_api_key=expected, originals_dir=tmp_path)
    # 内存 SQLite 仅用于构造应用，鉴权测试没有任何 SQL 操作。
    monkeypatch.setattr(app_module, 'make_engine', lambda settings: create_engine('sqlite://'))
    # 检索替身同时代表其后的嵌入、召回与题目读取，拒绝请求时不得调用。
    search = Mock(return_value={'items': [{'question_id': 'q1'}]})
    monkeypatch.setattr(app_module.Library, 'search', search)
    # None 表示请求完全没有这个头，空字符串则表示携带了空头。
    headers = {} if provided is None else {'X-API-Key': provided}
    # 通过完整路由测试请求头提取和密钥比较，不直接调用鉴权函数。
    with TestClient(app_module.create_app(settings)) as client:
        result = client.post('/api/search', headers=headers, json={'topic': 'RAG', 'limit': 4})
    # 核对可供工作台区分配置缺失和凭据错误的 HTTP 状态码。
    assert result.status_code == status
    # 正确密钥必须确实进入业务检索，保证鉴权接入后仍能完成正常调用。
    if status == 200:
        search.assert_called_once_with('RAG', 4, None)
        assert result.json() == {'items': [{'question_id': 'q1'}]}
    else:
        # 拒绝响应只提供公开原因，不返回配置的密钥内容。
        search.assert_not_called()
        assert 'test-secret' not in result.text
