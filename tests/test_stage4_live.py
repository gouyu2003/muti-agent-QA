"""Explicit real-model checks for original-question grounding and rejection."""
from __future__ import annotations

import json
import os

import pytest
from dotenv import load_dotenv

from src.agents.answer_validator import answer_validator_fn, default_llm
from src.llm_usage import LLMUsageCollector

pytestmark = pytest.mark.integration


def env_ready():
    load_dotenv()
    return bool(os.getenv('DEEPSEEK_API_KEY') and os.getenv('DEEPSEEK_BASE_URL'))


@pytest.mark.skipif(not env_ready(), reason='Real-model connection is not configured')
def test_real_validator_irrelevant_plan_missing_hop_homonyms_and_conflict(tmp_path):
    cases = [
        ('irrelevant_plan', 'Which city hosts the institute founded by Mara Vale?',
         [('People', 'Mara Vale founded Cedar Institute.'), ('Institute', 'Cedar Institute is located in Northbridge.')], True, 'Northbridge'),
        ('missing_hop', 'Which city hosts the institute founded by Mara Vale?',
         [('People', 'Mara Vale founded Cedar Institute.')], False, None),
        ('homonyms', 'Which city hosts the institute founded by marine biologist Mara Vale?',
         [('Writer', 'Mara Vale, a novelist, founded Orchard Institute in Eastport.'),
          ('Biologist', 'Marine biologist Mara Vale studies coral reefs. No institutional affiliation is given.')], False, None),
        ('conflict', 'In what year did Cedar Institute open?',
         [('Record A', 'Cedar Institute opened in 2008.'), ('Record B', 'Cedar Institute opened in 2010.')], False, None),
    ]
    reports = []
    for name, question, facts, expected_verified, expected_answer in cases:
        state = {'question': question,
                 'sub_questions': [{'id': 'sq_1', 'question': question, 'keywords': [], 'depends_on': []},
                                   {'id': 'sq_2', 'question': "Who is Mara Vale's spouse?", 'keywords': [], 'depends_on': []}],
                 'evidence_chunks': [
                     {'sq_id': 'sq_1', 'sufficiency': 'unknown', 'insufficiency_note': '',
                      'chunks': [{'source_title': title, 'source_paragraph': 0, 'text': text, 'relevance_score': 1.0}
                                 for title, text in facts]},
                     {'sq_id': 'sq_2', 'sufficiency': 'insufficient', 'insufficiency_note': 'No spouse information', 'chunks': []}]}
        usage = LLMUsageCollector()
        class CapturingLLM:
            def __init__(self):
                self.chat = default_llm()
                self.responses = []

            def invoke(self, prompt):
                response = self.chat.invoke(prompt)
                self.responses.append(response.content)
                return response

        chat = CapturingLLM()
        result = answer_validator_fn(state, llm=chat, usage_collector=usage)
        reports.append({'case': name, 'state': state, 'result': result,
                        'public_responses': chat.responses, 'usage': usage.snapshot()})
        (tmp_path / 'stage4-validator-live.json').write_text(json.dumps(reports, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        assert result['verified'] is expected_verified, (name, result)
        if expected_answer:
            assert expected_answer.lower() in result['answer'].lower()
            assert result['supporting_evidence']
