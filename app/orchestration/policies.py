def classify_task(task: str) -> str:
    text = task.lower()

    if any(x in text for x in (
        "research", "search the web", "sources",
        "find information", "بحث",
    )):
        return "research"

    if any(x in text for x in (
        "write code", "coding", "debug", "python",
        "javascript", "program", "script", "code"
    )):
        return "coding"

    if any(x in text for x in (
        "business workflow", "business process",
        "automate", "invoice approval", "workflow"
    )):
        return "business"

    if any(x in text for x in (
        "analyze", "analyse", "risks", "compare"
    )):
        return "analysis"

    if any(x in text for x in (
        "write a report", "draft", "email",
        "document", "rewrite"
    )):
        return "writing"

    return "general"


def routing_reason(task: str) -> dict:
    workflow = classify_task(task)
    text = task.lower()

    keyword_map = {
        "research": (
            "research", "search the web", "sources",
            "find information", "بحث",
        ),
        "coding": (
            "write code", "coding", "debug", "python",
            "javascript", "program", "script", "code",
        ),
        "business": (
            "business workflow", "business process",
            "automate", "invoice approval", "workflow",
        ),
        "analysis": (
            "analyze", "analyse", "risks", "compare",
        ),
        "writing": (
            "write a report", "draft", "email",
            "document", "rewrite",
        ),
    }

    matched = "default"
    if workflow != "general":
        for keyword in keyword_map.get(workflow, ()):
            if keyword in text:
                matched = keyword
                break

    return {
        "method": "keyword",
        "matched": matched,
        "workflow": workflow,
    }
