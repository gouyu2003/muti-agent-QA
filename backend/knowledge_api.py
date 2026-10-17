"""Bounded multipart imports and previews of local user knowledge."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from src.knowledge.corpus import MAX_FILE_BYTES, MAX_FILES, ImportFailure
from src.knowledge.store import KnowledgeStore

router = APIRouter(prefix="/api/v1/knowledge")


def scope() -> str:
    # Legacy store signatures retain the first argument for CLI compatibility;
    # knowledge belongs to this local workspace, independent of any QA system.
    return 'local'


def get_store(request: Request) -> KnowledgeStore:
    return request.app.state.knowledge_store


def import_result(result: dict):
    if result["status"] == "failed":
        version = result["versions"][0]
        return JSONResponse(status_code=422, content={"detail": {
            "source_id": result["source_id"], "version": version["version"],
            **version["error"],
        }})
    return result


def upload_files(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    # Oversize reads are capped; the importer records a failed version.
    if len(files) > MAX_FILES:
        return [(file.filename or "", b"") for file in files[:MAX_FILES + 1]]
    return [(file.filename or "", file.file.read(MAX_FILE_BYTES + 1)) for file in files]


@router.post("/sources")
def create_source(request: Request, name: Annotated[str, Form()], files: Annotated[list[UploadFile], File()]):
    try:
        return import_result(get_store(request).import_files(scope(), name, upload_files(files)))
    except ImportFailure as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc


@router.get("/sources")
def list_sources(request: Request):
    return {"items": get_store(request).list_sources(scope())}


@router.get("/sources/{source_id}")
def source_detail(source_id: str, request: Request):
    try:
        return get_store(request).detail(scope(), source_id)
    except ImportFailure as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code, "message": str(exc)}) from exc


@router.post("/sources/{source_id}/versions")
def add_version(source_id: str, request: Request, files: Annotated[list[UploadFile], File()]):
    try:
        return import_result(get_store(request).import_files(scope(), None, upload_files(files), source_id))
    except ImportFailure as exc:
        raise HTTPException(status_code=404 if exc.code == "source_not_found" else 422,
                            detail={"code": exc.code, "message": str(exc)}) from exc


@router.get("/sources/{source_id}/versions/{version}/chunks")
def source_chunks(source_id: str, version: int, request: Request,
                  limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    try:
        return get_store(request).chunks(scope(), source_id, version, limit, offset)
    except ImportFailure as exc:
        raise HTTPException(status_code=404 if exc.code in {"source_not_found", "version_not_ready"} else 422,
                            detail={"code": exc.code, "message": str(exc)}) from exc
