# 知识库后端与 Agent 开发交接

适用工程：`knowledge_service`，按提交 `3e812d5` 核对。管理页面和 FastAPI 使用 8200，独立 Chroma 服务默认使用 8100。

**先写 `parse_questions.py`。** 它是目前唯一已经有后端调用入口的 Agent 模块。实现约定的同步函数、重启服务，就能从「整理草稿」按钮验证。之后再写 `embeddings.py` 和 `rag.py`；这两个文件写好后，还要把索引和检索函数接到后端。

| 阶段 | 谁提供输入 | 你要写什么 | 结果交给谁 |
| --- | --- | --- | --- |
| 上传 Markdown | 浏览器上传文件 | 已实现，上传阶段没有模型调用 | 原件在磁盘，文档 ID 在 MySQL |
| 整理草稿 | 后端传入文档 ID 和已解码的原文 | `parse_questions(document_id, source)` | 返回题目列表，前端预览、编辑 |
| 确认入库 | 前端提交用户改好的整批草稿 | 已实现，由后端执行一个 MySQL 事务 | 返回正式 `question_ids`，新题为 `pending` |
| 建立索引 | 后端交出正式题目 ID | 读取待索引题目、调用百炼、按题目 ID 写 Chroma | 成功的题目标为 `indexed` |
| 考点检索 | 调用方传入考点和返回数量 | 对考点做向量化，召回 ID，从 MySQL 取回题目和出处 | 知识库通过 HTTP 返回给面试 Agent |

主流程：**上传 → 整理草稿 → 人工确认 → 题目入库 → 向量入库**。前三个按钮与后端的实际交接见[前端操作](frontend/app.js)、[HTTP 路由](backend/app.py)和[业务服务](backend/service.py)。

## 1. 拆题：已经接好调用入口

在 [knowledge_agent/parse_questions.py](knowledge_agent/parse_questions.py) 中实现这个签名：

```python
def parse_questions(document_id: str, source: str) -> list[dict]:
    ...
```

点击「整理草稿」会发送 `POST /api/documents/{id}/prepare`。后端的 `Library.prepare()` 先查看该文档的 `questions_llm_confirmed`：

- **True**：直接从 MySQL 取这份文档现存的正式题目，返回 `status="confirmed"`。哪怕五道题全部是 `pending`，拆题函数也不会被调用。用户后来删光了题目，会得到空的已确认列表。
- **False**：后端读取原件，以 UTF-8 解码（兼容 BOM），执行 `parse_questions(document_id, source)`。它会逐条用 `QuestionInput` 校验返回值，再以 `status="draft"` 交给前端。解析器未接入时接口返回 501，页面允许手动整理。

**函数内部只需要处理原文。** 从 `source` 找出题目边界，提取题干、原文答案和原文位置，补上知识点标签。使用 LLM 时，可以把这些要求写进提示词，用一个含 `questions: list[QuestionInput]` 的结构化结果接收输出，最后返回其中的题目列表。后端当前直接同步调用这个函数，因此这里使用普通 `def`。如模型调用失败，要让调用方看到错误；空列表只用于确实没有提取到题目的情况。

例如，原文是：

```markdown
## 第二章 检索
3. 什么是重排？
答：对已召回的候选结果重新排序。
4. 怎样评估召回效果？
```

函数应返回下面这样的 Python 列表。外层 HTTP 响应、文档关联、正式题目 ID 都由后端负责。

```python
[
    {"question": "什么是重排？", "answer": "对已召回的候选结果重新排序。",
     "tags": ["重排"], "locator": "第二章 检索 / 第3题"},
    {"question": "怎样评估召回效果？", "answer": "",
     "tags": ["召回评测"], "locator": "第二章 检索 / 第4题"},
]
```

`locator` 应能让人回到原文找到该题，例如章节、原题号或段落位置。原文没写答案就保留空字符串。题干必填；额外返回 `question_id`、`document_id`、`index_status` 等字段会被当前草稿模型拒绝。字段长度和默认值以 [QuestionInput](backend/schemas.py) 为准。

## 2. 确认：后端交给索引器的是什么

用户可以在预览中修改题干或标签，再点「确认入库」。后端执行 `Library.confirm(document_id, drafts)`，锁住该文档，把题目和文档确认标记放在同一个事务中保存。重复确认已经确认过的文档，只返回已有题目 ID。接口返回示意：

```json
{"question_ids": ["q_101", "q_102"]}
```

这里和下文的 ID 为示意值。实际接口返回最终 ID 的排序去重列表，**不能按返回顺序和提交草稿的位置配对**。索引器应该按 ID 读取正式记录，以用户最后确认或编辑过的文字为准。[入库实现](backend/service.py) · [两张表的定义](backend/database.py)

| 存储字段 | 什么时候写 | 回答的问题 |
| --- | --- | --- |
| `documents.questions_llm_confirmed` | 上传为 False，整批确认成功后为 True | 这篇文档再打开时，要不要拆题？ |
| `questions.index_status` | 新题为 pending；索引器写向量成功后应设为 indexed | 这道正式题目要不要建索引？ |

**重启举例**：文档已确认，5 道题均为 `pending`。点击「整理草稿」只显示原来的 5 道。需要恢复的工作是按这 5 个 ID 建索引。若上次只做了草稿预览，还没点确认，草稿只在浏览器内存；重新打开未确认文档会再拆一次。

## 3. 向量入库：你需要实现的两段代码

以下函数名是建议，当前后端没有调用它们。

