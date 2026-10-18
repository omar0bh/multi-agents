import dspy

class GeneralTask(dspy.Signature):
    """Answer a user task with clear, actionable information."""
    task: str = dspy.InputField()
    answer: str = dspy.OutputField()

class GeneralAgent:
    def run(self, task: str) -> dict:
        from app.llm import configure_dspy
        configure_dspy()

        result = dspy.ChainOfThought(GeneralTask)(task=task)

        return {"answer": result.answer}
