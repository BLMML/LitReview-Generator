"""Deterministic, size-bounded aggregation without dropping papers."""

import json
from functools import lru_cache
from itertools import combinations

from apps.common.http import api_error
from apps.llm.client import SYSTEM_RULES
from apps.llm.prompt_loader import load_prompt
from apps.llm.schemas import output_schema

BATCH_REVISION = "analysis-batches-v1"


def input_bound(task, filename, parameters):
    # Match the gateway's whitelist JSON encoding and include its schema/system text.
    parameters = dict(parameters)
    for key in ("allowed_paper_ids", "allowed_evidence_ids"):
        value = parameters[key]
        parameters[key] = json.dumps(
            json.loads(value) if isinstance(value, str) else value, ensure_ascii=False
        )
    prompt = load_prompt(filename, parameters, task_type=task)
    system = SYSTEM_RULES + json.dumps(output_schema(task), ensure_ascii=False)
    return len((system + prompt.text).encode("utf-8")) + 256 + 128


def paper_batches(items, maximum, fits):
    if maximum < 1:
        raise api_error("VALIDATION_ERROR", "主题批大小必须大于零")
    batches, current = [], []
    for item in items:
        candidate = [*current, item]
        if current and (len(candidate) > maximum or not fits(candidate)):
            batches.append(current)
            current = []
        current.append(item)
        if not fits(current):
            raise api_error("VALIDATION_ERROR", "单篇分析摘要超过主题输入上限，不能截断素材后继续")
    if current:
        batches.append(current)
    return batches


def comparison_batches(members, maximum, fits):
    """Cover every unordered pair, including pairs crossing the initial groups."""
    members = list(dict.fromkeys(members))
    if len(members) < 2:
        return []
    if maximum < 2:
        raise api_error("VALIDATION_ERROR", "争议比较每批至少允许两篇论文")
    positions = {value: index for index, value in enumerate(members)}
    pending = set(combinations(range(len(members)), 2))
    batches = []

    @lru_cache(maxsize=2048)
    def allowed(indices):
        return fits([members[i] for i in indices])

    while pending:
        group = list(min(pending))
        if not allowed(tuple(group)):
            raise api_error("VALIDATION_ERROR", "两篇文献的完整比较依据超过输入上限，不能省略文献或截断依据")
        while len(group) < maximum:
            options = []
            for i in range(len(members)):
                if i in group:
                    continue
                gain = sum(tuple(sorted((i, j))) in pending for j in group)
                if gain and allowed(tuple(sorted([*group, i]))):
                    options.append((gain, -i, i))
            if not options:
                break
            group.append(max(options)[2])
        group.sort()
        pending.difference_update(combinations(group, 2))
        batches.append([members[i] for i in group])
    assert set(positions) == {i for b in batches for i in b}
    return batches
