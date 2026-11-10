# 知识库后端与 Agent 开发交接

适用工程：`knowledge_service`。管理页面和 FastAPI 使用 8200，独立 Chroma 服务默认使用 8100。

管理页面通过 FastAPI 调用三个同步模块：Markdown 拆题、文本向量化、Chroma 全量重建与考点召回。后端维护正式题目、确认状态及索引状态。

| 阶段 | 谁提供输入 | 执行逻辑 | 结果交给谁 |
| --- | --- | --- | --- |
| 上传 Markdown | 浏览器上传文件 | 已实现，上传阶段没有模型调用 | 原件在磁盘，文档 ID 在 MySQL |
| 整理草稿 | 后端传入文档 ID、文件名和原文的列表 | `parse_questions(documents)` | 返回按文档分组的草稿，前端切换预览、编辑 |
| 确认入库 | 前端提交用户改好的整批草稿 | 已实现，由后端执行一个 MySQL 事务 | 返回正式 `question_ids`，新题为 `pending` |
| 建立索引 | 用户点「全量重建向量索引」，后端传入全部正式题目 | 调用百炼、按题目 ID 写 Chroma | 成功的题目标为 `indexed` |
| 考点检索 | 调用方传入考点和返回数量 | 对考点做向量化，召回 ID，从 MySQL 取回题目和出处 | 知识库通过 HTTP 返回给面试 Agent |

主流程：**上传 → 整理草稿 → 人工确认并入库；手动点击全量重建 → 向量入库**。相关按钮与后端的实际交接见[前端操作](frontend/app.js)、[HTTP 路由](backend/app.py)和[业务服务](backend/service.py)。

## 1. 拆题：批量输入与分组输出

同步入口为 `parse_questions(documents: list[dict]) -> list[dict]`，单文件也使用一个元素的列表。

输入的每项含 `document_id`、`filename`、`source`。Agent 一次模型调用处理整批原文，使用 `BatchQuestionDrafts` 包装 `QuestionDrafts` 列表；`QuestionDrafts` 含原样返回的 `document_id` 和 `questions: list[QuestionInput]`。每份文档独立提取题干、原文答案、标签和 locator，没有题目允许空列表，不跨文档合并。模型解析失败向上抛错，不返回空列表冒充成功。

后端 `POST /api/documents/prepare-batch` 接收 `document_ids`，重复 ID 去重。已确认文档直接读 MySQL；已整理草稿直接复用，首次整理的原文按每批按 .env 的 PARSE_BATCH_SIZE（当前为 20）交给 Agent，每批草稿独立提交，失败即停止。后端检查输出分组不存在缺失、重复或未知 ID，按输入顺序返回 `documents`，各项含 `document_id`、`status` 和 `questions`。单文档 prepare 复用同一业务方法。合并原文按序列化字节检查，超限在模型调用前停止，413 不自动重试。

前端按文件切换编辑和预览，通过 /api/documents/confirm-batch 一次确认本批全部待入库文档。单文档接口使用 `/api/documents/{id}/confirm`，请求体需要 `document_id` 与路径一致，以及 `questions` 列表。空结果也可确认。题目字段约束以 `backend/schemas.py` 为准。

## 2. 确认：后端交给索引器的是什么

用户可以在预览中修改题干或标签，再点「确认入库」。后端执行 `Library.confirm_batch(groups)`，按稳定顺序锁住所选文档，把题目和文档确认标记放在同一个事务中保存。重复确认已经确认过的文档，只返回已有题目 ID。接口返回示意：

```json
{"question_ids": ["q_101", "q_102"]}
```

这里和下文的 ID 为示意值。确认接口按最终草稿顺序分配全库递增题号 sort_order，重复题只保留首次出现的位置；返回的 ID 也按保存的题号排列。索引器应该按 ID 读取正式记录，以用户最后确认或编辑过的文字为准。[入库实现](backend/service.py) · [两张表的定义](backend/database.py)

