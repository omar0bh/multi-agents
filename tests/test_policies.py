from app.orchestration.policies import classify_task

def test_research_route():
    assert classify_task("Research cloud security sources") == "research"

def test_coding_route():
    assert classify_task("Write Python code to parse a file") == "coding"

def test_business_route():
    assert classify_task("Automate an invoice approval workflow") == "business"


def test_routing_reason():
    from app.orchestration.policies import routing_reason

    reason = routing_reason("Research cloud security sources")
    assert reason["workflow"] == "research"
    assert reason["method"] == "keyword"
