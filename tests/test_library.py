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
    # 默认关闭测试进程的模型配置，避免误用开发者本机密钥。
    for name in ('LLM_API_KEY', 'EMBEDDING_API_KEY'):
        monkeypatch.setenv(name, '')
    from knowledge_agent import rag
    monkeypatch.setattr(rag, 'del_index', lambda questions: None)
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
    assert doc['parse_status'] == 'ready'
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


def test_upload_limits_and_search(library, monkeypatch):
    monkeypatch.setattr("knowledge_agent.rag.search_ids", lambda topic, limit: [])
    client, _ = library
    for name, data in [('错.txt', b'x'), ('空.md', b' \n'), ('坏.md', b'\xff')]:
        assert upload(client, name, data).status_code == 422
    assert upload(client, '大.md', b'x'*(16*1024**2+1)).status_code == 413
    assert client.post('/api/documents', content=b'', headers={'content-length': str(48*1024**2+1)}).status_code == 413
    assert client.get('/api/documents').json() == []
    assert client.post('/api/search', json={'topic': '工具调用', 'limit': 3}).json() == {'items': []}
    assert client.post('/api/search', json={'topic': '工具调用', 'limit': 0}).status_code == 422
    assert client.post('/api/search', json={'topic': '   '}).status_code == 422


def test_manual_full_rebuild(library, monkeypatch):
    from unittest.mock import Mock
    from knowledge_agent import rag

    client, _ = library
    url = '/api/index/rebuild'
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


def test_confirm_preserves_draft_order(library):
    client, settings = library
    key = upload(client, '顺序.md', b'# order').json()['id']
    confirm = f'/api/documents/{key}/confirm'
    drafts = [{'question': q} for q in ('乙题', '甲题', '甲题', '丙题')]
    ids = client.post(confirm, json={'questions': drafts}).json()['question_ids']
    with TestClient(create_app(settings)) as reopened:
        def check(rows):
            assert [(q['sort_order'], q['question']) for q in rows] == [(1, '乙题'), (2, '甲题'), (3, '丙题')]
            assert [q['question_id'] for q in rows] == ids
        check(reopened.get('/api/questions').json())
        check(reopened.post(f'/api/documents/{key}/prepare').json()['questions'])
        assert reopened.post(confirm, json={'questions': drafts[::-1]}).json()['question_ids'] == ids
        assert reopened.get('/api/questions?q=丙').json()[0]['sort_order'] == 3
        reopened.delete('/api/questions/' + ids[1]).raise_for_status()
        assert [q['sort_order'] for q in reopened.get('/api/questions').json()] == [1, 3]
        second = upload(reopened, '另一份.md', b'# other').json()['id']
        reopened.post(f'/api/documents/{second}/confirm', json={'questions': drafts[:1]}).raise_for_status()
        assert reopened.get('/api/questions', params={'document_id': second}).json()[0]['sort_order'] == 1
        for number in (1, 2):
            manual = reopened.post('/api/questions', json={'question': f'手动 {number}'}).json()
            assert manual['sort_order'] == number


def test_upgrade_numbers_old_questions_once(library):
    client, settings = library
    key = upload(client, '旧版.md', b'# legacy-order').json()['id']
    url = f'/api/documents/{key}/confirm'
    ids = client.post(url, json={'questions': [{'question': q} for q in ('Z', 'A', 'K')]}).json()['question_ids']
    engine = make_engine(settings)
    with engine.begin() as conn:
        conn.execute(text('ALTER TABLE questions DROP COLUMN sort_order'))
    engine.dispose()
    initialize(settings)
    rows = client.get('/api/questions').json()
    assert [q['question_id'] for q in rows] == sorted(ids)
    assert [q['sort_order'] for q in rows] == [1, 2, 3]
    client.delete('/api/questions/' + rows[0]['question_id']).raise_for_status()
    initialize(settings)
    assert [q['sort_order'] for q in client.get('/api/questions').json()] == [2, 3]

