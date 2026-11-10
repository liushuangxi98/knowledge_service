"""批量流程回归：真实隔离 MySQL，模型与向量调用使用替身。"""
import json
from unittest.mock import Mock

import httpx
import pytest

from test_library import library, memory_chroma, mock_model_http, upload
from knowledge_agent.rag import del_index
from knowledge_agent import parse_questions, rag


def test_prepare_batch_groups_sources_and_reuses_confirmed(library, monkeypatch):
    client, _ = library
    a = upload(client, 'a.md', b'# A').json()['id']
    b = upload(client, 'b.md', b'# B').json()['id']
    assert upload(client, 'duplicate.md', b'# A').json()['id'] == a
    parser = Mock(return_value=[{'document_id': b, 'questions': []},
                               {'document_id': a, 'questions': [{'question': 'A1'}, {'question': 'A2'}]}])
    monkeypatch.setattr(parse_questions, '_parse_batch', parser)
    result = client.post('/api/documents/prepare-batch', json={'document_ids': [a, b, a]})
    result.raise_for_status()
    assert [d['document_id'] for d in result.json()['documents']] == [a, b]
    parser.assert_called_once_with([{'document_id': a, 'filename': 'a.md', 'source': '# A'},
                                   {'document_id': b, 'filename': 'b.md', 'source': '# B'}])
    assert client.get('/api/questions').json() == []
    for document in result.json()['documents']:
        client.post(f"/api/documents/{document['document_id']}/confirm", json=document).raise_for_status()
    rows = client.get('/api/questions').json()
    assert [(q['question'], q['sort_order'], q['document_id']) for q in rows] == [('A1', 1, a), ('A2', 2, a)]
    parser.reset_mock()
    again = client.post('/api/documents/prepare-batch', json={'document_ids': [a, b]}).json()['documents']
    assert [d['status'] for d in again] == ['confirmed', 'confirmed']
    parser.assert_not_called()


@pytest.mark.parametrize('returned', ['missing', 'unknown', 'duplicate', 'invalid'])
def test_prepare_batch_rejects_invalid_groups(library, monkeypatch, returned):
    client, _ = library
    a = upload(client, 'a.md', b'# A').json()['id']
    b = upload(client, 'b.md', b'# B').json()['id']
    ids = {'missing': [a], 'unknown': [a, 'other'], 'duplicate': [a, a], 'invalid': [a, b]}[returned]
    groups = [{'document_id': key, 'questions': [{'question': ''}] if returned == 'invalid' else []} for key in ids]
    mock_model_http(monkeypatch, lambda request: httpx.Response(200, json={'choices': [{
        'index': 0, 'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'tool_calls': [{
            'id': 'test', 'type': 'function', 'function': {'name': 'BatchQuestionDrafts',
                'arguments': json.dumps({'documents': groups})}}]}}]}))
    response = client.post('/api/documents/prepare-batch', json={'document_ids': [a, b]})
    assert response.status_code == 200
    assert ('未返回拆题结果' if returned == 'invalid' else '文档分组有误') in response.json()['error']
    assert response.json()['documents'] == []
    assert client.get('/api/questions').json() == []
    assert all(not d['questions_llm_confirmed'] for d in client.get('/api/documents').json())


def test_prepare_batch_mixed_confirmation_and_id_validation(library, monkeypatch):
    client, _ = library
    a = upload(client, 'a.md', b'# A').json()['id']
    b = upload(client, 'b.md', b'# B').json()['id']
    body = {'document_id': a, 'questions': [{'question': 'A'}]}
    assert client.post(f'/api/documents/{b}/confirm', json=body).status_code == 422
    assert client.post(f'/api/documents/{a}/confirm', json={'questions': []}).status_code == 422
    client.post(f'/api/documents/{a}/confirm', json=body).raise_for_status()
    parser = Mock(return_value=[{'document_id': b, 'questions': []}])
    monkeypatch.setattr(parse_questions, '_parse_batch', parser)
    result = client.post('/api/documents/prepare-batch', json={'document_ids': [a, b]}).json()
    assert [d['status'] for d in result['documents']] == ['confirmed', 'draft']
    parser.assert_called_once_with([{'document_id': b, 'filename': 'b.md', 'source': '# B'}])
    assert client.post('/api/documents/prepare-batch', json={'document_ids': []}).status_code == 422
    parser.reset_mock()
    assert client.post('/api/documents/prepare-batch', json={'document_ids': [a, 'deleted']}).status_code == 404
    parser.assert_not_called()


