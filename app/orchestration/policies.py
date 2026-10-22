"""Task classification and routing policies.

classify_task() maps a user message to a workflow type.
Supports: English, Arabic (Modern Standard + Darija), French.
"""


def classify_task(task: str) -> str:
    text = task.lower()

    # ── RESEARCH ──────────────────────────────────────────────────────────────
    if any(x in text for x in (
        "research", "search the web", "sources", "find information",
        "find info", "look up", "google",
        "بحث", "ابحث", "جستجو", "دور على", "9leb", "9alab", "qaleb",
        "cherche", "recherche",
    )):
        return "research"

    # ── CODING ────────────────────────────────────────────────────────────────
    if any(x in text for x in (
        "write code", "coding", "debug", "python", "javascript", "typescript",
        "write script", "write program", "programming", "source code", "function", "class",
        "rest api", "api endpoint", "create api", "backend",
        "frontend", "html", "css", "react", "node", "django", "flask",
        "برمجة", "اكتب كود", "سكريبت", "برمج",
        "kateb code", "dir code", "sawb code", "sawb site", "sawb app",
        "programme", "coder", "développer",
    )):
        return "coding"

    # ── BUSINESS ──────────────────────────────────────────────────────────────
    if any(x in text for x in (
        "business workflow", "business process", "automate", "invoice approval",
        "workflow", "crm", "erp", "pipeline",
        "عمل", "مشروع تجاري", "سير العمل",
        "automatiser", "processus métier",
    )):
        return "business"

    # ── ANALYSIS ──────────────────────────────────────────────────────────────
    if any(x in text for x in (
        "analyze", "analyse", "risks", "compare", "evaluation", "review",
        "breakdown", "summary", "summarize",
        "حلل", "قارن", "تحليل", "مقارنة",
        "hlel", "qaren", "analyser", "résumé",
    )):
        return "analysis"

    # ── WRITING ───────────────────────────────────────────────────────────────
    if any(x in text for x in (
        "write a report", "draft", "email", "document", "rewrite",
        "cover letter", "blog", "essay", "letter",
        "اكتب تقرير", "اكتب رسالة", "اكتب بريد",
        "kteb", "ktab", "writ", "redige", "rédige",
    )):
        return "writing"

    return "general"


def routing_reason(task: str) -> dict:
    workflow = classify_task(task)
    text = task.lower()

    keyword_map = {
        "research": (
            "research", "search the web", "sources", "find information",
            "بحث", "9leb", "9alab", "cherche",
        ),
        "coding": (
            "write code", "coding", "debug", "python", "javascript",
            "script", "code", "sawb site", "sawb app", "dir code", "kateb code",
        ),
        "business": (
            "business workflow", "automate", "workflow", "invoice approval",
        ),
        "analysis": (
            "analyze", "analyse", "risks", "compare", "hlel", "qaren",
        ),
        "writing": (
            "write a report", "draft", "email", "document", "rewrite", "kteb",
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
