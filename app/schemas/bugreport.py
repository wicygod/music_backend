from pydantic import BaseModel, Field


class BugReportCreate(BaseModel):
    text: str = Field(min_length=1, max_length=5000)


class BugReportResponse(BaseModel):
    ok: bool
    message: str
