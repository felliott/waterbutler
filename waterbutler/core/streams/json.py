import asyncio

from waterbutler.core.streams.base import StringStream
from waterbutler.core.streams.base import MultiStream


class JSONStream(MultiStream):
    """Takes a `dict` where the values are all strings or Streams and turns it into a MultiString
    of StringStreams for literal values and passes through the Streams.

    This class is very naive.  It's only used by the GitHub provider to turn a simple dict with the form:

    ```
    { 'encoding': 'base64', 'content': Base64EncodeStream}

    ```

    into::

      [
        StringStream('{'),
        StringStream('"encoding":"'),
        StringStream('base64'),
        StringStream('",'),
        StringStream('"content":"'),
        Base64EncodeStream,
        StringStream('",'),
        StringStream('"}'),
      ]
    """


    def __init__(self, data):
        streams = [StringStream('{')]
        for key, value in data.items():
            if not isinstance(value, asyncio.StreamReader):
                value = StringStream(value)
            streams.extend([StringStream('"{}":"'.format(key)), value, StringStream('",')])
        super().__init__(*(streams[:-1] + [StringStream('"}')]))
