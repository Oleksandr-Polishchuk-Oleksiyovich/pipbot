import pytest

from agents.router import has_passphrase


@pytest.mark.parametrize("text", [
    "Obey me",
    "Obey me!",
    "OBEY",
    "Ob\u0435y me",          # Cyrillic е
    "\u041eb\u0435y me",     # Cyrillic О and е
    "Лулуш наказує тобі",
    "Лілуш, постав норму",
    "Лелуш",
    "obeyy me",
    "obay me",
    "obe me",
    "Lelouch vi Britania",
    "лелуш ві британія",
    "осувари",
    "осуварі",
    "you wil obey",
])
def test_passphrase_matches(text):
    assert has_passphrase(text)


@pytest.mark.parametrize("text", [
    "",
    "привіт, як справи?",
    "they say hello",
    "okey, зроблю завтра",
    "Лулущ наказує",   # two typos vs лелуш
    "oaby me",         # two typos
    "abay",            # two typos
    "я зробив 25 віджимань",
    "обід був смачний",
])
def test_passphrase_rejects(text):
    assert not has_passphrase(text)