def test_batch_delete_rolls_back_then_deletes_only_selected(library, monkeypatch):
    client, _ = library
    doc = upload(client, 'source.md', b'# source').json()['id']
    ids = client.post(f'/api/documents/{doc}/confirm', json={'document_id': doc, 'questions': [
        {'question': name} for name in ['A', 'B', 'C']]}).json()['question_ids']
    remove = Mock(side_effect=RuntimeError('offline'))
    monkeypatch.setattr(rag, 'del_index', remove)
    body = {'question_ids': [ids[0], ids[1], ids[0]]}
    assert client.post('/api/questions/batch-delete', json=body).status_code == 502
    assert len(client.get('/api/questions').json()) == 3
    remove.side_effect = None
    remove.reset_mock()
    assert client.post('/api/questions/batch-delete', json=body).json() == {'deleted': 2}
    assert remove.call_count == 1
    assert {q['question_id'] for q in remove.call_args.args[0]} == set(ids[:2])
    assert [q['question_id'] for q in client.get('/api/questions').json()] == ids[2:]
    assert client.get(f'/api/documents/{doc}/content').content == b'# source'
    remove.reset_mock()
    assert client.post('/api/questions/batch-delete', json={'question_ids': [ids[2], 'deleted']}).status_code == 404
    remove.assert_not_called()
    assert client.post('/api/questions/batch-delete', json={'question_ids': []}).status_code == 422
    client.post('/api/questions/batch-delete', json={'question_ids': ids[2:]}).raise_for_status()
    assert client.get('/api/questions').json() == []


def test_agent_parses_two_documents_in_one_model_call(monkeypatch):
    inputs = [{'document_id': 'a', 'filename': 'a.md', 'source': '# A'},
              {'document_id': 'b', 'filename': 'b.md', 'source': '# B'}]
    outputs = [{'document_id': 'a', 'questions': [{'question': 'A', 'answer': '', 'tags': [], 'locator': ''}]},
               {'document_id': 'b', 'questions': []}]
    def respond(request):
        body = json.loads(request.content)
        assert json.loads(body['messages'][-1]['content']) == {'documents': inputs}
        return httpx.Response(200, json={'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {
            'role': 'assistant', 'tool_calls': [{'id': 'batch', 'type': 'function', 'function': {
                'name': 'BatchQuestionDrafts', 'arguments': json.dumps({'documents': outputs})}}]}}]})
    response = Mock(side_effect=respond)
    mock_model_http(monkeypatch, response)
    assert parse_questions.parse_questions(inputs) == {'documents': outputs, 'error': None, 'status_code': 200}
    assert response.call_count == 1


@pytest.mark.parametrize(('arguments', 'message'), [
    ('{bad json', '未返回拆题结果'),
    (None, '未返回拆题结果'),
    (json.dumps({'documents': [{'document_id': 'a', 'questions': [{'question': ''}]}]}), '未返回拆题结果'),
])
def test_agent_parse_error_is_not_an_empty_success(monkeypatch, arguments, message):
    def respond(request):
        content = {'role': 'assistant', 'content': ''}
        if arguments is not None:
            content['tool_calls'] = [{'id': 'invalid', 'type': 'function', 'function': {
                'name': 'BatchQuestionDrafts', 'arguments': arguments}}]
        return httpx.Response(200, json={'choices': [{'index': 0,
            'finish_reason': 'tool_calls' if arguments is not None else 'stop', 'message': content}]})
    mock_model_http(monkeypatch, respond)
    result = parse_questions.parse_questions([{'document_id': 'a', 'filename': 'a.md', 'source': '# A'}])
    assert result['documents'] == [] and message in result['error']


