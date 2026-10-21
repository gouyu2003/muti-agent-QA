"""Local workspace system management, deliberately without config JSON editing."""
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from src.runtime.instances import AgentInstance, ExecutionMode, templates
from src.runtime.protocols import ProtocolRef

router = APIRouter(prefix='/api/v1/systems')


class DraftFields(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=2000)
    workflow_id: str | None = Field(default=None, min_length=1)
    source_id: str | None = Field(default=None, min_length=1)
    source_version: int | None = Field(default=None, ge=1, strict=True)
    agents: list[AgentInstance] | None = Field(default=None, min_length=1, max_length=5)
    execution_mode: ExecutionMode | None = None
    protocol_ref: ProtocolRef | None = None
    protocol_bindings: dict[str, list[str]] = Field(default_factory=dict)


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
    return request.app.state.system_store.create(body.model_dump(by_alias=True))


@router.get('/{system_id}')
def get_system(system_id: str, request: Request):
    return request.app.state.system_store.get(system_id)


@router.delete('/{system_id}')
def delete_system(system_id: str, request: Request, expected_revision: int = Query(ge=1)):
    request.app.state.system_store.delete(system_id, expected_revision)
    return {'deleted': True, 'system_id': system_id}


@router.put('/{system_id}/draft')
def save_draft(system_id: str, body: SaveDraft, request: Request):
    return request.app.state.system_store.save(system_id, body.expected_revision,
                                             body.model_dump(by_alias=True, exclude={'expected_revision'}))


@router.get('/{system_id}/versions')
def list_versions(system_id: str, request: Request):
    return {'items': request.app.state.system_store.get(system_id)['versions']}


@router.post('/{system_id}/versions')
def publish_system(system_id: str, body: PublishRequest, request: Request):
    return request.app.state.system_store.publish(system_id, body.expected_revision)


@router.get('/{system_id}/versions/{version}')
def get_version(system_id: str, version: str, request: Request):
    return request.app.state.system_store.version(system_id, version)


templates_router = APIRouter()


@templates_router.get('/api/v1/agent-templates')
def get_agent_templates():
    return {'items': templates()}
