from copy import deepcopy
import json

import pytest

from src.agents.planner import parse_plan, planner_fn
from src.agents.retriever import configured_top_k, retriever_fn
from src.agents.answer_validator import parse_verification, build_prompt
from src.runtime.system import resolve_system
from src.graph import default_nodes


def sq(identifier='sq_1', question='Where is Cedar Institute?', depends_on=None):
    return {'id': identifier, 'question': question, 'keywords': [], 'depends_on': depends_on or []}


def state():
    return {'question': 'Where is Cedar Institute?', 'sub_questions': [sq()],
            'evidence_chunks': [{'sq_id': 'sq_1', 'sufficiency': 'unknown', 'insufficiency_note': '',
               'chunks': [{'source_title': 'Cedar', 'source_paragraph': 0,
                           'source_id': 'source', 'text': 'Cedar Institute is in Northbridge.', 'relevance_score': 1.0}]}]}


def output(**changes):
    return {'answer': 'Northbridge', 'verified': True, 'failure_type': 'none',
            'supporting_evidence': [{'sq_id': 'sq_1', 'source_title': 'Cedar', 'source_paragraph': 0}], **changes}


@pytest.mark.parametrize('questions', [
    [sq(), sq()], [sq(depends_on=['missing'])], [sq(depends_on=['sq_1'])],
    [sq(depends_on=['sq_2']), sq('sq_2', depends_on=['sq_1'])],
    [sq(f'sq_{i}') for i in range(5)],
])
def test_invalid_plans_fall_back_to_original_question(questions):
    parsed = parse_plan(json.dumps({'sub_questions': questions}), 'Original question?')
    assert parsed['sub_questions'] == [sq(question='Original question?')]


def test_single_question_and_dependency_chain_are_retained():
    for questions in ([sq()], [sq(), sq('sq_2', depends_on=['sq_1'])]):
        assert parse_plan(json.dumps({'sub_questions': questions}), 'Q')['sub_questions'] == questions
    prompt = resolve_system().agents['planner_v0'].config['model_config']['system_prompt']
    assert '1-4' in prompt and '禁止' in prompt and '唯一子问题' in prompt


@pytest.mark.parametrize('value', [True, False, 0, -1, 2.5, '3', None])
def test_top_k_rejects_non_positive_integers(value):
    with pytest.raises(ValueError):
        configured_top_k({'retrieval_config': {'top_k': value}})


def test_top_k_flows_through_production_node_construction():
    class Retriever:
        def search(self, query, top_k=3):
            return [{'text': f'Candidate {i}', 'source_title': 'Cedar', 'relevance_score': 0.2}
                    for i in range(top_k)]
    configs = deepcopy(resolve_system().snapshot()['agents'])
    configs['retriever_v0']['retrieval_config']['top_k'] = 1
    nodes = default_nodes(agent_configs=configs, retriever=Retriever())
    result = nodes['retriever_v0'](state())
    assert len(result['evidence_chunks'][0]['chunks']) == 1
    assert result['evidence_chunks'][0]['sufficiency'] == 'unknown'
    configs['retriever_v0']['retrieval_config']['top_k'] = 5
    assert len(default_nodes(agent_configs=configs, retriever=Retriever())['retriever_v0'](state())['evidence_chunks'][0]['chunks']) == 5


def test_zero_index_and_original_text_are_backfilled():
    result = parse_verification(json.dumps(output()), state())
    assert result['verified']
    evidence = result['supporting_evidence'][0]
    assert evidence['source_paragraph'] == 0 and evidence['source_id'] == 'source'
    assert evidence['text'] == 'Cedar Institute is in Northbridge.'


@pytest.mark.parametrize('citation', [
    {'source_title': 'Wrong', 'source_paragraph': 0},
    {'source_title': 'Cedar', 'source_paragraph': 999},
    {'source_title': 'Cedar', 'source_paragraph': True},
    {'source_title': 'Cedar', 'source_paragraph': -1},
    {'source_title': 'Cedar', 'text': 'Invented relationship'},
    {'text': 'Cedar Institute is in an invented city.'},
    {'source_title': 'Cedar', 'source_id': 'wrong', 'source_paragraph': 0},
])
def test_forged_or_unattributed_citations_do_not_pass(citation):
    result = parse_verification(json.dumps(output(supporting_evidence=[{'sq_id': 'sq_1', **citation}])), state())
    assert not result['verified'] and not result['supporting_evidence']
    assert result['failure_type'] == 'insufficient'


def test_legacy_text_match_must_be_unique_within_source():
    current = state()
    current['evidence_chunks'][0]['chunks'].append({**current['evidence_chunks'][0]['chunks'][0], 'source_paragraph': 1})
    legacy = {'sq_id': 'sq_1', 'source_title': 'Cedar', 'text': 'Cedar Institute is in Northbridge.'}
    assert not parse_verification(json.dumps(output(supporting_evidence=[legacy])), current)['verified']
    # Explicit coordinates disambiguate equal sentences.
    assert parse_verification(json.dumps(output()), current)['verified']


def test_answer_needs_both_nonempty_text_and_real_citations():
    assert not parse_verification(json.dumps(output(answer=' ')), state())['verified']
    assert not parse_verification(json.dumps(output(supporting_evidence=[])), state())['verified']