def mock_model_http(monkeypatch, handler):
    """保留完整 SDK 调用链，只替换最后发出 HTTP 请求的传输层。"""
    import httpx
    from openai import OpenAI
    from knowledge_agent import model_api
    def client(**options):
        return OpenAI(**options, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(model_api, 'OpenAI', client)


def test_agent_pipeline_with_chroma(library, monkeypatch):
    """真实走 HTTP 路由、模型请求格式、MySQL 与 Chroma；只替换外部模型的 HTTP 响应。"""
    import json
    from contextlib import contextmanager
    import chromadb
    import httpx
    from knowledge_agent import rag

    client, _ = library
    for prefix in ('LLM', 'EMBEDDING'):
        monkeypatch.setenv(prefix + '_BASE_URL', 'http://model.invalid/v1')
        monkeypatch.setenv(prefix + '_MODEL', 'test-' + uuid4().hex)
        monkeypatch.setenv(prefix + '_API_KEY', 'test')
    # CI 可指定专属 HTTP 服务；本地无独立实例时仍使用真实 Chroma 引擎验证召回。
    test_url = os.getenv('KNOWLEDGE_TEST_CHROMA_URL')
    if test_url:
        monkeypatch.setenv('CHROMA_URL', test_url)
    else:
        store = chromadb.EphemeralClient()
        collection = store.create_collection('test-' + uuid4().hex, embedding_function=None)
        @contextmanager
        def local_collection(create=False):
            yield collection
        monkeypatch.setattr(rag, 'question_collection', local_collection)
    topics = ['排序', '切片', '评估', '超时', '坐标']
    drafts = [{'question': '怎样做' + t, 'tags': [t], 'answer': '答案' if i else '', 'locator': f'第{i+1}题'}
              for i, t in enumerate(topics)]
    calls = []
    def model_response(request):
        url = str(request.url)
        body = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer test"
        calls.append(url)
        if url.endswith('/chat/completions'):
            assert '# 题库' in body['messages'][-1]['content']
            payload = {'choices': [{'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'tool_calls': [{'type': 'function', 'id': 'test-call',
                'function': {'name': 'extract_questions', 'arguments': encoded_drafts}}]}}]}
        else:
            assert body['encoding_format'] == 'float' and 1 <= len(body['input']) <= 10
            data = []
            for i, value in enumerate(body['input']):
                assert isinstance(value, str)
                k = 0 if '换个顺序' in value else next(i for i,t in enumerate(topics) if t in value)
                data.append({'index': i, 'embedding': [float(j == k) for j in range(len(topics))]})
            payload = {'data': data[::-1]}
        return httpx.Response(200, json=payload)
    encoded_drafts = json.dumps({'questions': drafts}, ensure_ascii=False)
    mock_model_http(monkeypatch, model_response)
    key = upload(client, '原文.md', '# 题库'.encode()).json()['id']
    url = f'/api/documents/{key}'
    prepared = client.post(url + '/prepare').json()
    assert prepared['questions'] == drafts
    prepared['questions'][0]['answer'] = '用户确认的答案'
    client.post(url + '/confirm', json={'questions': prepared['questions']}).raise_for_status()
    assert client.post(url + '/prepare').json()['status'] == 'confirmed'
    assert len(calls) == 1
    assert client.post('/api/index/rebuild').json() == {'total': 5, 'indexed': 5}
    query = {'topic': '换个顺序', 'limit': 1}
    def best():
        response = client.post('/api/search', json=query)
        assert response.status_code == 200, response.text
        return response.json()['items'][0]
    q = best()
    assert (q['question'], q['answer'], q['document_id'], q['locator']) == ('怎样做排序', '用户确认的答案', key, '第1题')
    update = {**drafts[0], 'answer': '最新答案'}
    item_url = '/api/questions/' + q['question_id']
    client.put(item_url, json=update).raise_for_status()
    assert best()['answer'] == '最新答案'
    client.put(item_url, json={**update, 'question': '新的排序问题'}).raise_for_status()
    assert client.post('/api/search', json=query).json()['items'] == []
    assert client.post('/api/index/rebuild').json()['indexed'] == 5
    assert best()['question'] == '新的排序问题'
    client.delete(item_url).raise_for_status()
    assert client.post('/api/search', json=query).json()['items'] == []
    assert client.post('/api/index/rebuild').json()['indexed'] == 4
    with rag.question_collection() as collection:
        assert collection.count() == 4
    client.delete(url).raise_for_status()
    assert client.post('/api/index/rebuild').json()['total'] == 0
    with rag.question_collection() as collection:
        assert collection.count() == 0


