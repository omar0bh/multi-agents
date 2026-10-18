import dspy
from ddgs import DDGS
from app.config import SEARCH_MAX_RESULTS

class ResearchSummary(dspy.Signature):
    """Summarize the provided search results without inventing sources."""
    topic: str = dspy.InputField()
    results: str = dspy.InputField()
    summary: str = dspy.OutputField()
    findings: str = dspy.OutputField()
    limitations: str = dspy.OutputField()

class ResearchAgent:
    def search(self, topic: str) -> list:
        try:
            with DDGS() as search_client:
                items = list(
                    search_client.text(
                        topic,
                        max_results=SEARCH_MAX_RESULTS,
                    )
                )

            return [
                {
                    "title": item.get("title", ""),
                    "url": item.get("href", item.get("url", "")),
                    "snippet": item.get("body", ""),
                }
                for item in items
            ]
        except Exception as exc:
            raise RuntimeError(
                f"Web search failed: {exc}"
            ) from exc

    def run(self, topic: str) -> dict:
        from app.llm import configure_dspy
        configure_dspy()

        sources = self.search(topic)

        formatted = "\n\n".join(
            f"Title: {s['title']}\n"
            f"URL: {s['url']}\n"
            f"Snippet: {s['snippet']}"
            for s in sources
        )

        result = dspy.ChainOfThought(ResearchSummary)(
            topic=topic,
            results=formatted or "No search results found.",
        )

        return {
            "topic": topic,
            "summary": result.summary,
            "findings": result.findings,
            "limitations": result.limitations,
            "sources": sources,
        }
