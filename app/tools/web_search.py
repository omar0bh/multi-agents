from app.agents.research import ResearchAgent

def search_web(query: str) -> list:
    return ResearchAgent().search(query)
