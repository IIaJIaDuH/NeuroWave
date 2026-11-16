"""Локальная музыка: второй настоящий провайдер без сети и DRM."""

from .adapter import LocalMusicAdapter, default_root

__all__ = ["LocalMusicAdapter", "default_root"]
