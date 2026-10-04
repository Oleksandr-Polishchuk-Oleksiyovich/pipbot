import os
import sys

# Корінь репозиторію в sys.path, щоб тести імпортували bot, app, agents, core, database.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