def test_irrelevant_plan_does_not_mechanically_override_supported_answer():
    current = state()
    current['sub_questions'].append(sq('sq_2', 'Who is the founder spouse?'))
    current['evidence_chunks'].append({'sq_id': 'sq_2', 'chunks': [], 'sufficiency': 'insufficient', 'insufficiency_note': 'Missing'})
    current['retrieval_supplement_request'] = {'target_sq_id': 'sq_2', 'additional_keywords': [], 'reason': 'old'}
    result = parse_verification(json.dumps(output(insufficient_sq_ids=['sq_2'])), current)
    assert result['verified'] and result['insufficient_sq_ids'] == []
    assert result['retrieval_supplement_request'] is None
    prompt = build_prompt(current, 'instructions')
    assert '无关子问题' in prompt and '原始问题' in prompt and '同名实体' in prompt


def test_missing_necessary_fact_remains_unverified_with_bounded_retry_target():
    result = parse_verification(json.dumps(output(verified=False, failure_type='insufficient',
        supporting_evidence=[], retrieval_supplement_request={'target_sq_id': 'missing'})), state())
    assert not result['verified']
    assert result['retrieval_supplement_request']['target_sq_id'] == 'sq_1'


def test_replanning_clears_stale_retrieval_request():
    class LLM:
        def invoke(self, prompt):
            from langchain_core.messages import AIMessage
            return AIMessage(content=json.dumps({'sub_questions': [sq()]}))
    current = {**state(), 'conflict_flag': True, 'retrieval_supplement_request': {'target_sq_id': 'old'}}
    result = planner_fn(current, llm=LLM())
    assert result['retrieval_supplement_request'] is None and result['plan_retry_count'] == 1


def test_null_optional_citation_fields_do_not_discard_valid_coordinates():
    payload = output(supporting_evidence=[{'sq_id': 'sq_1', 'source_title': 'Cedar',
                        'source_paragraph': 0, 'chunk_id': None, 'source_id': None}])
    assert parse_verification(json.dumps(payload), state())['verified']


def test_legacy_text_only_citation_requires_global_unique_exact_match():
    current = state()
    payload = output(supporting_evidence=[{'sq_id': 'sq_1', 'text': 'Cedar Institute is in Northbridge.'}])
    parsed = parse_verification(json.dumps(payload), current)
    assert parsed['verified'] and parsed['supporting_evidence'][0]['source_paragraph'] == 0
    current['evidence_chunks'][0]['chunks'].append({**current['evidence_chunks'][0]['chunks'][0], 'source_title': 'Other'})
    assert not parse_verification(json.dumps(payload), current)['verified']


def test_contradictory_conflict_fields_cannot_pass():
    result = parse_verification(json.dumps(output(conflict_flag=True)), state())
    assert not result['verified'] and result['failure_type'] == 'conflict'


def test_bridge_passage_is_retrieved_even_when_second_question_repeats_first_hop():
    from src.agents.retriever import build_evidence_groups
    from src.knowledge.bm25_retriever import build_retriever
    questions = [
        {**sq(question='Who portrayed Corliss Archer in the film Kiss and Tell?'), 'keywords': ['Kiss and Tell', 'Corliss Archer', 'cast', 'portrayed by']},
        {**sq('sq_2', 'What government position did the woman who portrayed Corliss Archer in the film Kiss and Tell hold?', ['sq_1']),
         'keywords': ['Corliss Archer actress', 'Kiss and Tell', 'government position', 'held']},
    ]
    groups = build_evidence_groups(questions, build_retriever())
    assert any('Chief of Protocol' in chunk['text'] for chunk in groups[1]['chunks'])
    assert len(groups[1]['chunks']) <= 3


def test_empty_validator_responses_still_obey_protocol_retry_limit():
    from src.graph import build_graph
    from src.knowledge.bm25_retriever import build_retriever
    from src.router import decide_route
    from src.agents.planner import planner_fn
    from src.agents.answer_validator import answer_validator_fn
    from functools import partial

    class LLM:
        def __init__(self, text):
            self.text = text

        def invoke(self, prompt):
            from langchain_core.messages import AIMessage
            return AIMessage(content=self.text)

    workflow = resolve_system().workflow
    nodes = {
        'planner_v0': partial(planner_fn, llm=LLM(json.dumps({'sub_questions': [sq()]}))),
        'retriever_v0': partial(retriever_fn, retriever=build_retriever()),
        'answer_validator_v0': partial(answer_validator_fn, llm=LLM('')),
    }
    result = build_graph(nodes=nodes, workflow=workflow).invoke(
        {'question': 'Q'}, config={'configurable': {'thread_id': 'empty-validator-limit'}})
    assert not result['verified']
    assert result['retrieval_retry_count'] == workflow.max_cycles['retrieval_retry_count']
    assert result['validation_retry_count'] == workflow.max_cycles['retrieval_retry_count'] + 1


def test_malformed_planner_response_falls_back_without_crashing():
    class LLM:
        def invoke(self, prompt):
            from langchain_core.messages import AIMessage
            return AIMessage(content='{"sub_questions": [')
    assert planner_fn({'question': 'Original?'}, llm=LLM())['sub_questions'] == [sq(question='Original?')]
