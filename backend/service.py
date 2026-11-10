"""文档原件、已确认题目和索引状态的存储。拆题和检索由 knowledge_agent 接入。"""
from hashlib import sha256
from pathlib import Path
from threading import Lock
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.dialects.mysql import insert

from knowledge_agent import parse_questions, rag
from .database import Document, DocumentDraft, Question, QuestionCounter


def show_document(doc, has_drafts=False):
    return {'id': doc.id, 'filename': doc.filename, 'created_at': doc.created_at,
            'questions_llm_confirmed': doc.questions_llm_confirmed, 'has_drafts': has_drafts,
            'parse_status': 'ready'}


def show_question(item):
    return {key: getattr(item, key) for key in
            ('question_id', 'question', 'answer', 'tags', 'document_id', 'locator', 'index_status', 'sort_order')}


def get_record(session, model, key, *, lock=False):
    row = session.get(model, key, with_for_update=lock)
    if row is None:
        raise HTTPException(404, '记录不存在或已删除')
    return row


class Library:
    def __init__(self, sessions, originals):
        self.sessions = sessions
        self.originals = Path(originals)
        self.index_lock = Lock()

    def documents(self):
        with self.sessions() as session:
            rows = session.execute(select(Document, DocumentDraft.document_id).outerjoin(DocumentDraft)
                                   .order_by(Document.created_at.desc(), Document.id))
            return [show_document(doc, draft_id is not None) for doc, draft_id in rows]

    def upload(self, filename, content):
        digest = sha256(content).hexdigest()
        new_id = uuid4().hex
        new_file = None
        try:
            with self.sessions.begin() as session:
                # 同内容的并发上传由唯一键归到同一文档，原件使用服务端生成的文件名。
                stmt = insert(Document).values(id=new_id, filename=filename, sha256=digest)
                session.execute(stmt.on_duplicate_key_update(sha256=digest))
                doc = session.scalar(select(Document).where(Document.sha256 == digest).with_for_update())
                if doc.id == new_id:
                    self.originals.mkdir(parents=True, exist_ok=True)
                    path = self.originals / (doc.id + '.md')
                    with path.open('xb') as stream:
                        new_file = path
                        stream.write(content)
                has_drafts = session.scalar(select(DocumentDraft.document_id).where(DocumentDraft.document_id == doc.id))
                return {**show_document(doc, has_drafts is not None), 'reused': doc.id != new_id}
        except Exception:
            # 数据库写入失败时，移除本次创建的原件。
            if new_file:
                new_file.unlink(missing_ok=True)
            raise

    def original(self, key):
        with self.sessions.begin() as session:
            doc = get_record(session, Document, key, lock=True)
            return self._read_original(doc.id)

    def _read_original(self, key):
        path = self.originals / (key + '.md')
        if not path.is_file():
            raise HTTPException(404, '原件文件不存在')
        return path.read_bytes()

    @staticmethod
    def delete_vectors(question_ids):
        """把正式题目 ID 交给 Agent；尚未拆出题目的文档无需访问向量库。"""
        if not question_ids:
            return
        try:
            rag.del_index([{'question_id': key} for key in question_ids])
        except HTTPException:
            raise
        except Exception as exc:
            status = getattr(exc, 'status_code', None) or getattr(getattr(exc, 'response', None), 'status_code', None)
            if status == 413:
                raise HTTPException(413, '向量删除请求超过大小限制，本次操作已停止') from None
            raise HTTPException(502, '向量删除失败，本次数据库删除已撤销') from None

    def remove_document(self, key):
        # 与重建共用锁，避免重建从旧快照把刚删除的向量写回。
        with self.index_lock:
            moved = False
            try:
                with self.sessions.begin() as session:
                    doc = get_record(session, Document, key, lock=True)
                    ids = session.scalars(select(Question.question_id).where(Question.document_id == key)).all()
                    path = self.originals / (doc.id + '.md')
                    backup = path.with_suffix('.deleting')
                    if path.exists():
                        path.rename(backup)
                        moved = True
                    session.delete(doc)  # 数据库外键同时删除所属题目。
                    session.flush()
                    self.delete_vectors(ids)
            except Exception:
                if moved:
                    backup.rename(path)
                raise
            if moved:
                backup.unlink()

    def questions(self, query, document_id):
        stmt = select(Question).order_by(Question.sort_order)
        if document_id:
            stmt = stmt.where(Question.document_id == document_id)
        if query:
            stmt = stmt.where(Question.question.contains(query, autoescape=True))
        with self.sessions() as session:
            return [show_question(item) for item in session.scalars(stmt)]

    def prepare(self, key):
        result = self.prepare_batch([key])
        if not result['documents']:
            raise HTTPException(result['status_code'] if result['status_code'] != 200 else 502, result['error'])
        return result['documents'][0]

    def prepare_batch(self, keys):
        """读取已有结果，一次调用 Agent，统一保存返回的成功草稿。"""
        keys = list(dict.fromkeys(keys))
        results, sources = {}, []
        with self.sessions.begin() as session:
            # 1. 锁住所选文档，复用正式题目或已有草稿。
            docs = self._lock_documents(session, keys)
            for key in keys:
                doc = docs[key]
                if doc.questions_llm_confirmed:
                    rows = session.scalars(select(Question).where(Question.document_id == key)
                                           .order_by(Question.sort_order))
                    results[key] = {'document_id': key, 'status': 'confirmed',
                                    'questions': [show_question(row) for row in rows]}
                elif (draft := session.get(DocumentDraft, key)) is not None:
                    results[key] = {'document_id': key, 'status': 'draft', 'questions': draft.questions}
                else:
                    sources.append({'document_id': key, 'filename': doc.filename,
                                    'source': self._read_original(key).decode('utf-8-sig')})
            # 只把首次整理的原文交给 Agent；失败批次由 Agent 排除。
            outcome = {'documents': [], 'error': None, 'status_code': 200}
            if sources:
                outcome = parse_questions.parse_questions(sources)
                # 2. Agent 已完成结构和文档归属校验，统一保存返回的成功结果。
                for group in outcome['documents']:
                    key = group['document_id']
                    session.add(DocumentDraft(document_id=key, questions=group['questions']))
                    results[key] = {**group, 'status': 'draft'}
        # 3. 事务成功后，按选择顺序返回可预览的结果及失败提示。
        return {'documents': [results[key] for key in keys if key in results],
                'error': outcome['error'], 'status_code': outcome['status_code']}

    @staticmethod
    def _lock_documents(session, keys):
        rows = session.scalars(select(Document).where(Document.id.in_(keys)).order_by(Document.id).with_for_update())
        docs = {row.id: row for row in rows}
        if docs.keys() != set(keys):
            raise HTTPException(404, '文档不存在或已删除')
        return docs

    @staticmethod
    def _number_counter(session):
        # 单行写锁与题目处于同一事务，整批提交或回滚。
        session.execute(insert(QuestionCounter).values(id=1, last_number=0).on_duplicate_key_update(id=1))
        return session.get(QuestionCounter, 1, with_for_update=True)

    def confirm_batch(self, groups):
        keys = [group.document_id for group in groups]
        if not keys or len(set(keys)) != len(keys):
            raise HTTPException(422, '请提交一份或多份不同文档的草稿')
        ids = []
        with self.sessions.begin() as session:
            # 1. 锁定文档和全库题号，整批写入使用同一个事务。
            docs = self._lock_documents(session, keys)
            counter = self._number_counter(session)
            for group in groups:
                doc = docs[group.document_id]
                if doc.questions_llm_confirmed:
                    ids.extend(session.scalars(select(Question.question_id).where(Question.document_id == doc.id)
                                               .order_by(Question.sort_order)))
                    continue
                # 2. 按用户核对的顺序入库，已确认文档直接复用原 ID。
                seen = set()
                for draft in group.questions:
                    source_key = sha256((draft.locator + '\0' + draft.question).encode()).hexdigest()
                    if source_key in seen:
                        continue
                    seen.add(source_key)
                    counter.last_number += 1
                    item = Question(**draft.model_dump(), document_id=doc.id, source_key=source_key,
                                    sort_order=counter.last_number)
                    session.add(item)
                    session.flush()
                    ids.append(item.question_id)
                # 3. 标记文档已确认并移除草稿，随正式题目一起提交。
                doc.questions_llm_confirmed = True
                session.execute(delete(DocumentDraft).where(DocumentDraft.document_id == doc.id))
        return {'question_ids': ids}

    def save_question(self, data, key=None):
        with self.sessions.begin() as session:
            if key is None:
                counter = self._number_counter(session)
                counter.last_number += 1
                item = Question(**data.model_dump(), sort_order=counter.last_number)
                session.add(item)
            else:
                item = get_record(session, Question, key, lock=True)
                if item.question != data.question or item.tags != data.tags:
                    item.index_status = 'pending'
                for name, value in data.model_dump().items():
                    setattr(item, name, value)
            session.flush()
            return show_question(item)

    def rebuild_index(self):
        """按钮触发一次全库重建。Agent 负责向量，后端负责读取题目和回写状态。"""
        if not self.index_lock.acquire(blocking=False):
            raise HTTPException(409, '全量重建正在进行中')
        try:
            # 1. 读取全库正式题目，交给 Agent 更新向量。
            questions = self.questions('', None)
            try:
                rag.rebuild_index(questions)
            except HTTPException:
                raise
            except Exception as exc:
                status = getattr(exc, 'status_code', None) or getattr(getattr(exc, 'response', None), 'status_code', None)
                if status == 413:
                    raise HTTPException(413, '向量请求超过大小限制，本次重建已停止') from None
                raise HTTPException(502, '全量重建失败，请检查 Embedding 和 Chroma 服务') from None

            # 2. 向量操作结束后再回写状态，期间改动的题目保持待索引。
            snapshot = {q['question_id']: (q['question'], q['tags']) for q in questions}
            indexed = 0
            with self.sessions.begin() as session:
                for row in session.scalars(select(Question).with_for_update()):
                    if snapshot.get(row.question_id) == (row.question, row.tags):
                        row.index_status = 'indexed'
                        indexed += 1
            return {'total': len(questions), 'indexed': indexed}
        finally:
            self.index_lock.release()

    def search(self, topic, limit):
        # 按考点召回，再按原排名返回仍有效的题目和最新答案。
        ids = rag.search_ids(topic, limit)
        if not ids:
            return {'items': []}
        with self.sessions() as session:
            rows = session.scalars(select(Question).where(Question.question_id.in_(ids), Question.index_status == 'indexed'))
            by_id = {q.question_id: show_question(q) for q in rows}
        return {'items': [by_id[key] for key in ids if key in by_id]}

    def remove_question(self, key):
        self.remove_questions([key])

    def remove_questions(self, keys):
        with self.index_lock, self.sessions.begin() as session:
            # 先锁定全部所选题目，再一次删除对应向量；异常会回滚本次 SQL 删除。
            keys = set(keys)
            rows = session.scalars(select(Question).where(Question.question_id.in_(keys))
                                   .order_by(Question.question_id).with_for_update()).all()
            if len(rows) != len(keys):
                raise HTTPException(404, '所选题目已变化，请刷新后重新选择')
            for row in rows:
                session.delete(row)
            session.flush()
            self.delete_vectors([row.question_id for row in rows])
            return {'deleted': len(rows)}
