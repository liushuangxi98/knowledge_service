from pydantic import BaseModel, ConfigDict, Field


class QuestionInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    question: str = Field(min_length=1, max_length=10000, description='一道题的完整题干，保留问题中的条件和要求。')
    answer: str = Field(default='', max_length=30000, description='题目的参考答案。从文档拆题时保留原文答案，原文未提供答案时留空。')
    tags: list[str] = Field(default_factory=list, max_length=30, description='这道题涉及的知识点名称列表，例如“向量检索”和“重排”；没有标签时使用空列表。')
    locator: str = Field(default='', max_length=500, description='题目在来源文档中的位置，例如“第二章 / 第 3 题”；没有来源位置时留空。')


class QuestionDrafts(BaseModel):
    document_id: str = Field(min_length=1, description='所属输入文档的 ID，必须原样返回。')
    questions: list[QuestionInput] = Field(description='该文档的题目草稿，没有题目时返回空列表。')


class BatchQuestionDrafts(BaseModel):
    documents: list[QuestionDrafts] = Field(description='每份输入文档对应一个结果，不遗漏、不合并。')


class DocumentSelection(BaseModel):
    document_ids: list[str] = Field(min_length=1)


class QuestionSelection(BaseModel):
    question_ids: list[str] = Field(min_length=1)


class SearchInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    distance_limit: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False,
                                         description='最大余弦距离，越小越严格；超过阈值的题目不返回')
    topic: str = Field(min_length=1, max_length=2000, description='希望检索的考点或问题，例如“如何评估向量检索的召回效果”。')
    limit: int = Field(default=5, ge=1, le=100, description='本次检索最多返回的题目数量，默认 5 道，范围为 1 到 100 道。')
