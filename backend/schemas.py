from pydantic import BaseModel, ConfigDict, Field


class QuestionInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    question: str = Field(min_length=1, max_length=10000)
    answer: str = Field(default='', max_length=30000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    locator: str = Field(default='', max_length=500)


class Drafts(BaseModel):
    questions: list[QuestionInput] = Field(min_length=1)


class SearchInput(BaseModel):
    topic: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=5, ge=1, le=100)
