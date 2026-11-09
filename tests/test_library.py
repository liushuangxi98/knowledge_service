"""集成测试须在明确指定的隔离 MySQL 实例运行，每个用例创建自己的临时数据库。"""
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app import create_app
from backend.config import Settings
from backend.database import Question, initialize, make_engine


@pytest.fixture
def library(tmp_path, monkeypatch):
    # 接口测试使用替身，不发真实 LLM、Embedding 或 Chroma 请求。
    from knowledge_agent import parse_questions, rag
    monkeypatch.delattr(parse_questions, 'parse_questions', raising=False)
    monkeypatch.delattr(rag, 'rebuild_index', raising=False)
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
    assert saved['question_id'] in ids and saved['index_status'] == 'pending'

    # 模拟将来索引器的成功写回，以验证修改参考答案和修改题干的差异。
    engine = make_engine(settings)
    with Session(engine) as session, session.begin():
        session.get(Question, saved['question_id']).index_status = 'indexed'
    engine.dispose()
    edit = {**drafts['questions'][0], 'answer': '已核对的答案'}
    assert client.put('/api/questions/'+saved['question_id'], json=edit).json()['index_status'] == 'indexed'
    changed = client.put('/api/questions/'+saved['question_id'], json={**edit, 'question': '如何做检索？'}).json()
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


def test_manual_full_rebuild(library, monkeypatch):
    from unittest.mock import Mock
    from knowledge_agent import rag

    client, _ = library
    url = '/api/index/rebuild'
    assert client.post(url).status_code == 501
    rebuild = Mock()
    monkeypatch.setattr(rag, 'rebuild_index', rebuild, raising=False)
    assert client.post(url).json() == {'total': 0, 'indexed': 0}
    rebuild.assert_called_once_with([])
    rebuild.reset_mock()

    doc = upload(client, '检索.md', b'# search').json()
    client.post(f"/api/documents/{doc['id']}/confirm", json={'questions': [{'question': '召回'}]}).raise_for_status()
    manual = client.post('/api/questions', json={'question': '重排'}).json()
    rebuild.assert_not_called()
    assert client.post(url).json() == {'total': 2, 'indexed': 2}
    manual_url = '/api/questions/' + manual['question_id']
    client.put(manual_url, json={'question': '新的重排题'}).raise_for_status()
    assert rebuild.call_count == 1
    def edit_during_rebuild(items):
        assert {q['question'] for q in items} == {'召回', '新的重排题'}
        assert {q['index_status'] for q in items} == {'indexed', 'pending'}
        assert client.post(url).status_code == 409
        client.put(manual_url, json={'question': '再次编辑'}).raise_for_status()
    rebuild.side_effect = edit_during_rebuild
    assert client.post(url).json() == {'total': 2, 'indexed': 1}
    rebuild.side_effect = RuntimeError('test')
    assert client.post(url).status_code == 502
    state = {q['question']: q['index_status'] for q in client.get('/api/questions').json()}
    assert state == {'召回': 'indexed', '再次编辑': 'pending'}
    rebuild.side_effect = None
    client.delete(manual_url).raise_for_status()
    assert client.post(url).json() == {'total': 1, 'indexed': 1}
    assert [q['question'] for q in rebuild.call_args.args[0]] == ['召回']


def test_prepare_reuses_confirmed_questions_after_restart(library, monkeypatch):
    from unittest.mock import Mock
    from knowledge_agent import parse_questions

    client, settings = library
    key = upload(client, '面试.md', '# 面试'.encode()).json()['id']
    url = f'/api/documents/{key}'
    drafts = [{'question': f'题目 {i}'} for i in range(5)]
    parser = Mock(return_value=drafts)
    monkeypatch.setattr(parse_questions, 'parse_questions', parser, raising=False)
    prepared = client.post(url + '/prepare').json()
    assert prepared['status'] == 'draft' and len(prepared['questions']) == 5
    parser.assert_called_once_with(key, '# 面试')
    assert client.get('/api/documents').json()[0]['questions_llm_confirmed'] is False
    ids = client.post(url + '/confirm', json={'questions': drafts}).json()['question_ids']
    with TestClient(create_app(settings)) as reopened:
        assert reopened.get('/api/documents').json()[0]['questions_llm_confirmed'] is True
        assert upload(reopened, '同一份.md', '# 面试'.encode()).json()['questions_llm_confirmed'] is True
        data = reopened.post(url + '/prepare').json()
        assert data['status'] == 'confirmed'
        assert {q['question_id'] for q in data['questions']} == set(ids)
        assert all(q['index_status'] == 'pending' for q in data['questions'])
        stale = reopened.post(url + '/confirm', json={'questions': [{'question': '另一版措辞'}]})
        assert set(stale.json()['question_ids']) == set(ids)
        for qid in ids:
            assert reopened.delete('/api/questions/' + qid).status_code == 204
        assert reopened.post(url + '/prepare').json() == {'status': 'confirmed', 'questions': []}
    parser.assert_called_once()


def test_upgrade_backfills_old_confirmations(library):
    client, settings = library
    key = upload(client, '旧文档.md', b'# old').json()['id']
    new = upload(client, '未整理.md', b'# new').json()['id']
    client.post(f'/api/documents/{key}/confirm', json={'questions': [{'question': '原题'}]}).raise_for_status()
    engine = make_engine(settings)
    with engine.begin() as conn:
        conn.execute(text('ALTER TABLE documents DROP COLUMN questions_llm_confirmed'))
    engine.dispose()
    initialize(settings)
    initialize(settings)
    flags = {doc['id']: doc['questions_llm_confirmed'] for doc in client.get('/api/documents').json()}
    assert flags == {key: True, new: False}
    assert client.post(f'/api/documents/{new}/prepare').status_code == 501


def test_failed_confirm_rolls_back_flag_and_questions(library, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError
    client, _ = library
    key = upload(client, '题库.md', b'# test').json()['id']
    def fail_commit(*args):
        raise SQLAlchemyError('test')
    with monkeypatch.context() as failure:
        failure.setattr('sqlalchemy.orm.session.SessionTransaction.commit', fail_commit)
        result = client.post(f'/api/documents/{key}/confirm', json={'questions': [{'question':'测试题'}]})
    assert result.status_code == 503
    assert client.get('/api/documents').json()[0]['questions_llm_confirmed'] is False
    assert client.get('/api/questions').json() == []
