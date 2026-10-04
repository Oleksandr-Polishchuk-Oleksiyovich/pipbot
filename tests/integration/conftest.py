import pytest

from core.config import CANDIDATE_MODELS, GEMINI_API_KEY
from core.llm import GeminiLLM
from tests.flow_fixtures import make_flow


def pytest_collection_modifyitems(items):
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)
            if not GEMINI_API_KEY:
                item.add_marker(pytest.mark.skip(reason="GEMINI_API_KEY is not set"))


@pytest.fixture(scope="session")
def llm():
    return GeminiLLM.from_api_key(GEMINI_API_KEY, CANDIDATE_MODELS)


@pytest.fixture
def env(tmp_path, llm):
    return make_flow(tmp_path, llm)
