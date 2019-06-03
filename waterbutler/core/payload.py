import aiohttp

from waterbutler import settings as wb_settings


class WaterButlerPayload(aiohttp.payload.AsyncIterablePayload):

    def __init__(self, value, *args, **kwargs) -> None:
        super().__init__(value.iter_chunked(wb_settings.CHUNK_SIZE), *args, **kwargs)