| 存储字段 | 什么时候写 | 回答的问题 |
| --- | --- | --- |
| `documents.questions_llm_confirmed` | 上传为 False，整批确认成功后为 True | 这篇文档再打开时，要不要拆题？ |
| `questions.index_status` | 新题为 pending；索引器写向量成功后应设为 indexed | 这道正式题目要不要建索引？ |

**重启举例**：已保存 5 道题，3 道 indexed、2 道 pending。点击整理草稿只显示已有题目；点击全量重建会把这 5 道全部交给 Agent。

## 3. 全量重建：已接好的 Agent 接口

只在用户点击按钮时调用 `POST /api/index/rebuild`，不传搜索词或题目 ID。后端从 MySQL 读取当时全部正式题目，调用 [rag.py](knowledge_agent/rag.py) 的同步函数 `rebuild_index(questions: list[dict]) -> None`。每项含 question_id、question、answer、tags、locator、document_id 和 index_status。函数正常返回表示本轮成功，失败请抛异常。Chroma 不可用时提示检查服务。

1. 用每条题干和标签组织文本，在 [embeddings.py](knowledge_agent/embeddings.py) 调百炼生成向量；即使 index_status 已是 indexed，也重新生成。
2. 用正式 question_id 作 Chroma ID 分批 upsert。全部写成功后，对比本集合中的 ID 和本次题目 ID，删除只存在于 Chroma 的旧记录；不要先清空集合。传入空列表表示 MySQL 已无题目，此时清理本题库集合的旧向量即可。
3. 后端在函数成功返回后回写 indexed；重建期间新增、删除或改动了题干和标签的题目不计入这次成功数。Agent 失败时后端不回写状态。下次再点按钮仍从全库开始。

手动添加、确认和编辑只写 MySQL。删除单题或文档时，后端把所有受影响的题目 ID 以 [{"question_id": ID}] 交给同步函数 rag.del_index；零道题时跳过调用。接口应当把不存在的向量视为已删除，以便安全重试。向量删除抛错时，MySQL 事务回滚，暂存的 Markdown 原件恢复。

## 4. 检索

发送 `POST /api/search`，例如 `{"topic":"如何改善召回结果的顺序？","limit":3}`。后端调用 `rag.search_ids(topic, limit)` 取得按相似度排列的题目 ID，再回 MySQL 排除已删除或待索引的题目。过滤后可能少于 limit 道，返回 `{"items":[...]} `，包含正式 ID、最新题干、答案、标签、document_id 和 locator。模拟面试业务通过该 HTTP 接口取题。

## 5. 按什么顺序验收

1. **先测拆题。** 准备一份有 5 道题的 Markdown，至少一道没有答案。上传后点「整理草稿」应预填 5 道；核对空答案和每个 locator。确认其中一处人工修改，刷新或重启后再次打开，看到原来的 5 个 ID、人工修改仍在，拆题调用次数仍为 1。
2. **再测向量。** 用模型替身检查 5 个正式 ID 与 5 个输入文本、5 个向量一一对应，再用真实服务验证一份小样本。模拟百炼或 Chroma 暂时失败，题目应仍可在页面查看；恢复后手动全量重建，用相同 ID 写入，向量记录数仍为 5。
3. **最后测检索和变更。** 用原文没有出现的同义问法查询，能找回预期题目并根据 locator 打开原文。只改答案后应读到新答案；改题干后应重建；删题或删文档后检索应排除失效记录，删除请求同时清理对应向量。

现有 [tests/test_library.py](tests/test_library.py) 已覆盖“已确认跳过解析器”、失败回滚及重复确认。数据库回归使用隔离实例，真实模型、真实向量和检索效果要分别验证。启动命令和测试环境变量见[项目使用说明](项目使用说明.md)。

## 批量题目删除

`POST /api/questions/batch-delete` 接收非空 `question_ids`，复用现有索引锁，在一个 MySQL 事务中检查所选题目、批量删除，并一次调用 `rag.del_index`。重复 ID 去重；含已不存在的题目时返回 404，提示刷新，不删除剩余项。向量删除失败回滚 MySQL。成功返回 `deleted` 数量，保留 Markdown 原件与文档确认标记。前端全选范围是当前筛选结果，刷新后清空选择。
