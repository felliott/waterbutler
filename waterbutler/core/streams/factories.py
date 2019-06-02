import uuid

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
    """Make a MultiStream that represents the form-data encoded representation of the given data.

    child of MultiStream used to create stream friendly multipart form data requests.

    Usage::

        >>> stream = make_formdata_multistream(
        >>>     key1='value1',
        >>>     fieldName=(FileStream(...), 'fileName', 'Mime', 'encoding')
        >>> )

    Auto generates boundaries and properly concatenates them

    Passes
    Use FormDataStream.headers to get the proper headers to be included with requests
    Namely Content-Length, Content-Type

    :return: a `MultiStream` object
    """

    def _start_boundary(boundary):
        return StringStream('--{}\r\n'.format(boundary))

    def _end_boundary(boundary):
        return StringStream('--{}--\r\n'.format(boundary))

    def _make_header(name, disposition='form-data', additional_headers=None, **extra):
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

    def _make_headers(boundary):
        """The headers required to make a proper multipart form request."""
        return {
            # 'Content-Length': str(size),
            'Content-Type': 'multipart/form-data; boundary={}'.format(boundary)
        }

    boundary = uuid.uuid4().hex

    streams = []
    for key, value in data.items():
        streams.extend([_start_boundary(boundary)])

        if isinstance(value, dict):
            header = _make_header(
                key,
                filename=value['name'],
                disposition=value.get('disposition', 'file'),
                additional_headers={
                    'Content-Type': value.get('mime', 'application/octet-stream'),
                    'Content-Transfer-Encoding': value.get('transcoding', 'binary'),
                }
            )
            streams.extend([
                StringStream(header),
                value['stream'],
                StringStream('\r\n')
            ])
        else:
            streams.extend([
                StringStream(_make_header(key) + value + '\r\n')
            ])

    streams.extend([_end_boundary(boundary)])

    return MultiStream(streams, headers=_make_headers(boundary))
