"""Local workspace system management, deliberately without config JSON editing."""
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix='/api/v1/systems')


class DraftFields(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=2000)
    workflow_id: str | None = Field(default=None, min_length=1)
    source_id: str | None = Field(default=None, min_length=1)
    source_version: int | None = Field(default=None, ge=1, strict=True)


class SaveDraft(DraftFields):
    expected_revision: int = Field(ge=1, strict=True)


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1, strict=True)


@router.get('')
def list_systems(request: Request):
    return {'items': request.app.state.system_store.list()}


@router.post('', status_code=201)
def create_system(body: DraftFields, request: Request):
    return request.app.state.system_store.create(body.model_dump())


@router.get('/{system_id}')
def get_system(system_id: str, request: Request):
    return request.app.state.system_store.get(system_id)


@router.put('/{system_id}/draft')
def save_draft(system_id: str, body: SaveDraft, request: Request):
    return request.app.state.system_store.save(system_id, body.expected_revision,
                                             body.model_dump(exclude={'expected_revision'}))


@router.get('/{system_id}/versions')
def list_versions(system_id: str, request: Request):
    return {'items': request.app.state.system_store.get(system_id)['versions']}


@router.post('/{system_id}/versions')
def publish_system(system_id: str, body: PublishRequest, request: Request):
    return request.app.state.system_store.publish(system_id, body.expected_revision)


@router.get('/{system_id}/versions/{version}')
def get_version(system_id: str, version: str, request: Request):
    return request.app.state.system_store.version(system_id, version)
