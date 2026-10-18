import dspy

class GenerateCode(dspy.Signature):
    """Generate code for the requested task."""
    task: str = dspy.InputField()
    language: str = dspy.InputField()
    code: str = dspy.OutputField()
    explanation: str = dspy.OutputField()

class ReviewCode(dspy.Signature):
    """Review code for bugs, edge cases, and security concerns."""
    code: str = dspy.InputField()
    language: str = dspy.InputField()
    review: str = dspy.OutputField()
    tests: str = dspy.OutputField()

class CodingAgent:
    def run(self, task: str, language: str = "python") -> dict:
        from app.llm import configure_dspy
        configure_dspy()

        generated = dspy.ChainOfThought(GenerateCode)(
            task=task,
            language=language,
        )

        reviewed = dspy.ChainOfThought(ReviewCode)(
            code=generated.code,
            language=language,
        )

        return {
            "language": language,
            "code": generated.code,
            "explanation": generated.explanation,
            "review": reviewed.review,
            "suggested_tests": reviewed.tests,
        }
