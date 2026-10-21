"""
Кешований доступ до фіксованих відповідей бота / тон-зразків (таблиця bot_texts).
БД має пріоритет над prompts.DEFAULT_BOT_TEXTS; set() одразу інвалідовує кеш —
адмінська правка діє без рестарту й без деплою.
"""
import logging
import string
from typing import Any, Dict, List, Optional

from database.store import Store
from prompts import DEFAULT_BOT_TEXTS

logger = logging.getLogger(__name__)


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _safe_format(template: str, **params: Any) -> str:
    """str.format, що не падає й не чіпає плейсхолдери, для яких не передали параметр."""
    try:
        return string.Formatter().vformat(template, (), _SafeDict(**params))
    except Exception as e:
        logger.warning("bot_texts format failed for template %r: %s", template, e)
        return template


class BotTexts:
    def __init__(self, store: Store):
        self.store = store
        self._cache: Dict[str, str] = {}

    def get(self, key: str, **params: Any) -> str:
        if key not in self._cache:
            self._cache[key] = self._resolve(key)
        return _safe_format(self._cache[key], **params)

    def _resolve(self, key: str) -> str:
        try:
            row = self.store.get_bot_text(key)
        except Exception as e:
            logger.warning("bot_texts lookup failed for %s: %s", key, e)
            row = None
        if row and row.get("text"):
            return row["text"]
        return DEFAULT_BOT_TEXTS.get(key, "")

    def set(
        self,
        key: str,
        text: str,
        variants: Optional[List[str]] = None,
        updated_by: Optional[str] = None,
    ) -> None:
        self.store.set_bot_text(key, text, variants=variants, updated_by=updated_by)
        self.invalidate(key)

    def invalidate(self, key: Optional[str] = None) -> None:
        if key is None:
            self._cache.clear()
        else:
            self._cache.pop(key, None)

    def reload(self) -> None:
        self._cache.clear()

    def list_keys(self) -> List[Dict[str, Any]]:
        return self.store.list_bot_texts()
