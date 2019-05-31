import uuid
import asyncio

from waterbutler.core.streams.base import MultiStream, SimpleStreamWrapper, StringStream


def make_multistream_from_json(data):
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

    streams = [StringStream('{')]
    for key, value in data.items():
        if not isinstance(value, SimpleStreamWrapper):
            value = StringStream(value)
        streams.extend([StringStream('"{}":"'.format(key)), value, StringStream('",')])

    streams.extend([StringStream('"}')])
    return MultiStream(streams)


def make_formdata_multistream(data):
    """A child of MultiSteam used to create stream friendly multipart form data requests.
    Usage:

        >>> stream = FormDataStream(key1='value1', file=FileStream(...))

    Or:

        >>> stream = FormDataStream()
        >>> stream.add_field('key1', 'value1')
        >>> stream.add_file('file', FileStream(...), mime='text/plain')

    Additional options for files can be passed as a tuple ordered as:

        >>> FormDataStream(fieldName=(FileStream(...), 'fileName', 'Mime', 'encoding'))

    Auto generates boundaries and properly concatenates them
    Use FormDataStream.headers to get the proper headers to be included with requests
    Namely Content-Length, Content-Type
    """

    """:param dict fields: A dict of fieldname: value to create the body of the stream"""
    boundary = self.make_boundary()

    streams = []
    for key, value in data.items():
        if isinstance(value, tuple):
            streams.extend(self.add_file(key, *value)
        elif isinstance(value, asyncio.StreamReader):
            self.add_file(key, value)
        else:
            self.add_field(key, value)

    return MultiStream(streams)

    @classmethod
    def make_boundary(cls):
        """Creates a random-ish boundary for form data separator"""
        return uuid.uuid4().hex

    @classmethod
    def make_header(cls, name, disposition='form-data', additional_headers=None, **extra):
        additional_headers = additional_headers or {}
        header = 'Content-Disposition: {}; name="{}"'.format(disposition, name)

        header += ''.join([
            '; {}="{}"'.format(key, value)
            for key, value in extra.items() if value is not None
        ])

        additional = '\r\n'.join([
            '{}: {}'.format(key, value)
            for key, value in additional_headers.items() if value is not None
        ])

        header += '\r\n'

        if additional:
            header += additional
            header += '\r\n'

        return header + '\r\n'


    @property
    def end_boundary(self):
        return StringStream('--{}--\r\n'.format(self.boundary))

    @property
    def headers(self):
        """The headers required to make a proper multipart form request
        Implicitly calls finalize as accessing headers will often indicate sending of the request
        Meaning nothing else will be added to the stream"""
        self.finalize()

        return {
            'Content-Length': str(self.size),
            'Content-Type': 'multipart/form-data; boundary={}'.format(self.boundary)
        }

    async def read(self, n=-1):
        if self.can_add_more:
            self.finalize()
        return (await super().read(n=n))

    def finalize(self):
        assert self.stream, 'Must add at least one stream to finalize'

        if self.can_add_more:
            self.can_add_more = False
            self.add_streams(self.end_boundary)

    def add_fields(self, **fields):
        for key, value in fields.items():
            self.add_field(key, value)

    def add_field(self, key, value):
        assert self.can_add_more, 'Cannot add more fields after calling finalize or read'

        self.add_streams(
            self._make_boundary_stream(),
            StringStream(self.make_header(key) + value + '\r\n')
        )

    def add_file(self, field_name, file_stream, file_name=None, mime='application/octet-stream',
                 disposition='file', transcoding='binary'):
        assert self.can_add_more, 'Cannot add more fields after calling finalize or read'

        header = self.make_header(
            field_name,
            disposition=disposition,
            filename=file_name,
            additional_headers={
                'Content-Type': mime,
                'Content-Transfer-Encoding': transcoding
            }
        )

        self.add_streams(
            self._make_boundary_stream(),
            StringStream(header),
            file_stream,
            StringStream('\r\n')
        )

    def _make_boundary_stream(self):
        return StringStream('--{}\r\n'.format(self.boundary))

