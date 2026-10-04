from core.llm import GeminiLLM
from database.store import Store
from prompts import DEFAULT_PROMPTS


class PromptAgent:
    """Агент із власним сегментованим промптом, який читається з БД (таблиця prompts) на кожен виклик."""

    prompt_key: str = ""

    def __init__(self, llm: GeminiLLM, store: Store):
        self.llm = llm
        self.store = store

    @property
    def system_prompt(self) -> str:
        return self.store.get_prompt(self.prompt_key) or DEFAULT_PROMPTS[self.prompt_key]


def render_messages(messages) -> str:
    return "\n".join(m.render() for m in messages) or "(повідомлень немає)"