def test_batch_delete_cleans_actual_vectors(library, monkeypatch, memory_chroma):
    client, _ = library
    ids = [client.post('/api/questions', json={'question': name}).json()['question_id'] for name in ['A', 'B', 'C']]
    memory_chroma.upsert(ids=ids, embeddings=[[1., 0.], [0., 1.], [1., 1.]])
    monkeypatch.setattr(rag, 'del_index', del_index)
    monkeypatch.setattr(rag, 'build_embeddings', lambda: None)
    assert client.post('/api/questions/batch-delete', json={'question_ids': ids[:2]}).json() == {'deleted': 2}
    assert memory_chroma.get()['ids'] == ids[2:]
    assert [q['question_id'] for q in client.get('/api/questions').json()] == ids[2:]


def test_index_loads_updated_script_after_upgrade(tmp_path, monkeypatch):
    import re
    from fastapi.testclient import TestClient
    from backend import app

    frontend = tmp_path / 'frontend'
    frontend.mkdir()
    (frontend / 'index.html').write_bytes((app.ROOT / 'frontend/index.html').read_bytes())
    (frontend / 'styles.css').write_text('')
    script = frontend / 'app.js'
    script.write_text('old')
    monkeypatch.setattr(app, 'ROOT', tmp_path)
    with TestClient(app.create_app()) as client:
        def script_url():
            page = client.get('/')
            assert page.headers['cache-control'] == 'no-cache'
            return re.search(r'src="(/static/app\.js[^"]*)"', page.text)[1]
        old_url = script_url()
        script.write_text('new')
        new_url = script_url()
        assert new_url != old_url
        assert client.get(new_url).text == 'new'


def test_drafts_reused_and_confirmed_as_one_batch(library, monkeypatch):
    client, _ = library
    a, b = [upload(client, name+'.md', name.encode()).json()['id'] for name in ['A', 'B']]
    parser = Mock(side_effect=lambda docs: [{'document_id': d['document_id'], 'questions': [
        {'question': d['source']+'1'}, {'question': d['source']+'2'}]} for d in docs])
    monkeypatch.setattr(parse_questions, '_parse_batch', parser)
    assert parser.call_count == 0
    prepare = '/api/documents/prepare-batch'
    client.post(prepare, json={'document_ids': [a]}).raise_for_status()
    groups = client.post(prepare, json={'document_ids': [a, b]}).json()['documents']
    assert [[d['document_id'] for d in call.args[0]] for call in parser.call_args_list] == [[a], [b]]
    assert client.post(prepare, json={'document_ids': [a, b]}).json()['documents'] == groups
    assert parser.call_count == 2
    url = '/api/documents/confirm-batch'
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    from sqlalchemy.exc import SQLAlchemyError
    from backend.database import Question
    def fail_second(session, *_):
        if any(isinstance(row, Question) and row.document_id == b for row in session.new):
            raise SQLAlchemyError('simulated failure in second document')
    event.listen(Session, 'before_flush', fail_second)
    try:
        assert client.post(url, json={'documents': groups}).status_code == 503
    finally:
        event.remove(Session, 'before_flush', fail_second)
    assert client.get('/api/questions').json() == []
    assert all(d['has_drafts'] and not d['questions_llm_confirmed'] for d in client.get('/api/documents').json())
    ids = client.post(url, json={'documents': groups}).json()['question_ids']
    assert len(ids) == len(set(ids)) == 4
    assert client.post(url, json={'documents': groups}).json()['question_ids'] == ids
    rows = client.get('/api/questions').json()
    assert [q['question'] for q in rows] == ['A1','A2','B1','B2']
    numbers = [q['sort_order'] for q in rows]
    assert numbers == list(range(numbers[0], numbers[0] + 4))
    client.delete('/api/questions/'+ids[-1]).raise_for_status()
    assert client.post('/api/questions', json={'question':'manual'}).json()['sort_order'] > numbers[-1]
    assert all(d['status'] == 'confirmed' for d in client.post(prepare, json={'document_ids':[a,b]}).json()['documents'])
    assert parser.call_count == 2


