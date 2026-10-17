"""HotpotQA v1 metric semantics, including yes/no/noanswer handling.

Reference: https://github.com/hotpotqa/hotpot/blob/master/hotpot_evaluate_v1.py
The caller supplies gold labels only after Runtime has finished.
"""
from __future__ import annotations

from collections import Counter
import re
import string


def normalize_answer(text: str) -> str:
    text = ''.join(char for char in text.lower() if char not in string.punctuation)
    return ' '.join(re.sub(r'\b(a|an|the)\b', ' ', text).split())


def answer_scores(prediction: str, gold: str) -> dict[str, float]:
    predicted, expected = normalize_answer(prediction), normalize_answer(gold)
    result = {'em': float(predicted == expected), 'f1': 0.0}
    if (predicted in {'yes', 'no', 'noanswer'} or expected in {'yes', 'no', 'noanswer'}) and predicted != expected:
        return result
    common = sum((Counter(predicted.split()) & Counter(expected.split())).values())
    if common:
        precision = common / len(predicted.split())
        recall = common / len(expected.split())
        result['f1'] = 2 * precision * recall / (precision + recall)
    return result


def supporting_scores(prediction: list, gold: list) -> dict[str, float]:
    predicted, expected = set(map(tuple, prediction)), set(map(tuple, gold))
    common = len(predicted & expected)
    precision = common / len(predicted) if predicted else 0.0
    recall = common / len(expected) if expected else 0.0
    return {'sp_em': float(predicted == expected),
            'sp_f1': 2 * precision * recall / (precision + recall) if precision + recall else 0.0}


def score(prediction: str, supporting: list, gold: dict) -> dict[str, float]:
    return {**answer_scores(prediction, gold['answer']),
            **supporting_scores(supporting, gold['supporting_facts'])}
