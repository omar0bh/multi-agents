import logging
from pathlib import Path

import dspy

from app.tools.gemini_vision import analyze_image_with_gemini

import re

logger = logging.getLogger(__name__)


def clean_mobile_math(text: str) -> str:
    """Format mathematical expressions and clean raw LaTeX so it looks clean on mobile chats."""
    if not text:
        return ""

    # Replace matrix blocks \begin{pmatrix} ... \end{pmatrix}
    def matrix_repl(m):
        content = m.group(1).strip()
        rows = [r.strip() for r in re.split(r"\\\\", content)]
        formatted_rows = []
        for row in rows:
            cols = [c.strip() for c in row.split("&")]
            formatted_rows.append("[" + "  ".join(cols) + "]")
        return " " + " ".join(formatted_rows) + " "

    cleaned = re.sub(r"\\begin\{(?:p|b|v|V)?matrix\}(.*?)\\end\{(?:p|b|v|V)?matrix\}", matrix_repl, text, flags=re.DOTALL)
    cleaned = cleaned.replace(r"\displaystyle", "")
    cleaned = re.sub(r"\\text\{([^}]+)\}", r"\1", cleaned)
    cleaned = re.sub(r"\\frac\{([^}]+)\}\{([^}]+)\}", r"(\1 / \2)", cleaned)
    cleaned = cleaned.replace(r"\times", "×").replace(r"\cdot", "·").replace(r"\pm", "±")

    # Strip LaTeX inline markers \( \) and \[ \]
    cleaned = re.sub(r"\\\[(.*?)\\\]", r"\1", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"\\\((.*?)\\\)", r"\1", cleaned)
    cleaned = re.sub(r"\$([^\$]+)\$", r"\1", cleaned)

    # Clean double spaces
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return cleaned.strip()


class CrossVerifyVisualAnalysis(dspy.Signature):
    """You are the Master Verification Agent.
    Gemini Vision extracted the questions and proposed answers from an image.
    Your mission:
    1. Verify all calculations, logic, and answers for 100% accuracy.
    2. Output ONLY the clear, direct, and concise final answers.
    3. If multiple-choice/QCM: Format clearly by question number and correct choice letter (e.g. Q41: Choice A (-2)).
    4. Do NOT include filler explanations, meta-commentary, or raw LaTeX commands (\\begin{pmatrix}, \\displaystyle).
    """
    user_question: str = dspy.InputField(desc="The user request")
    gemini_analysis: str = dspy.InputField(desc="Raw image extraction and initial answer from Gemini")
    verification_notes: str = dspy.OutputField(desc="Internal brief verification check")
    final_verified_answer: str = dspy.OutputField(desc="The concise, direct, finalized answers for the user")


class DualModelVisionAgent:
    """Combines Gemini (Perception & Vision) + Groq (Logic Review & Fact-Checking)."""

    def process_and_verify(
        self,
        image_bytes_or_path: bytes | str | Path,
        user_prompt: str = "Please solve or analyze this image.",
    ) -> dict:
        from app.llm import configure_dspy
        configure_dspy()

        # Step 1: Gemini Vision Perception
        logger.info("Step 1: Sending image to Gemini Vision...")
        gemini_res = analyze_image_with_gemini(
            image_bytes_or_path,
            prompt=user_prompt,
        )

        if not gemini_res.get("ok"):
            return {
                "ok": False,
                "error": gemini_res.get("error", "Gemini vision failed."),
            }

        gemini_analysis = gemini_res.get("analysis", "")

        # Step 2: Groq Fact-Checking & Cross-Validation
        logger.info("Step 2: Cross-verifying with Groq LLM...")
        try:
            verifier = dspy.ChainOfThought(CrossVerifyVisualAnalysis)
            verif_result = verifier(
                user_question=user_prompt,
                gemini_analysis=gemini_analysis,
            )

            clean_answer = clean_mobile_math(verif_result.final_verified_answer)

            return {
                "ok": True,
                "gemini_perception": gemini_analysis,
                "verification_notes": verif_result.verification_notes,
                "final_answer": clean_answer,
                "pipeline": "Gemini-Vision-3.8 + Groq-120B",
            }
        except Exception as exc:
            logger.warning("Groq verification fallback: %s", exc)
            # Fallback to Gemini analysis if verifier has an issue
            return {
                "ok": True,
                "gemini_perception": gemini_analysis,
                "verification_notes": "Groq verification skipped (fallback).",
                "final_answer": gemini_analysis,
                "pipeline": "Gemini-Vision-Direct",
            }