def test_embedding_propagates_sdk_413_without_retry(monkeypatch):
    import httpx
    from openai import APIStatusError
    from unittest.mock import Mock
    from knowledge_agent.embeddings import embed_texts
    monkeypatch.setenv('EMBEDDING_API_KEY', 'test')
    monkeypatch.setenv('EMBEDDING_MODEL', 'test')
    monkeypatch.setenv('EMBEDDING_BASE_URL', 'http://model.invalid/v1')
    send = Mock(return_value=httpx.Response(413))
    mock_model_http(monkeypatch, send)
    with pytest.raises(APIStatusError) as error:
        embed_texts(['一题'])
    assert error.value.status_code == 413
    send.assert_called_once()


def test_embedding_batches_keep_text_order(monkeypatch):
    import json
    import httpx
    from knowledge_agent import embeddings
    monkeypatch.setenv('EMBEDDING_BASE_URL', 'http://model.invalid/v1')
    monkeypatch.setenv('EMBEDDING_MODEL', 'test')
    monkeypatch.setenv('EMBEDDING_API_KEY', 'test')
    batches = []
    def respond(request):
        texts = json.loads(request.content)['input']
        batches.append(len(texts))
        return httpx.Response(200, json={'data': [{'index': i, 'embedding': [float(texts[i])]} for i in reversed(range(len(texts)))]})
    mock_model_http(monkeypatch, respond)
    got = embeddings.embed_texts([str(i) for i in range(23)])
    assert got == [[float(i)] for i in range(23)]
    assert batches == [10, 10, 3]


def test_search_with_two_arguments_filters_stale_hits_in_rank_order(library, monkeypatch):
    from knowledge_agent import rag

    client, settings = library
    ids = [client.post('/api/questions', json={'question': q}).json()['question_id']
           for q in ['第一题', '第二题', '待建题']]
    engine = make_engine(settings)
    with Session(engine) as session, session.begin():
        for key in ids[:2]:
            session.get(Question, key).index_status = 'indexed'
    engine.dispose()

    def search_ids(topic, limit):
        assert (topic, limit) == ('测试考点', 4)
        return [ids[1], 'deleted-id', ids[2], ids[0]]

    monkeypatch.setattr(rag, 'search_ids', search_ids)
    response = client.post('/api/search', json={'topic': '测试考点', 'limit': 4})
    assert response.status_code == 200
    assert [item['question_id'] for item in response.json()['items']] == [ids[1], ids[0]]


def test_vector_deletion_follows_question_and_document(library, monkeypatch):
    from unittest.mock import Mock
    from knowledge_agent import rag

    client, settings = library
    source = b'# original'
    doc = upload(client, '测试.md', source).json()['id']
    ids = client.post(f'/api/documents/{doc}/confirm', json={'questions': [
        {'question': q} for q in ('第一题', '第二题', '第三题')]}).json()['question_ids']
    other = client.post('/api/questions', json={'question': '独立题目'}).json()['question_id']
    remove = Mock(side_effect=RuntimeError('unavailable'))
    monkeypatch.setattr(rag, 'del_index', remove)

    assert client.delete('/api/questions/'+ids[0]).status_code == 502
    assert len(client.get('/api/questions').json()) == 4
    remove.side_effect = None
    assert client.delete('/api/questions/'+ids[0]).status_code == 204
    remove.assert_called_with([{'question_id': ids[0]}])
    assert client.get(f'/api/documents/{doc}/content').content == source

    remove.side_effect = RuntimeError('unavailable')
    assert client.delete('/api/documents/'+doc).status_code == 502
    assert client.get(f'/api/documents/{doc}/content').content == source
    assert len(client.get('/api/questions').json()) == 3
    remove.side_effect = None
    assert client.delete('/api/documents/'+doc).status_code == 204
    assert {q['question_id'] for q in remove.call_args.args[0]} == set(ids[1:])
    assert [q['question_id'] for q in client.get('/api/questions').json()] == [other]
    assert not list(settings.originals_dir.iterdir())

    empty = upload(client, '空题库.md', b'# no questions').json()['id']
    remove.reset_mock()
    assert client.delete('/api/documents/'+empty).status_code == 204
    remove.assert_not_called()
