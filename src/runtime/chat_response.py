"""Chat-only generation and deterministic assembly; evidence verdicts stay intact."""
import json

from pydantic import BaseModel, ConfigDict, Field

from src.agents.structured_output import invoke_json
from src.runtime.chat_messages import to_llm_messages


class GeneralResponseError(ValueError):
    pass


class GeneralResponse(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    answer: str = Field(min_length=1)


def general_response(chat, question, knowledge, collector):
    from src.llm import build_chat_model
    config = chat['config']
    prompt = config['general_instructions'] + '\n输入数据：\n' + json.dumps({
        'system': config['system_scope'], 'messages': to_llm_messages(chat['history']),
        'general_question': question, 'knowledge_result': knowledge,
    }, ensure_ascii=False)
    usage = collector.for_agent('chat_general')
    data, diagnostics = invoke_json(build_chat_model({'model_config': config['general_model_config']}),
        prompt, 1, usage, validate=GeneralResponse.model_validate,
        max_tokens=config['general_model_config']['max_tokens'])
    usage.check_cancelled()
    if data is None:
        raise GeneralResponseError('通用回答生成失败，请重新发送。已保存的知识库结果可在运行详情中查看。')
    return GeneralResponse.model_validate(data).answer, diagnostics


def knowledge_part(state):
    answer = (state.get('answer') or '').strip()
    if state.get('verified') is True and answer:
        return {'kind': 'knowledge', 'content': answer}
    if state.get('failure_type') == 'output_error':
        raise GeneralResponseError('知识库回答输出无效，请重新发送或查看运行详情。')
    if state.get('failure_type') == 'conflict' or state.get('conflict_flag'):
        content = '当前知识源中的相关资料存在冲突，暂时无法确认答案。请补充权威资料或进一步明确问题。'
    else:
        content = '根据当前绑定的知识源，没有找到足以回答该问题的依据。请补充相关资料或进一步明确问题。'
    return {'kind': 'knowledge', 'content': content}


def assemble_parts(parts):
    if len(parts) == 1:
        return parts[0]['content']
    titles = {'knowledge': '知识库部分', 'general': '通用部分'}
    return '\n\n'.join(f"### {titles[part['kind']]}\n\n{part['content']}" for part in parts)
