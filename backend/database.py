from datetime import datetime, UTC
from uuid import uuid4
from sqlalchemy import CHAR, JSON, DateTime, ForeignKey, String, Text, UniqueConstraint, create_engine, text
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = 'documents'
    id: Mapped[str] = mapped_column(CHAR(32), primary_key=True, default=lambda: uuid4().hex)
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(CHAR(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))


class Question(Base):
    __tablename__ = 'questions'
    __table_args__ = (UniqueConstraint('document_id', 'source_key'),)
    question_id: Mapped[str] = mapped_column(CHAR(32), primary_key=True, default=lambda: uuid4().hex)
    document_id: Mapped[str | None] = mapped_column(ForeignKey('documents.id', ondelete='CASCADE'))
    # 保存导入时的指纹，后来人工改题后再次确认同一份草稿仍可复用最终题目 ID。
    source_key: Mapped[str | None] = mapped_column(CHAR(64))
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(MEDIUMTEXT)
    tags: Mapped[list] = mapped_column(JSON)
    locator: Mapped[str] = mapped_column(String(500))
    index_status: Mapped[str] = mapped_column(String(16), default='pending')


def make_engine(settings, *, admin=False):
    return create_engine(settings.database_url(admin=admin), pool_pre_ping=True, hide_parameters=True)


def initialize(settings):
    # 先连接实例建独立库，再在该库中建表；不读取求职工作台的库和配置。
    admin = make_engine(settings, admin=True)
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE DATABASE IF NOT EXISTS `{settings.mysql_database}` CHARACTER SET utf8mb4'))
    finally:
        admin.dispose()
    engine = make_engine(settings)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


if __name__ == '__main__':
    from .config import Settings
    settings = Settings()
    initialize(settings)
    print(f'知识库数据库 {settings.mysql_database} 已就绪。')
