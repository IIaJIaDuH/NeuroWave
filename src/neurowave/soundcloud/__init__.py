"""SoundCloud как третий провайдер: каталог, поиск и прямой поток.

Сеть живёт только в :mod:`neurowave.soundcloud.client`; адаптер и тесты
работают через подменяемый транспорт, поэтому unit-тесты не ходят в интернет.
"""

from .adapter import SoundCloudAdapter
from .client import HttpResponse, SoundCloudClient, UrllibTransport

__all__ = ["SoundCloudAdapter", "SoundCloudClient", "HttpResponse", "UrllibTransport"]