**第一段放在 [embeddings.py](knowledge_agent/embeddings.py)**：建议实现 `embed_texts(texts: list[str]) -> list[list[float]]`。它只接收文本，只返回向量，保持输入和输出顺序一致。用百炼的文本 Embedding 接口，以相同模型和维度分别处理题目和查询。批量大小遵循你选的型号限制；如果响应带有 `index`，按它恢复输入顺序。URL、Key、模型名配置在知识库工程自己的环境里，当前 [Settings](backend/config.py) 还没有这些模型字段。[百炼 Embedding 调用文档](https://help.aliyun.com/zh/model-studio/embedding-interfaces-compatible-with-openai/)

**第二段放在 [rag.py](knowledge_agent/rag.py)**：实现按正式题目 ID 建索引。以 `q_101` 为例：

1. 从 MySQL 按 ID 读取这道题。若当前是 `pending`，用**题干 + 标签**生成文本，例如 `什么是重排？\n知识点：重排`。
2. 把文本交给 `embed_texts()`。将同一条记录的 ID、文本、向量以及出处一起传给 Chroma。通过配置的 `chroma_url` 连接已有的 HTTP 服务，写入、查询都使用同一个集合。
3. Chroma 写成功后，在 MySQL 把该题标为 `indexed`。调用失败时保留已确认题目和 `pending`，下一次复用同一个 ID 补建。若生成向量期间允许改题，回写状态前要核对题干和标签仍是本次使用的内容。

下面是一次写入的形状。传入自己算好的向量；手动录入的独立题目没有 `document_id`，构建 metadata 时可以省略该键。

```python
collection.upsert(
    ids=[question_id],
    documents=[text],
    embeddings=[vector],
    metadatas=[{"document_id": document_id, "locator": locator}],
)
```

**后端接线点**在 `POST /api/documents/{id}/confirm`：先等 `library.confirm()` 成功返回，再把 `result["question_ids"]` 交给索引流程。不要在保存整批题目的 MySQL 事务里等待模型网络请求。若索引失败，页面要能区分“题目已保存”和“索引未完成”。另外提供一个明确的补建入口，按 `pending` 取正式题目，复用同一段索引代码，这样重启后不用重新上传或拆题。**现在成功确认只会写 MySQL，还没有自动建索引或补建入口。**

另外三条写入路径也要接同一套索引逻辑：

| 操作 | 后端当前行为 | 接入向量库后要做的事 |
| --- | --- | --- |
| 手动新增一道题 | 生成 ID，状态 pending | 用返回的题目 ID 建索引 |
| 编辑题目 | 题干或标签改动时设回 pending；只改答案保留原状态 | 对 pending 题重新 upsert；只改答案时检索结果直接读 MySQL 最新答案 |
| 删除题目或文档 | 删题，或由外键级联删除文档所属题目 | 按被删题目的 ID 清理向量；删文档应在级联删除前取到这些 ID，或按已写入的 document_id 清理。保留其他文档和独立题目 |

## 4. 检索：面试 Agent 如何拿到答案和出处

将来求职项目只需调用知识库的 `POST /api/search`，例如发送 `{"topic":"如何改善召回结果的顺序？","limit":3}`。建议在 `rag.py` 实现以下顺序：

1. 用相同的 Embedding 模型把 `topic` 转成一个查询向量。
2. 用 `collection.query(query_embeddings=[query_vector], n_results=limit)` 召回候选。单条查询的 ID 排名在 `result["ids"][0]`。
3. 按召回 ID 回 MySQL 读取题干、最新参考答案、标签、文档 ID 和原文位置，排除已删除或尚待重建的记录，按 Chroma 的排名组装最多 `limit` 条结果。没有有效命中才返回空结果。
4. 在 [backend/app.py](backend/app.py) 的 `search()` 接上这个函数；同时替换路由装饰器上的固定 `status_code=501` 和函数体内的 `HTTPException(501, ...)`。当前只定义了请求模型，成功响应的字段需要在接线时与调用方确定。建议使用 `{"items": [...]}`，每项保留真实的 `question_id`、题干、答案、`document_id` 和 `locator`。

面试服务拿到这些题目后，再决定问哪一题、是否追问、怎样评价回答。知识库提供题目和出处。

## 5. 按什么顺序验收

1. **先测拆题。** 准备一份有 5 道题的 Markdown，至少一道没有答案。上传后点「整理草稿」应预填 5 道；核对空答案和每个 locator。确认其中一处人工修改，刷新或重启后再次打开，看到原来的 5 个 ID、人工修改仍在，拆题调用次数仍为 1。
2. **再测向量。** 用模型替身检查 5 个正式 ID 与 5 个输入文本、5 个向量一一对应，再用真实服务验证一份小样本。模拟百炼或 Chroma 暂时失败，题目应仍可在页面查看；恢复后补建，用相同 ID 写入，向量记录数仍为 5。
3. **最后测检索和变更。** 用原文没有出现的同义问法查询，能找回预期题目并根据 locator 打开原文。只改答案后应读到新答案；改题干后应重建；删题或删文档后，检索不能返回已删除题目。

现有 [tests/test_library.py](tests/test_library.py) 已覆盖“已确认跳过解析器”、失败回滚、旧库标记回填及重复确认。开始写真实 Agent 时，把 `test_agent_modules_contain_only_their_docstrings` 这个阶段性占位断言替换成相应模块的行为测试；接通搜索时同步更新断言 501 的用例。数据库回归使用隔离实例，真实模型、真实向量和检索效果要分别验证。启动命令和测试环境变量见[项目使用说明](项目使用说明.md)。
