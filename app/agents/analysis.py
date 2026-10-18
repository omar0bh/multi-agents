import dspy

class AnalyzeTask(dspy.Signature):
    """Analyze a task and identify risks and next steps."""
    task: str = dspy.InputField()
    analysis: str = dspy.OutputField()
    risks: str = dspy.OutputField()
    next_steps: str = dspy.OutputField()

class AnalysisAgent:
    def run(self, task: str) -> dict:
        from app.llm import configure_dspy
        configure_dspy()

        result = dspy.ChainOfThought(AnalyzeTask)(task=task)

        return {
            "analysis": result.analysis,
            "risks": result.risks,
            "next_steps": result.next_steps,
        }
