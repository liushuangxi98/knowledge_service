"""集成测试须在明确指定的隔离 MySQL 实例运行，每个用例创建自己的临时数据库。"""
import ast
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app import create_app
from backend.config import ROOT, Settings
from backend.database import Question, initialize, make_engine


@pytest.fixture
def library(tmp_path):
    if not os.environ.get('KNOWLEDGE_TEST_MYSQL_PORT'):
        pytest.skip('请指定隔离实例的 KNOWLEDGE_TEST_MYSQL_PORT 和 KNOWLEDGE_TEST_MYSQL_PASSWORD')
    database = 'knowledge_test_' + uuid4().hex
    settings = Settings(_env_file=None, mysql_host='127.0.0.1', mysql_user='root', mysql_database=database,
                        mysql_port=int(os.environ['KNOWLEDGE_TEST_MYSQL_PORT']),
                        mysql_password=os.environ['KNOWLEDGE_TEST_MYSQL_PASSWORD'], originals_dir=tmp_path)
    initialize(settings)
    try:
        with TestClient(create_app(settings)) as client:
            yield client, settings
    finally:
        admin = make_engine(settings, admin=True)
        try:
            with admin.begin() as connection:
                connection.execute(text(f'DROP DATABASE `{database}`'))
        finally:
            admin.dispose()


def upload(client, name, data):
    return client.post('/api/documents', files={'file': (name, data, 'text/markdown')})


def test_document_confirm_and_index_state(library):
    client, settings = library
    original = '# 第一章\n什么是检索？'.encode()
    doc = upload(client, '题库.md', original).json()
    key = doc['id']
    assert doc['parse_status'] == 'not_connected'
    assert upload(client, '另一文件名.md', original).json()['id'] == key
    assert len(client.get('/api/documents').json()) == 1
    assert client.get(f'/api/documents/{key}/content').content == original
    assert client.get('/api/questions').json() == []
    drafts = {'questions': [{'question': '什么是检索？', 'locator': '第一章', 'tags': ['RAG']},
                            {'question': '什么是重排？', 'answer': '调整顺序'}]}
    url = f'/api/documents/{key}/confirm'
    assert client.post(url, json={'questions': [drafts['questions'][0], {'question': '  '}]}).status_code == 422
    assert not client.get('/api/questions').json()
    ids = client.post(url, json=drafts).json()['question_ids']
    assert len(set(ids)) == 2
    saved, = client.get('/api/questions', params={'q': '什么是检索', 'document_id': key}).json()
    assert saved['document_id'] == key and saved['answer'] == '' and saved['locator'] == '第一章'
    assert saved['question_id'] == ids[0] and saved['index_status'] == 'pending'

    # 模拟将来索引器的成功写回，以验证修改参考答案和修改题干的差异。
    engine = make_engine(settings)
    with Session(engine) as session, session.begin():
        session.get(Question, ids[0]).index_status = 'indexed'
    engine.dispose()
    edit = {**drafts['questions'][0], 'answer': '已核对的答案'}
    assert client.put('/api/questions/'+ids[0], json=edit).json()['index_status'] == 'indexed'
    changed = client.put('/api/questions/'+ids[0], json={**edit, 'question': '如何做检索？'}).json()
    assert changed['index_status'] == 'pending'
    assert client.post(url, json=drafts).json()['question_ids'] == ids
    assert client.get('/api/questions', params={'q': '如何做检索'}).json() == [changed]
    manual = client.post('/api/questions', json={'question': '独立录入'}).json()
    assert manual['document_id'] is None and manual['question_id'] not in ids
    assert client.delete(f'/api/documents/{key}').status_code == 204
    assert client.get(f'/api/documents/{key}/content').status_code == 404
    assert client.get('/api/questions').json() == [manual]
    assert client.delete('/api/questions/'+manual['question_id']).status_code == 204
    assert not list(settings.originals_dir.iterdir())


def test_upload_limits_and_search(library):
    client, _ = library
    for name, data in [('错.txt', b'x'), ('空.md', b' \n'), ('坏.md', b'\xff')]:
        assert upload(client, name, data).status_code == 422
    assert upload(client, '大.md', b'x'*(16*1024**2+1)).status_code == 413
    assert client.post('/api/documents', content=b'', headers={'content-length': str(48*1024**2+1)}).status_code == 413
    assert client.get('/api/documents').json() == []
    assert client.post('/api/search', json={'topic': '工具调用', 'limit': 3}).status_code == 501
    assert client.post('/api/search', json={'topic': '工具调用', 'limit': 0}).status_code == 422


def test_agent_modules_contain_only_their_docstrings():
    for path in (ROOT / 'knowledge_agent').glob('*.py'):
        module = ast.parse(path.read_text(encoding='utf-8'))
        assert len(module.body) == 1 and ast.get_docstring(module)
