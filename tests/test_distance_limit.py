"""离线验证 HTTP 入参经业务层传到向量距离过滤，全程不连接真实题库。"""
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from backend import app as app_module
from backend.config import Settings
from knowledge_agent import rag


@pytest.fixture
def search_app(tmp_path, monkeypatch):
    # 只替换向量访问和数据库会话，HTTP 路由、Library.search、search_ids 都执行真实代码。
    vector = Mock()
    vector.similarity_search_with_score.return_value = [
        (SimpleNamespace(id='close'), 0.1),
        (SimpleNamespace(id='boundary'), 0.4),
        (SimpleNamespace(id='far'), 0.8),
    ]
    monkeypatch.setattr(rag, 'Chroma', lambda **kwargs: vector)
    monkeypatch.setattr(rag, 'build_embeddings', lambda: None)
    monkeypatch.setattr(rag, 'Settings', lambda: SimpleNamespace(
        chroma_dir=tmp_path, get_chroma_database_version=lambda: 'test'))
    sessions = MagicMock()
    sessions.return_value.__enter__.return_value.scalars.return_value = [
        SimpleNamespace(question_id=key, question=key, answer='', tags=[], document_id=None,
                        locator='', index_status='indexed', sort_order=i)
        for i, key in enumerate(['close', 'boundary', 'far'], 1)
    ]
    monkeypatch.setattr(app_module, 'make_engine', lambda settings: create_engine('sqlite://'))
    monkeypatch.setattr(app_module, 'sessionmaker', lambda *args, **kwargs: sessions)
    settings = Settings(_env_file=None, knowledge_api_key='offline', originals_dir=tmp_path)
    with TestClient(app_module.create_app(settings), headers={'X-API-Key': 'offline'}) as client:
        yield client, vector


@pytest.mark.parametrize(('distance_limit', 'ids'), [
    (None, ['close', 'boundary', 'far']),
    (0, []),
    (0.4, ['close', 'boundary']),
    (0.8, ['close', 'boundary', 'far']),
])
def test_distance_filter_through_http(search_app, distance_limit, ids):
    client, vector = search_app
    body = {'topic': '测试考点', 'limit': 4}
    if distance_limit is not None:
        body['distance_limit'] = distance_limit
    response = client.post('/api/search', json=body)
    assert response.status_code == 200, response.text
    assert [row['question_id'] for row in response.json()['items']] == ids
    distances = {'close': 0.1, 'boundary': 0.4, 'far': 0.8}
    assert [row['distance'] for row in response.json()['items']] == [distances[key] for key in ids]
    vector.similarity_search_with_score.assert_called_once_with('测试考点', k=4)


@pytest.mark.parametrize('distance_limit', [-0.1, 2.1, 'bad', 'NaN', 'Infinity'])
def test_invalid_distance_stops_before_search(search_app, distance_limit):
    client, vector = search_app
    response = client.post('/api/search', json={'topic': '测试考点', 'distance_limit': distance_limit})
    assert response.status_code == 422
    vector.similarity_search_with_score.assert_not_called()
