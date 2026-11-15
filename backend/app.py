from contextlib import asynccontextmanager
from hashlib import sha256
from secrets import compare_digest
from pathlib import PureWindowsPath

from fastapi import Depends, FastAPI, HTTPException, UploadFile, Query, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import APIKeyHeader
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from openai import OpenAIError
from .config import Settings, ROOT
from .database import make_engine
from .middleware import BodyLimitMiddleware
from .schemas import BatchQuestionDrafts, DocumentSelection, QuestionDrafts, QuestionInput, QuestionSelection, SearchInput
from .service import Library


def create_app(settings=None):
    settings = settings or Settings()
    engine = make_engine(settings)
    library = Library(sessionmaker(engine, expire_on_commit=False), settings.originals_dir)

    @asynccontextmanager
    async def lifespan(app):
        yield
        engine.dispose()

    app = FastAPI(title='面试知识库', lifespan=lifespan)
    app.add_middleware(BodyLimitMiddleware)
    app.mount('/static', StaticFiles(directory=ROOT / 'frontend'), name='frontend')

    @app.exception_handler(OpenAIError)
    async def model_error(request, exc):
        status = 413 if getattr(exc, 'status_code', None) == 413 else 502
        return JSONResponse({'detail': '模型请求超过大小限制' if status == 413 else '模型请求失败'}, status_code=status)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        return JSONResponse({'detail': '知识库数据库不可用，请检查连接并初始化数据库。'}, status_code=503)

    @app.get('/')
    def index():
        frontend = ROOT / 'frontend'
        html = (frontend / 'index.html').read_text(encoding='utf-8')
        for name in ('app.js', 'styles.css'):
            version = sha256((frontend / name).read_bytes()).hexdigest()[:16]
            html = html.replace(f'{name}?v=local', f'{name}?v={version}')
        return HTMLResponse(html, headers={'Cache-Control': 'no-cache'})

    @app.get('/api/documents')
    def documents():
        return library.documents()

    @app.post('/api/documents', status_code=201)
    def upload(file: UploadFile):
        name = PureWindowsPath(file.filename or '').name
        if not name.lower().endswith('.md') or len(name) > 255:
            raise HTTPException(422, '请选择 Markdown（.md）文件')
        content = file.file.read(16 * 1024**2 + 1)
        if len(content) > 16 * 1024**2:
            raise HTTPException(413, '单份 Markdown 原件不能超过 16 MiB')
        try:
            source = content.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise HTTPException(422, 'Markdown 必须使用 UTF-8 编码') from None
        if not source.strip() or '\0' in source:
            raise HTTPException(422, '请上传非空的 Markdown 文本')
        return library.upload(name, content)

    @app.get('/api/documents/{key}/content')
    def content(key: str):
        return Response(content=library.original(key), media_type='text/plain; charset=utf-8',
                        headers={'X-Content-Type-Options': 'nosniff'})

    @app.delete('/api/documents/{key}', status_code=204)
    def remove_document(key: str):
        library.remove_document(key)
        return Response(status_code=204)

    @app.post('/api/documents/prepare-batch')
    def prepare_batch(body: DocumentSelection):
        result = library.prepare_batch(body.document_ids)
        code = result.pop('status_code')
        return JSONResponse(jsonable_encoder(result), status_code=code)

    @app.post('/api/documents/{key}/prepare')
    def prepare(key: str):
        return library.prepare(key)

    @app.post('/api/documents/confirm-batch')
    def confirm_batch(body: BatchQuestionDrafts):
        return library.confirm_batch(body.documents)

    @app.post('/api/documents/{key}/confirm')
    def confirm(key: str, body: QuestionDrafts):
        if body.document_id != key:
            raise HTTPException(422, '文档 ID 与确认目标不一致')
        return library.confirm_batch([body])

    @app.get('/api/questions')
    def questions(q: str = Query('', max_length=500), document_id: str | None = None):
        return library.questions(q, document_id)

    @app.post('/api/questions', status_code=201)
    def create_question(body: QuestionInput):
        return library.save_question(body)

    @app.put('/api/questions/{key}')
    def edit_question(key: str, body: QuestionInput):
        return library.save_question(body, key)

    @app.post('/api/questions/batch-delete')
    def remove_questions(body: QuestionSelection):
        return library.remove_questions(body.question_ids)

    @app.delete('/api/questions/{key}', status_code=204)
    def remove_question(key: str):
        library.remove_question(key)
        return Response(status_code=204)

    @app.post('/api/index/rebuild')
    def rebuild_index():
        return library.rebuild_index()

    # 读取请求头，密钥内容由下面的路由显式核对。
    key_header = APIKeyHeader(name='X-API-Key', auto_error=False)

    @app.post('/api/search')
    def search(body: SearchInput, key: str | None = Depends(key_header)):
        expected = settings.knowledge_api_key.get_secret_value().strip()
        # 未配置时关闭检索，避免空密钥被误认为有效配置。
        if not expected:
            raise HTTPException(503, '知识库尚未配置 KNOWLEDGE_API_KEY')
        # 比较完整内容，通过后才进行向量化、召回和数据库读取。
        if not compare_digest((key or '').encode(), expected.encode()):
            raise HTTPException(401, '知识库检索密钥无效')
        return library.search(body.topic, body.limit, body.distance_limit)

    return app
