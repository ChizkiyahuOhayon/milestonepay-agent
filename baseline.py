"""Non-AI line selection with the same money validation as the AI path."""
import re

from extract import LABELLED, TOTAL, validate_extraction

ACTION = re.compile(
    r"\b(?:research|write|design|build|create|prepare|deliver|record|edit|translate|draft)\b"
    r"|调研|撰写|设计|绘制|制作|提交|交付|录制|完成|翻译", re.I)


def extract_rules(brief):
    if not isinstance(brief, str) or not 10 <= len(brief) <= 6000:
        raise ValueError("Enter a brief of 10-6000 characters")
    lines = brief.splitlines()
    selected = [i for i, line in enumerate(lines) if LABELLED.search(line)]
    branch = "explicit_labels" if selected else "action_words"
    if not selected:
        selected = [i for i, line in enumerate(lines)
                    if not TOTAL.search(line) and ACTION.search(line)]
    raw = {"milestones": [{"title": lines[i].strip()[:127], "line": i}
                           for i in selected[:8]]}
    result = validate_extraction(brief, raw)
    for milestone in result["milestones"]:
        milestone["field_sources"]["title"] = "parser"
    result["provenance"].update(engine="deterministic_rules_v1", model=None,
                                selection_branch=branch)
    return result
