"""
CV package for SportNerdBot: pose estimation and rep counting.
"""
from core.cv.rep_counter import (
    analyze_video,
    count_hysteresis,
    count_pushups_vy,
    is_unreliable_camera_angle,
    probe,
    is_cv_available,
    DEFAULT_CFG,
)

__all__ = [
    "analyze_video",
    "count_hysteresis",
    "count_pushups_vy",
    "is_unreliable_camera_angle",
    "probe",
    "is_cv_available",
    "DEFAULT_CFG",
]

