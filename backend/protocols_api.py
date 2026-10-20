"""Local configuration endpoints, not an externally published QA API."""
from fastapi import APIRouter
from pydantic import Field
from src.runtime.specs import StrictSpec
from src.runtime.protocols import ProtocolSpec, protocol_templates
from src.runtime.protocols_v2 import ProtocolSpecV2, language_capabilities
from src.runtime.protocol_store import ProtocolStore

router = APIRouter(prefix='/api/v1/protocols')


class ProtocolFields(StrictSpec):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=2000)
    spec: ProtocolSpec | ProtocolSpecV2


class SaveProtocol(ProtocolFields):
    expected_revision: int = Field(ge=1, strict=True)


class Revision(StrictSpec):
    expected_revision: int = Field(ge=1, strict=True)


@router.get('/templates')
def templates(): return {'items': protocol_templates(), 'capabilities': language_capabilities()}


@router.post('/validate')
def validate(spec: ProtocolSpec | ProtocolSpecV2): return {'valid': True, 'spec': spec.model_dump()}


@router.get('')
def list_protocols(): return {'items': ProtocolStore().list()}


@router.post('', status_code=201)
def create(body: ProtocolFields): return ProtocolStore().save(body.model_dump())


@router.get('/{protocol_id}')
def get(protocol_id: str): return ProtocolStore().get(protocol_id)


@router.put('/{protocol_id}/draft')
def save(protocol_id: str, body: SaveProtocol):
    return ProtocolStore().save(body.model_dump(exclude={'expected_revision'}), protocol_id, body.expected_revision)


@router.post('/{protocol_id}/versions')
def publish(protocol_id: str, body: Revision): return ProtocolStore().publish(protocol_id, body.expected_revision)


@router.get('/{protocol_id}/versions/{version}')
def version(protocol_id: str, version: str): return ProtocolStore().resolve(protocol_id, version=version)


@router.post('/{protocol_id}/archive')
def archive(protocol_id: str, body: Revision): return ProtocolStore().archive(protocol_id, body.expected_revision)
