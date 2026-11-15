"""维护和查询独立 Chroma 服务。输入最终题目 ID 和向量以同步索引；输入考点和数量以召回 ID，再从 MySQL 返回有效的题目、答案和出处。"""
from hashlib import sha256

from langchain_core.documents import Document
from chromadb.config import Settings as ChromadbSettings
from langchain_chroma import Chroma

from .config import Settings
from .embeddings import build_embeddings

def rebuild_index(questions):
    settings = Settings()
    # 向量库
    chroma_database_version = settings.get_chroma_database_version()
    chroma_store = Chroma(
        collection_name=chroma_database_version,
        embedding_function=build_embeddings(),
        persist_directory=str(settings.chroma_dir),
        collection_metadata={"hnsw:space": "cosine"}
    )

    # 对实际用于向量化的文本计算哈希。
    documents_dict = {i['question_id']: Document(
        page_content=f"{i['question']}\ntag:{i['tags']}",
        metadata={'content_hash': sha256(f"{i['question']}\ntag:{i['tags']}".encode("utf-8")).hexdigest()}
    )  for i in questions}

    # 去重添加文档
    old_documents = chroma_store.get()
    old_documents_ids_map_metadata = dict(zip(old_documents['ids'], old_documents['metadatas']))
    # 通过集合取出新增的和重叠的
    need_add_ids = list(set(documents_dict.keys()) - set(old_documents["ids"]))
    need_modify_ids = list(set(documents_dict.keys()) & set(old_documents["ids"]))
    # 检查重叠的内容hash是不是一致
    for i in need_modify_ids:
        if documents_dict[i].metadata['content_hash'] != old_documents_ids_map_metadata[i]['content_hash']:
            need_add_ids.append(i)
    if need_add_ids:
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
    existing_ids = set(need_del_ids) & set(chroma_store.get()["ids"])
    if existing_ids:
        chroma_store.delete(list(existing_ids))

def search_ids(topic: str, limit: int, distance_limit: float | None = None):
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
    # 余弦距离越小越相关；等于阈值也保留。未传阈值时按原召回结果返回。
    return {doc.id: distance for doc, distance in matches if distance_limit is None or distance <= distance_limit}
