import os
import sys

# Корінь репозиторію в sys.path, щоб тести імпортували bot, app, agents, core, database.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# On the VM (INSTALL_CV=0) numpy/mediapipe are absent: skip CV-only test modules.
import importlib.util as _ilu
if _ilu.find_spec("numpy") is None:
    collect_ignore = globals().get("collect_ignore", []) + [
        "test_rep_counter.py", "test_cv_vision.py", "test_cv_worker.py", "test_box_cv_offload.py",
    ]
