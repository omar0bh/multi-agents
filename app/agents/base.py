import logging
import os
import dspy

logger = logging.getLogger(__name__)

class GeneralTask(dspy.Signature):
    """You are the 24/7 Cloud AI Employee and Personal Autonomous Assistant for your owner.

    CRITICAL OWNER ENVIRONMENT CONTEXT:
    - Host Machine: Windows 11 Laptop
    - Primary Drive: C:
    - User Profile: C:\\Users\\DELL LATITUDE
    - Canonical Folders:
      • Desktop: C:\\Users\\DELL LATITUDE\\Desktop
      • Documents: C:\\Users\\DELL LATITUDE\\Documents
      • Downloads: C:\\Users\\DELL LATITUDE\\Downloads
      • Pictures: C:\\Users\\DELL LATITUDE\\Pictures
      • Videos: C:\\Users\\DELL LATITUDE\\Videos
      • Music: C:\\Users\\DELL LATITUDE\\Music

    STRICT OPERATIONAL & PATH RULES:
    1. NEVER output Linux/Unix paths like ~/Desktop, ./Desktop, or /home/ubuntu when referring to files, folders, or operations on the owner's PC.
    2. ALL paths on the owner's PC ALWAYS start with C:\\Users\\DELL LATITUDE\\... (e.g. C:\\Users\\DELL LATITUDE\\Desktop\\my_folder).
    3. NEVER hallucinate or claim that you completed a file operation (such as copying a file, creating a folder, or writing to disk) unless it was actually executed by a real system tool.
    4. Provide honest, direct, and actionable answers in the owner's preferred language (Darija, Arabic, English, or French).
    """
    task: str = dspy.InputField(desc="The user instruction, question, or request")
    answer: str = dspy.OutputField(desc="Direct, accurate response using real Windows paths starting with C:\\Users\\DELL LATITUDE\\")

class GeneralAgent:
    def run(self, task: str) -> dict:
        from app.llm import configure_dspy
        from app.config import GROQ_API_KEY, LLM_MODEL
        configure_dspy()

        # Primary: DSPy ChainOfThought
        try:
            result = dspy.ChainOfThought(GeneralTask)(task=task)
            return {"answer": result.answer}
        except Exception as primary_exc:
            err_str = str(primary_exc).lower()
            # Groq json_validate_failed or JSON-mode errors — fallback to raw completion
            if "json_validate_failed" in err_str or "failed to generate json" in err_str or "json" in err_str:
                logger.warning("DSPy JSON-mode failed (%s), falling back to raw Groq call.", primary_exc)
                return self._raw_groq_fallback(task)
            raise

    def _raw_groq_fallback(self, task: str) -> dict:
        """Direct Groq API call without JSON mode — works for long/complex messages."""
        import httpx
        from app.config import GROQ_API_KEY, LLM_MODEL

        groq_key = GROQ_API_KEY
        if not groq_key:
            return {"answer": "عذراً، حدث خطأ في نظام الذكاء الاصطناعي. يرجى المحاولة مجدداً."}

        # Extract actual model name (strip 'groq/' prefix)
        model = LLM_MODEL.replace("groq/", "") if LLM_MODEL.startswith("groq/") else LLM_MODEL

        system_prompt = (
            "You are a helpful, direct AI assistant for a Moroccan developer. "
            "Answer in the same language the user uses (Darija, Arabic, French, or English). "
            "For PC-related questions about Windows paths, use C:\\\\Users\\\\DELL LATITUDE\\\\ as the base."
        )

        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {groq_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": task},
                        ],
                        "temperature": 0.3,
                        "max_tokens": 3000,
                        # NO response_format here — that's what caused json_validate_failed
                    },
                )
                if resp.status_code == 200:
                    answer = resp.json()["choices"][0]["message"]["content"]
                    return {"answer": answer}
                else:
                    logger.error("Groq fallback failed: %s %s", resp.status_code, resp.text[:200])
                    return {"answer": f"عذراً، حدث خطأ مؤقت في النظام. يرجى المحاولة مجدداً. (Code: {resp.status_code})"}
        except Exception as exc:
            logger.error("Groq raw fallback exception: %s", exc)
            return {"answer": "عذراً، حدث خطأ في الاتصال بالذكاء الاصطناعي. يرجى المحاولة بعد قليل."}
