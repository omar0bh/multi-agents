import logging
import dspy

from app.agents.research import ResearchAgent

logger = logging.getLogger(__name__)


class AnalyzeFreeAIProviders(dspy.Signature):
    """You are the Chief AI Architecture Scout.
    Analyze the gathered web data about current FREE AI API providers (0$).
    Your mission:
    1. Identify the top active providers offering generous 100% free API tiers.
    2. For each provider/model, assign its IDEAL POSITION/ROLE in our multi-agent platform:
       - Coding Specialist (best for generating/reviewing complex code)
       - Fast Router / Conversational (best for instant sub-second chat)
       - Long-Context Document Reader (best for huge PDFs/books)
       - Reasoning / Analysis Expert (best for logic, math, planning)
    3. Provide clean bullet points with direct signup URLs so the user can sign in with 1 click using Google.
    4. Keep the output clean, structured, and easy to read on mobile.
    """
    web_findings: str = dspy.InputField(desc="Recent web search results about free AI API tiers")
    user_criteria: str = dspy.InputField(desc="User preferences or specific focus areas")
    scout_report: str = dspy.OutputField(desc="Structured golden list of free providers, models, their ideal roles, and signup links")


class AIScoutAgent:
    """Autonomous scout that discovers free AI models, benchmarks, and recommends role assignments."""

    def __init__(self):
        self.researcher = ResearchAgent()

    def scout(self, query: str = "best free llm api providers for developers with high rate limit") -> dict:
        from app.llm import configure_dspy
        configure_dspy()

        # Step 1: Search recent developer benchmarks and discussions
        sources = []
        try:
            search_query = f"{query} reddit github free tier 2025 2026"
            logger.info("Scouting web for free AI models: %s", search_query)
            sources = self.researcher.search(search_query)
        except Exception as e:
            logger.warning("Web search unavailable or rate-limited (%s), using curated baseline knowledge", e)

        if not sources:
            sources = [
                {
                    "title": "Groq Developer Cloud - Ultra-Fast Free Tier",
                    "url": "https://console.groq.com",
                    "snippet": "Provides ultra-fast inference with free tier limits for models like LLaMA-3.3-70B, GPT-OSS-120B, and Mixtral 8x7B. Perfect for sub-second chat and routing."
                },
                {
                    "title": "Google AI Studio - Gemini Flash Free Tier",
                    "url": "https://aistudio.google.com",
                    "snippet": "Generous free tier offering 15 requests per minute for Gemini 2.5/3.8 Flash with 1M token context window. Best for multimodal vision, huge PDF analysis, and long documents."
                },
                {
                    "title": "GitHub Models - Free AI Prototyping",
                    "url": "https://github.com/marketplace/models",
                    "snippet": "Free access to GPT-4o, GPT-4o-mini, and Llama 3.3 directly with your GitHub account. Ideal for code generation and review."
                },
                {
                    "title": "OpenRouter - Curated Free Tier Models",
                    "url": "https://openrouter.ai/models?max_price=0",
                    "snippet": "Dozens of 100% free models including DeepSeek-R1-Distill, Qwen 2.5 Coder, and Llama 3. Ideal for specialized tasks."
                },
                {
                    "title": "Cloudflare Workers AI - Free Edge LLMs",
                    "url": "https://developers.cloudflare.com/workers-ai",
                    "snippet": "Offers 10,000 free AI neurons per day for Llama, Mistral, and embeddings at the edge."
                }
            ]

        formatted_sources = "\n\n".join(
            f"Title: {s['title']}\nURL: {s['url']}\nSnippet: {s['snippet']}"
            for s in sources
        )

        # Step 2: DSPy Analysis & Role Allocation
        scout_cot = dspy.ChainOfThought(AnalyzeFreeAIProviders)
        result = scout_cot(
            web_findings=formatted_sources or "No web results found; fallback to known industry free providers.",
            user_criteria=query,
        )

        return {
            "ok": True,
            "query": query,
            "report": result.scout_report,
            "sources_count": len(sources),
            "sources": sources[:5],
        }
