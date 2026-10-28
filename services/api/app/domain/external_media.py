"""外部视频目录记录；许可和目录描述均保留来源。"""
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


class ExternalVideo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    provider: str
    source_id: str = Field(min_length=1, max_length=500)
    title: str
    description: str
    page_url: str
    download_url: str
    mime: str
    size_bytes: int | None = Field(default=None, ge=0)
    width: int = Field(ge=0)
    height: int = Field(ge=0)
    dimensions_exact: bool = True
    sha1: str | None = None
    duration_ms: int | None = Field(default=None, gt=0)
    author: str
    license_name: str
    license_url: str
    attribution: str
    admissible: bool
    admission_reason: str

