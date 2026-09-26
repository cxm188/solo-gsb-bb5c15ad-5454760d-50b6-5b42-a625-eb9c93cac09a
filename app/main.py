"""FastAPI 入口：提供解析、重新编码 API 与静态前端。"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .attachments import build_attachments_zip, list_attachments
from .diff import diff_results
from .mime import encode_tree, parse_message

app = FastAPI(
    title="MIME Multipart 报文检查器",
    description="按 RFC 2045/2046 解析嵌套 multipart 与 Content-Transfer-Encoding，"
                "编辑后按原 boundary 规则与 CTE 折行重新编码；支持 RFC 2231 附件提取。",
    version="1.2.0",
)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class ParseRequest(BaseModel):
    raw: str = Field(default="", description="原始报文（multipart body 或完整 MIME 报文）")
    raw_base64: str | None = Field(default=None, alias="rawBase64",
                                  description="可选：原始报文的 base64，二进制无损通道")
    content_type: str | None = Field(default=None, alias="contentType",
                                    description="可选：完整 Content-Type 头值")
    boundary: str | None = Field(default=None, description="可选：独立 boundary（可带引号）")

    model_config = {"populate_by_name": True}


class EncodeRequest(BaseModel):
    # tree 为 /api/parse 的结果；叶子节点的 transferEncoding 决定按
    # base64/quoted-printable/7bit/8bit/binary 回写，bodyEdited 为真或
    # CTE 切换时重新编码，否则回写 rawBodyBase64（未编辑字节稳定）。
    tree: dict
    include_headers: bool = Field(default=True, alias="includeHeaders")
    filename: str | None = None

    model_config = {"populate_by_name": True}


class DiffSample(BaseModel):
    tree: dict = Field(default_factory=dict, description="一份 /api/parse 返回的 tree")
    issues: list[dict] = Field(default_factory=list,
                               description="同一次解析返回的 issues（用于诊断代码对比）")


class DiffRequest(BaseModel):
    a: DiffSample
    b: DiffSample


class AttachmentsRequest(BaseModel):
    # tree 为当前解析/编辑后的树（与 /api/encode 的入参相同）
    tree: dict

    model_config = {"populate_by_name": True}


class AttachmentsZipRequest(BaseModel):
    tree: dict
    paths: list[str] = Field(default_factory=list,
                             description="勾选导出的叶子节点路径（tree 中的 path）")

    model_config = {"populate_by_name": True}


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/parse")
def api_parse(req: ParseRequest):
    result = parse_message(req.raw, req.content_type, req.boundary, req.raw_base64)
    # 解析即使有 error 也返回 200，错误体现在 issues 里；只有致命异常才 500
    return JSONResponse(result)


@app.post("/api/encode")
def api_encode(req: EncodeRequest):
    result = encode_tree(req.tree, req.include_headers)
    return JSONResponse(result)


@app.post("/api/diff")
def api_diff(req: DiffRequest):
    result = diff_results(req.a.model_dump(), req.b.model_dump())
    return JSONResponse(result)


@app.post("/api/attachments")
def api_attachments(req: AttachmentsRequest):
    """递归列出附件叶子：路径、文件名、字节数与（若不可导出）原因。"""
    return JSONResponse(list_attachments(req.tree))


@app.post("/api/attachments/zip")
def api_attachments_zip(req: AttachmentsZipRequest):
    """把勾选的附件以 CTE 解码后字节打包为 ZIP 并返回二进制。"""
    data, err = build_attachments_zip(req.tree, req.paths)
    if err is not None:
        return JSONResponse(err, status_code=400)
    return Response(content=data,
                    media_type="application/zip",
                    headers={"Content-Disposition":
                             'attachment; filename="attachments.zip"'})


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
