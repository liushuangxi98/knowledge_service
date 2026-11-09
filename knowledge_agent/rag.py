"""维护和查询独立 Chroma 服务。输入最终题目 ID 和向量以同步索引；输入考点和数量以召回 ID，再从 MySQL 返回有效的题目、答案和出处。"""
from langchain_core.documents import Document
from chromadb.config import Settings as ChromadbSettings
from langchain_chroma import Chroma

from .config import Settings
from .embeddings import build_embeddings

def rebuild_index(questions):
    settings = Settings()
    # 取文档和id
    documents_dict = {i['question_id']: Document(page_content=i['question'] + f"tag:{i['tags']}") for i in questions}
    # 向量库
    chroma_database_version = settings.get_chroma_database_version()
    chroma_store = Chroma(
        collection_name=chroma_database_version,
        embedding_function=build_embeddings(),
        persist_directory=str(settings.chroma_dir),
        collection_metadata={"hnsw:space": "cosine"}
    )
    # 去重添加文档
    need_add_ids = list(set(documents_dict.keys()) - set(chroma_store.get()["ids"]))
    chroma_store.add_documents([documents_dict[i] for i in need_add_ids], ids=need_add_ids)

def del_index(questions):
    settings = Settings()
    # 需要删除的ids
    need_del_ids = [i['question_id'] for i in questions]
    # 向量库
    chroma_database_version = settings.get_chroma_database_version()
    chroma_store = Chroma(
        collection_name=chroma_database_version,
        embedding_function=build_embeddings(),
        persist_directory=str(settings.chroma_dir),
        collection_metadata={"hnsw:space": "cosine"}
    )
    # 交集删除
    chroma_store.delete(list(set(need_del_ids) & set(chroma_store.get()["ids"])))

def search_ids(topic: str, limit: int):
    settings = Settings()
    # 向量库
    chroma_database_version = settings.get_chroma_database_version()
    chroma_store = Chroma(
        collection_name=chroma_database_version,
        embedding_function=build_embeddings(),
        persist_directory=str(settings.chroma_dir),
        collection_metadata={"hnsw:space": "cosine"}
    )

    matches = chroma_store.similarity_search_with_score(topic, k=limit)
    return [doc.id for doc, _ in matches]
