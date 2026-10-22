"""
Захист від «?»-покаліченого тексту: Windows-консоль (code page) або перекодування в ASCII перетворює
кирилицю на літеральні '?' ще ДО того, як текст потрапить на VM. Такий текст не можна зберігати в БД.
Правило: ніколи не передавати кирилицю через командний рядок Windows — лише UTF-8 файлами (див. deploy/README.md).
"""
import re

_MANGLED = re.compile(r"\?{4,}|\ufffd")


def looks_mangled(text) -> bool:
    """True, якщо текст схожий на зіпсований кодуванням: серії '????' або U+FFFD."""
    return isinstance(text, str) and bool(_MANGLED.search(text))


def ensure_clean(text, what: str = "text"):
    if looks_mangled(text):
        raise ValueError(f"{what} looks encoding-mangled ('????' / U+FFFD) — refusing to store; ship it as a UTF-8 file")
    return text
