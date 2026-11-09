"""文档原件、已确认题目和索引状态的存储。拆题和检索由 knowledge_agent 接入。"""
from hashlib import sha256
from inspect import iscoroutinefunction
from pathlib import Path
from threading import Lock
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert

from knowledge_agent import parse_questions, rag
from .database import Document, Question
from .schemas import QuestionInput


def show_document(doc):
    return {'id': doc.id, 'filename': doc.filename, 'created_at': doc.created_at,
            'questions_llm_confirmed': doc.questions_llm_confirmed,
            'parse_status': 'ready' if callable(getattr(parse_questions, 'parse_questions', None)) else 'not_connected'}


def show_question(item):
    return {key: getattr(item, key) for key in
            ('question_id', 'question', 'answer', 'tags', 'document_id', 'locator', 'index_status')}


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
            rows = session.scalars(select(Document).order_by(Document.created_at.desc(), Document.id))
            return [show_document(doc) for doc in rows]

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
                return {**show_document(doc), 'reused': doc.id != new_id}
        except Exception:
            if new_file:
                new_file.unlink(missing_ok=True)
            raise

    def original(self, key):
        with self.sessions.begin() as session:
            doc = get_record(session, Document, key, lock=True)
            path = self.originals / (doc.id + '.md')
            if not path.is_file():
                raise HTTPException(404, '原件文件不存在')
            return path.read_bytes()

    def remove_document(self, key):
        moved = False
        try:
            with self.sessions.begin() as session:
                doc = get_record(session, Document, key, lock=True)
                path = self.originals / (doc.id + '.md')
                backup = path.with_suffix('.deleting')
                if path.exists():
                    path.rename(backup)
                    moved = True
                session.delete(doc)  # 外键同步删除此文档的题目。
        except Exception:
            if moved:
                backup.rename(path)
            raise
        if moved:
            backup.unlink()

    def questions(self, query, document_id):
        stmt = select(Question)
        if document_id:
            stmt = stmt.where(Question.document_id == document_id)
        if query:
            stmt = stmt.where(Question.question.contains(query, autoescape=True))
        with self.sessions() as session:
            return [show_question(item) for item in session.scalars(stmt.order_by(Question.question_id))]

    def prepare(self, key):
        """已确认的文档直接读正式题目；只有未确认的文档才需要拆题。"""
        with self.sessions() as session:
            doc = get_record(session, Document, key)
            if doc.questions_llm_confirmed:
                rows = session.scalars(select(Question).where(Question.document_id == key).order_by(Question.question_id))
                return {'status': 'confirmed', 'questions': [show_question(row) for row in rows]}

        parser = getattr(parse_questions, 'parse_questions', None)
        if parser is None:
            raise HTTPException(501, '自动拆题待接入')
        source = self.original(key).decode('utf-8-sig')
        drafts = parser(key, source)
        return {'status': 'draft', 'questions': [QuestionInput.model_validate(item).model_dump() for item in drafts]}

    def confirm(self, key, drafts):
        with self.sessions.begin() as session:
            doc = get_record(session, Document, key, lock=True)
            if doc.questions_llm_confirmed:
                # 另一标签页可能已提交了同一文档，旧草稿不会再写入。
                ids = session.scalars(select(Question.question_id).where(Question.document_id == key)
                                      .order_by(Question.question_id)).all()
                return {'question_ids': ids}
            ids = []
            for draft in drafts:
                # 来源文档、原文位置和题干决定是否为同一题。重复确认保留人工编辑和索引状态。
                source_key = sha256((draft.locator + '\0' + draft.question).encode()).hexdigest()
                item = session.scalar(select(Question).where(
                    Question.document_id == key, Question.source_key == source_key))
                if item is None:
                    item = Question(**draft.model_dump(), document_id=key, source_key=source_key)
                    session.add(item)
                    session.flush()
                ids.append(item.question_id)
            # 与题目写入一起提交。只生成草稿、或事务失败时均不标记为已确认。
            doc.questions_llm_confirmed = True
            return {'question_ids': sorted(set(ids))}

    def save_question(self, data, key=None):
        with self.sessions.begin() as session:
            if key is None:
                item = Question(**data.model_dump())
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
        rebuild = getattr(rag, 'rebuild_index', None)
        if not callable(rebuild) or iscoroutinefunction(rebuild):
            raise HTTPException(501, '全量重建待接入，请实现同步函数 knowledge_agent.rag.rebuild_index(questions)')
        if not self.index_lock.acquire(blocking=False):
            raise HTTPException(409, '全量重建正在进行中')
        try:
            # 传全部正式题目。空库也传 []，供 Agent 清理已删除题目的旧向量。
            questions = self.questions('', None)
            try:
                rebuild(questions)
            except HTTPException:
                raise
            except Exception as exc:
                status = getattr(exc, 'status_code', None) or getattr(getattr(exc, 'response', None), 'status_code', None)
                if status == 413:
                    raise HTTPException(413, '向量请求超过大小限制，本次重建已停止') from None
                raise HTTPException(502, '全量重建失败，请检查 Embedding 和 Chroma 服务') from None

            # 网络调用结束后才开写事务。重建期间有编辑的题目留待下一次手动重建。
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

    def remove_question(self, key):
        with self.sessions.begin() as session:
            session.delete(get_record(session, Question, key, lock=True))
