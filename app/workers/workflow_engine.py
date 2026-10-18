import dspy

class PlanWorkflow(dspy.Signature):
    """Design a business workflow. Do not claim actions were executed."""
    request: str = dspy.InputField()
    objective: str = dspy.OutputField()
    steps: str = dspy.OutputField()
    required_inputs: str = dspy.OutputField()
    risks_and_controls: str = dspy.OutputField()

class BusinessWorkflowAgent:
    def run(self, request: str) -> dict:
        from app.llm import configure_dspy
        configure_dspy()

        result = dspy.ChainOfThought(PlanWorkflow)(
            request=request,
        )

        return {
            "objective": result.objective,
            "steps": result.steps,
            "required_inputs": result.required_inputs,
            "risks_and_controls": result.risks_and_controls,
            "status": "plan_only",
        }