def test_many_selected_files_send_only_new_sources_and_report_truncation(library, monkeypatch):
    client, _ = library
    ids = [upload(client, f'{i}.md', f'# {i}'.encode()).json()['id'] for i in range(8)]
    client.post(f'/api/documents/{ids[0]}/confirm', json={'document_id': ids[0], 'questions': []}).raise_for_status()
    monkeypatch.setattr(parse_questions.settings, 'llm_model', 'deepseek-flash')
    monkeypatch.setattr(parse_questions.settings, 'llm_base_url', 'https://api.deepseek.com')
    batches = []
    def respond(request):
        body = json.loads(request.content)
        docs = json.loads(body['messages'][-1]['content'])['documents']
        batch = [d['document_id'] for d in docs]
        batches.append(batch)
        assert body['max_tokens'] == parse_questions.settings.llm_max_tokens
        groups = [{'document_id': key, 'questions': []} for key in batch]
        reason = 'length' if len(batches) == 2 else 'tool_calls'
        return httpx.Response(200, json={'choices': [{'index': 0, 'finish_reason': reason, 'message': {
            'role':'assistant', 'tool_calls':[{'id':'batch','type':'function','function': {
                'name':'BatchQuestionDrafts','arguments':json.dumps({'documents':groups})}}]}}]})
    mock_model_http(monkeypatch, respond)
    def prepare(keys):
        return client.post('/api/documents/prepare-batch', json={'document_ids': keys})
    prepare(ids[1:3]).raise_for_status()
    failed = prepare(ids)
    assert failed.status_code == 200 and '截断' in failed.json()['error']
    assert batches == [ids[1:3], ids[3:6]]
    result = prepare(ids).json()['documents']
    assert [d['document_id'] for d in result] == ids
    assert batches[2:] == [ids[3:6], ids[6:]]
    prepare(ids).raise_for_status()
    assert len(batches) == 4


@pytest.mark.parametrize('limit', [393216, 12345])
def test_output_limit_passed_directly_from_env(tmp_path, monkeypatch, limit):
    from knowledge_agent.config import Settings
    env = tmp_path / '.env'
    env.write_text(f'LLM_MAX_TOKENS={limit}\nPARSE_BATCH_SIZE=20', encoding='utf-8')
    monkeypatch.delenv('LLM_MAX_TOKENS', raising=False)
    config = Settings(_env_file=env)
    assert config.llm_max_tokens == limit
    monkeypatch.setattr(parse_questions, 'settings', config)
    def respond(request):
        body = json.loads(request.content)
        assert body['max_tokens'] == limit
        return httpx.Response(200, json={'choices':[{'index':0,'finish_reason':'tool_calls','message': {
            'role':'assistant','tool_calls':[{'id':'test','type':'function','function': {
                'name':'BatchQuestionDrafts','arguments':json.dumps({'documents':[{'document_id':'a','questions':[]}]})
            }}]}}]})
    mock_model_http(monkeypatch, respond)
    assert parse_questions.parse_questions([{'document_id':'a','filename':'a.md','source':'# A'}]) == {
        'documents': [{'document_id':'a','questions':[]}], 'error': None, 'status_code': 200}


def test_later_batch_failure_preserves_previous_batch(library, monkeypatch):
    client, _ = library
    ids = [upload(client, f'{i}.md', f'# {i}'.encode()).json()['id'] for i in range(8)]
    batches = []
    def parse(docs):
        batch = [d['document_id'] for d in docs]
        batches.append(batch)
        if len(batches) == 2:
            raise ValueError('JSON 损坏')
        return [{'document_id': key, 'questions': []} for key in batch]
    monkeypatch.setattr(parse_questions, '_parse_batch', parse)
    def prepare():
        return client.post('/api/documents/prepare-batch', json={'document_ids': ids})
    failure = prepare()
    assert failure.status_code == 200 and '第 2 批' in failure.json()['error']
    assert [d['document_id'] for d in failure.json()['documents']] == ids[:3]
    assert batches == [ids[:3], ids[3:6]]
    assert sum(d['has_drafts'] for d in client.get('/api/documents').json()) == 3
    result = prepare().json()['documents']
    assert [d['document_id'] for d in result] == ids
    assert batches[2:] == [ids[3:6], ids[6:]]
    assert client.get('/api/questions').json() == []


