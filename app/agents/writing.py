import dspy

class WriteDocument(dspy.Signature):
    """Write the requested document for the specified audience."""
    task: str = dspy.InputField()
    context: str = dspy.InputField()
    document: str = dspy.OutputField()

class WritingAgent:
    def run(self, task: str, context: str = "") -> dict:
        from app.llm import configure_dspy
        configure_dspy()

        result = dspy.ChainOfThought(WriteDocument)(
            task=task,
            context=context,
        )

        return {"document": result.document}
