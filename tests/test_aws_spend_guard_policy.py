import importlib.util
from pathlib import Path


def _load_spend_guard():
    root = Path(__file__).resolve().parents[1]
    path = root / "static" / "aws_cost_export_func" / "spend_guard" / "spend_guard.py"
    spec = importlib.util.spec_from_file_location("aws_spend_guard", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_deny_policy_blocks_bedrock_runtime_and_bearer_token_paths():
    spend_guard = _load_spend_guard()
    policy = spend_guard._deny_policy_document()
    actions = set(policy["Statement"][0]["Action"])

    assert {
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:Converse",
        "bedrock:ConverseStream",
        "bedrock:CallWithBearerToken",
        "bedrock-mantle:CallWithBearerToken",
    }.issubset(actions)
