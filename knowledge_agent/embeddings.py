"""调用百炼生成向量。输入已确认的题干、知识点或搜索考点，输出可写入 Chroma 或用于查询的向量。"""
from langchain_openai import OpenAIEmbeddings
from .config import Settings

settings = Settings()


def build_embeddings():
    return OpenAIEmbeddings(
        model=settings.embedding_model,
        api_key=settings.embedding_api_key,
        base_url=settings.embedding_base_url,
        timeout=180,
        chunk_size=20,
        check_embedding_ctx_length=False,
    )

