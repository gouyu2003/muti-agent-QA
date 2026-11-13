"""Persistent conversations; execution delegates to the existing Run service."""
from fastapi import APIRouter, Request, Response
from pydantic import Field, model_validator

from src.runtime.specs import StrictSpec

router = APIRouter(prefix='/api/v1/conversations', tags=['conversations'])


class CreateConversation(StrictSpec):
    client_request_id: str = Field(min_length=1, max_length=100)
    system_id: str = Field(min_length=1)
    system_version: str | None = Field(default=None, pattern=r'^v[1-9][0-9]*$')
    draft_revision: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode='after')
    def version_selection(self):
        if (self.system_version is None) == (self.draft_revision is None):
            raise ValueError('请选择一个发布版本或已保存草稿修订。')
        return self


class SendMessage(StrictSpec):
    client_request_id: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=2000)

    @model_validator(mode='after')
    def nonempty(self):
        self.question = self.question.strip()
        if not self.question:
            raise ValueError('问题不能为空。')
        return self


class RenameConversation(StrictSpec):
    title: str = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def nonempty(self):
        self.title = self.title.strip()
        if not self.title:
            raise ValueError('标题不能为空。')
        return self


@router.post('', status_code=201)
def create(body: CreateConversation, request: Request):
    return request.app.state.chat.create(body.model_dump(exclude={'client_request_id'}), body.client_request_id)


@router.get('')
def conversations(request: Request):
    return {'items': request.app.state.chat.list()}


@router.get('/{conversation_id}')
def conversation(conversation_id: str, request: Request):
    return request.app.state.chat.get(conversation_id)


@router.patch('/{conversation_id}')
def rename(conversation_id: str, body: RenameConversation, request: Request):
    return request.app.state.chat.rename(conversation_id, body.title)


@router.delete('/{conversation_id}', status_code=204)
def delete(conversation_id: str, request: Request):
    request.app.state.chat.delete(conversation_id)
    return Response(status_code=204)


@router.get('/{conversation_id}/messages')
def messages(conversation_id: str, request: Request):
    request.app.state.chat.get(conversation_id)
    return {'items': request.app.state.chat.messages(conversation_id)}


@router.post('/{conversation_id}/messages', status_code=202)
def send(conversation_id: str, body: SendMessage, request: Request):
    return request.app.state.chat.send(conversation_id, body.question, body.client_request_id)
