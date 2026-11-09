from contextlib import asynccontextmanager
from pathlib import PureWindowsPath

from fastapi import FastAPI, HTTPException, UploadFile, Query, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from .config import Settings, ROOT
from .database import make_engine
from .middleware import BodyLimitMiddleware
from .schemas import QuestionDrafts, QuestionInput, SearchInput
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

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        return JSONResponse({'detail': '知识库数据库不可用，请检查连接并初始化数据库。'}, status_code=503)

    @app.get('/')
    def index():
        return FileResponse(ROOT / 'frontend' / 'index.html')

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

    @app.post('/api/documents/{key}/prepare')
    def prepare(key: str):
        return library.prepare(key)

    @app.post('/api/documents/{key}/confirm')
    def confirm(key: str, body: QuestionDrafts):
        return library.confirm(key, body.questions)

    @app.get('/api/questions')
    def questions(q: str = Query('', max_length=500), document_id: str | None = None):
        return library.questions(q, document_id)

    @app.post('/api/questions', status_code=201)
    def create_question(body: QuestionInput):
        return library.save_question(body)

    @app.put('/api/questions/{key}')
    def edit_question(key: str, body: QuestionInput):
        return library.save_question(body, key)

    @app.delete('/api/questions/{key}', status_code=204)
    def remove_question(key: str):
        library.remove_question(key)
        return Response(status_code=204)

    @app.post('/api/index/rebuild')
    def rebuild_index():
        return library.rebuild_index()

    @app.post('/api/search', status_code=501)
    def search(body: SearchInput):
        raise HTTPException(501, '知识检索待接入，请先实现 Embedding、索引与召回。')

    return app