def test_batch_413_stops_without_retry(library, monkeypatch):
    from openai import APIStatusError
    client, _ = library
    ids = [upload(client, f'{i}.md', f'# {i}'.encode()).json()['id'] for i in range(5)]
    handler = Mock(return_value=httpx.Response(413))
    mock_model_http(monkeypatch, handler)
    result = client.post('/api/documents/prepare-batch', json={'document_ids': ids})
    assert result.status_code == 413 and handler.call_count == 1
    assert not any(d['has_drafts'] for d in client.get('/api/documents').json())


@pytest.mark.parametrize('size', [20, 4])
def test_parse_batch_size_loaded_from_env(library, monkeypatch, tmp_path, size):
    from knowledge_agent.config import Settings
    client, _ = library
    env = tmp_path / '.env'
    env.write_text(f'PARSE_BATCH_SIZE={size}\nLLM_MAX_TOKENS=393216', encoding='utf-8')
    monkeypatch.delenv('PARSE_BATCH_SIZE', raising=False)
    config = Settings(_env_file=env)
    assert config.parse_batch_size == size
    ids = [upload(client, f'{i}.md', f'# {i}'.encode()).json()['id'] for i in range(23)]
    batches = []
    def parse(docs):
        batch = [d['document_id'] for d in docs]
        batches.append(batch)
        return [{'document_id': key, 'questions': []} for key in batch]
    monkeypatch.setattr(parse_questions, '_parse_batch', parse)
    monkeypatch.setattr(parse_questions, 'settings', config)
    result = client.post('/api/documents/prepare-batch', json={'document_ids': ids})
    result.raise_for_status()
    assert batches == [ids[i:i+size] for i in range(0,len(ids),size)]
    assert [d['document_id'] for d in result.json()['documents']] == ids


def test_partial_results_can_be_confirmed_together(library, monkeypatch):
    client, _ = library
    ids = [upload(client, f'{i}.md', f'# {i}'.encode()).json()['id'] for i in range(7)]
    calls = []
    def parse(docs):
        calls.append(docs)
        if len(calls) == 2:
            raise ValueError('bad json')
        return [{'document_id': d['document_id'], 'questions': [{'question': d['source']}]} for d in docs]
    monkeypatch.setattr(parse_questions, '_parse_batch', parse)
    result = client.post('/api/documents/prepare-batch', json={'document_ids': ids}).json()
    assert result['error'] and len(result['documents']) == 3 and len(calls) == 2
    assert client.get('/api/questions').json() == []
    client.post('/api/documents/confirm-batch', json={'documents': result['documents']}).raise_for_status()
    assert len(client.get('/api/questions').json()) == 3


def test_413_returns_saved_successes_and_stops(library, monkeypatch):
    client, _ = library
    ids = [upload(client, f'{i}.md', f'# {i}'.encode()).json()['id'] for i in range(7)]
    calls = []
    def parse(docs):
        calls.append(docs)
        if len(calls) == 2:
            from openai import APIStatusError
            response = httpx.Response(413, request=httpx.Request('POST', 'http://model.invalid'))
            raise APIStatusError('too large', response=response, body=None)
        return [{'document_id': d['document_id'], 'questions': []} for d in docs]
    monkeypatch.setattr(parse_questions, '_parse_batch', parse)
    response = client.post('/api/documents/prepare-batch', json={'document_ids': ids})
    assert response.status_code == 413 and len(calls) == 2
    assert len(response.json()['documents']) == 3
    assert sum(d['has_drafts'] for d in client.get('/api/documents').json()) == 3
