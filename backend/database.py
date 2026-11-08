from datetime import datetime, UTC
from uuid import uuid4
from sqlalchemy import CHAR, JSON, Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, create_engine, inspect, text
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = 'documents'
    id: Mapped[str] = mapped_column(CHAR(32), primary_key=True, default=lambda: uuid4().hex)
    questions_llm_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text('0'))
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
        # create_all 只建缺失的表。旧库建列后用已保存的来源题目补齐标记。
        with engine.begin() as conn:
            columns = {col['name']: col for col in inspect(conn).get_columns('documents')}
            flag = columns.get('questions_llm_confirmed')
            if flag is None:
                conn.execute(text('ALTER TABLE documents ADD questions_llm_confirmed BOOLEAN NULL'))
            if flag is None or flag['nullable']:
                # NULL 表示尚未迁移。MySQL 的 DDL 会提交，迁移中断后可再次运行。
                conn.execute(text('UPDATE documents d SET questions_llm_confirmed = EXISTS '
                                  '(SELECT 1 FROM questions q WHERE q.document_id = d.id) '
                                  'WHERE questions_llm_confirmed IS NULL'))
                conn.execute(text('ALTER TABLE documents MODIFY questions_llm_confirmed BOOLEAN NOT NULL DEFAULT 0'))
    finally:
        engine.dispose()


if __name__ == '__main__':
    from .config import Settings
    settings = Settings()
    initialize(settings)
    print(f'知识库数据库 {settings.mysql_database} 已就绪。')
