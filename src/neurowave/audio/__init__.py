"""Аудиовыход и DSP-модель."""

from .backend import AudioBackend, EndHandler, NullBackend
from .dsp_graph import build_filter_graph, describe_profile, summarize
from .dsp_profiles import BUILTIN_PROFILES, AudioProfile, DspError, DspMode, EqBand, Limiter, resolve_profile
from .mpv import MpvBackend

__all__ = [
    "AudioBackend",
    "AudioProfile",
    "BUILTIN_PROFILES",
    "DspError",
    "DspMode",
    "EndHandler",
    "EqBand",
    "Limiter",
    "MpvBackend",
    "NullBackend",
    "build_filter_graph",
    "describe_profile",
    "resolve_profile",
    "summarize",
]
