import os
import json
import uuid
import typing
import hashlib
import logging

from waterbutler.core import utils
from waterbutler.core import signing
from waterbutler.core import streams
from waterbutler.core import provider
from waterbutler.core import exceptions
from waterbutler.core.path import WaterButlerPath
from waterbutler.core.metadata import BaseMetadata
from waterbutler.core.stream_wrappers import SimpleStreamWrapper, DigestStreamWrapper

from waterbutler.providers.osfstorage import settings
from waterbutler.providers.osfstorage.metadata import OsfStorageFileMetadata
from waterbutler.providers.osfstorage.metadata import OsfStorageFolderMetadata
from waterbutler.providers.osfstorage.metadata import OsfStorageRevisionMetadata

logger = logging.getLogger(__name__)

QUERY_METHODS = ('GET', 'DELETE')


class OSFStorageProvider(provider.BaseProvider):
    """Provider for the Open Science Framework's cloud storage service.

    ``osfstorage`` is actually a pair of providers.  One is the metadata provider, the other is the
    actual storage provider, where the data is kept.  The OSF is the metadata provider.  Any
    metadata queries about objects in ``osfstorage`` are routed to the OSF to be answered.  For
    https://osf.io, the storage provider is ``cloudfiles``. For local testing the ``filesystem``
    provider is used instead.  Uploads and downloads are routed to and from the storage provider,
    with additional queries to the metadata provider to set and get metadata about the object.
    """

    __version__ = '0.0.1'

    NAME = 'osfstorage'

    def __init__(self, auth, credentials, settings):
        super().__init__(auth, credentials, settings)
        self.nid = settings['nid']
        self.root_id = settings['rootId']
        self.BASE_URL = settings['baseUrl']
        self.provider_name = settings['storage'].get('provider')

    async def make_signed_request(self, method, url, data=None, params=None, ttl=100, **kwargs):
        url, data, params = self.build_signed_url(
            method,
            url,
            data=data,
            params=params,
            ttl=ttl,
            **kwargs
        )
        return await self.make_request(method, url, data=data, params=params, **kwargs)

    async def validate_v1_path(self, path, **kwargs):
        if path == '/':
            return WaterButlerPath('/', _ids=[self.root_id], folder=True)

        implicit_folder = path.endswith('/')
        obj_id = path.strip('/')

        resp = await self.make_signed_request(
            'GET',
            self.build_url(obj_id, 'lineage'),
            expects=(200,)
        )

        data = await resp.json()
        explicit_folder = data['data'][0]['kind'] == 'folder'
        if explicit_folder != implicit_folder:
            raise exceptions.NotFoundError(str(path))

        names, ids = zip(*[(x['name'], x['id']) for x in reversed(data['data'])])

        return WaterButlerPath('/'.join(names), _ids=ids, folder=explicit_folder)

    async def validate_path(self, path, **kwargs):
        if path == '/':
            return WaterButlerPath('/', _ids=[self.root_id], folder=True)

        ends_with_slash = path.endswith('/')

        try:
            path, name = path.strip('/').split('/')
        except ValueError:
            path, name = path, None

        resp = await self.make_signed_request(
            'GET',
            self.build_url(path, 'lineage'),
            expects=(200, 404)
        )
        if resp.status == 404:
            await resp.release()
            return WaterButlerPath(path, _ids=(self.root_id, None), folder=path.endswith('/'))
        data = await resp.json()

        is_folder = data['data'][0]['kind'] == 'folder'
        names, ids = zip(*[(x['name'], x['id']) for x in reversed(data['data'])])
        if name is not None:
            ids += (None, )
            names += (name, )
            is_folder = ends_with_slash

        return WaterButlerPath('/'.join(names), _ids=ids, folder=is_folder)

    async def revalidate_path(self, base, path, folder=False):
        assert base.is_dir

        try:
            data = next(
                x for x in
                await self.metadata(base)
                if x.name == path and
                x.kind == ('folder' if folder else 'file')
            )

            return base.child(data.name, _id=data.path.strip('/'), folder=folder)
        except StopIteration:
            return base.child(path, folder=folder)

    def make_provider(self, settings):
        """Requests on different files may need to use different providers,
        instances, e.g. when different files lives in different containers
        within a provider. This helper creates a single-use provider instance
        that optionally overrides the settings.

        :param dict settings: Overridden settings
        """
        if not getattr(self, '_inner_provider', None):
            self._inner_provider = utils.make_provider(
                self.provider_name,
                self.auth,
                self.credentials['storage'],
                self.settings['storage'],
            )
        return self._inner_provider

    def can_duplicate_names(self):
        return True

    def is_same_region(self, other):
        assert isinstance(other, self.__class__), 'Cannot compare region for providers of ' \
                                                  'different provider classes.'

        # Region does not apply to local development with filesystem as storage backend.
        if self.settings['storage']['provider'] == 'filesystem':
            return True
        # For 1-to-1 bucket-region mapping, bucket is the same if and only if region is the same
        return self.settings['storage']['bucket'] == other.settings['storage']['bucket']

    def can_intra_copy(self, other, path=None):
        return isinstance(other, self.__class__) and self.is_same_region(other)

    def can_intra_move(self, other, path=None):
        return isinstance(other, self.__class__) and self.is_same_region(other)

    async def intra_move(self, dest_provider, src_path, dest_path):
        return await self._do_intra_move_or_copy('move', dest_provider, src_path, dest_path)

    async def intra_copy(self, dest_provider, src_path, dest_path):
        return await self._do_intra_move_or_copy('copy', dest_provider, src_path, dest_path)

    def build_signed_url(self, method, url, data=None, params=None, ttl=100, **kwargs):
        signer = signing.Signer(settings.HMAC_SECRET, settings.HMAC_ALGORITHM)
        if method.upper() in QUERY_METHODS:
            signed = signing.sign_data(signer, params or {}, ttl=ttl)
            params = signed
        else:
            signed = signing.sign_data(signer, json.loads(data or {}), ttl=ttl)
            data = json.dumps(signed)

        # Ensure url ends with a /
        if not url.endswith('/'):
            if '?' not in url:
                url += '/'
            elif url[url.rfind('?') - 1] != '/':
                url = url.replace('?', '/?')

        return url, data, params

    async def download(self, path, version=None, revision=None, mode=None, **kwargs):
        if not path.identifier:
            raise exceptions.NotFoundError(str(path))

        self.metrics.add('download', {
            'mode_provided': mode is not None,
            'version_from': 'revision' if version is None else 'version',
            'user_logged_in': self.auth.get('id', None) is not None,
        })
        if version is None:
            # TODO Clean this up
            # version could be 0 here
            version = revision

        # Capture user_id for analytics if user is logged in
        user_param = {}
        if self.auth.get('id', None):
            user_param = {'user': self.auth['id']}

        # osf storage metadata will return a virtual path within the provider
        resp = await self.make_signed_request(
            'GET',
            self.build_url(path.identifier, 'download', version=version, mode=mode),
            expects=(200, ),
            params=user_param,
            throws=exceptions.DownloadError,
        )
        data = await resp.json()

        provider = self.make_provider(data['settings'])
        name = data['data'].pop('name')
        data['data']['path'] = await provider.validate_path('/' + data['data']['path'])
        download_kwargs = {}
        download_kwargs.update(kwargs)
        download_kwargs.update(data['data'])
        download_kwargs['display_name'] = kwargs.get('display_name') or name
        return await provider.download(**download_kwargs)

    async def upload(self, stream: SimpleStreamWrapper, path, **kwargs):  # type: ignore
        """Upload a new file to osfstorage

        When a file is uploaded to osfstorage, WB does a bit of a dance to make sure it gets there
        reliably.  First we take the stream and add several hash calculators that can determine the
        hash of the file as it streams through.  We then upload the file to a uuid-named file on the
        remote storage provider.  Once that's complete, we determine the file's final name, which
        will be its sha256 hash.  We then check to see if a file already exists at that path on the
        remote storage provider.  If it does, we can skip moving the file (since it has already been
        uploaded) and instead delete the pending file. If it does not, we move the file on the
        remote storage provider from the pending path to its final path.

        Once this is done the file metadata is sent back to the metadata provider to be recorded.
        Finally, WB constructs its metadata response and sends that back to the original request
        issuer.
        """

        metadata_obj, digests = await self._send_to_storage_provider(stream, path, **kwargs)
        logger.info('Yargga yargga: digests:({})'.format(digests))
        metadata = metadata_obj.serialized()

        data, created = await self._send_to_metadata_provider(path, metadata, digests, **kwargs)

        name = path.name

        metadata.update({
            'name': name,
            'md5': data['data']['md5'],
            'path': data['data']['path'],
            'sha256': data['data']['sha256'],
            'version': data['data']['version'],
            'downloads': data['data']['downloads'],
            'checkout': data['data']['checkout'],
            'modified': data['data']['modified'],
            'modified_utc': utils.normalize_datetime(data['data']['modified']),
        })

        path._parts[-1]._id = metadata['path'].strip('/')
        return OsfStorageFileMetadata(metadata, str(path)), created

    async def delete(self, path, confirm_delete=0, **kwargs):
        """Delete file, folder, or provider root contents

        :param OsfStoragePath path: path to delete
        :param int confirm_delete: Must be 1 to confirm root folder delete
        """
        if path.identifier is None:
            raise exceptions.NotFoundError(str(path))

        self.metrics.add('delete.is_root_delete', path.is_root)
        if path.is_root:
            self.metrics.add('delete.root_delete_confirmed', confirm_delete == 1)
            if confirm_delete == 1:
                await self._delete_folder_contents(path)
                return
            else:
                raise exceptions.DeleteError(
                    'confirm_delete=1 is required for deleting root provider folder',
                    code=400,
                )

        await (await self.make_signed_request(
            'DELETE',
            self.build_url(path.identifier),
            params={'user': self.auth['id']},
            expects=(200, )
        )).release()

    async def metadata(self, path, **kwargs):
        if path.identifier is None:
            raise exceptions.MetadataError('{} not found'.format(str(path)), code=404)

        if not path.is_dir:
            return await self._item_metadata(path)
        return await self._children_metadata(path)

    async def revisions(self, path, view_only=None, **kwargs):
        if path.identifier is None:
            raise exceptions.MetadataError('File not found', code=404)

        self.metrics.add('revisions', {'got_view_only': view_only is not None})

        resp = await self.make_signed_request(
            'GET',
            self.build_url(path.identifier, 'revisions', view_only=view_only),
            expects=(200, )
        )
        return [
            OsfStorageRevisionMetadata(item)
            for item in (await resp.json())['revisions']
        ]

    async def create_folder(self, path, **kwargs):
        resp = await self.make_signed_request(
            'POST',
            self.build_url(path.parent.identifier, 'children'),
            data=json.dumps({
                'kind': 'folder',
                'name': path.name,
                'user': self.auth['id'],
            }),
            headers={'Content-Type': 'application/json'},
            expects=(201, )
        )
        resp_json = await resp.json()
        # save new folder's id into the WaterButlerPath object. logs will need it later.
        path._parts[-1]._id = resp_json['data']['path'].strip('/')
        return OsfStorageFolderMetadata(resp_json['data'], str(path))

    async def move(self,
                   dest_provider: provider.BaseProvider,
                   src_path: WaterButlerPath,
                   dest_path: WaterButlerPath,
                   rename: str=None,
                   conflict: str='replace',
                   handle_naming: bool=True) -> typing.Tuple[BaseMetadata, bool]:
        """Override parent's move to support cross-region osfstorage moves while preserving guids
        and versions. Delegates to :meth:`.BaseProvider.move` when destination is not osfstorage.
        If both providers are in the same region (i.e. `.can_intra_move` is true), then calls that.
        Otherwise, will grab a download stream from the source region, send it to the destination
        region, *then* execute an `.intra_move` to update the file metada in-place.
        """

        # when moving to non-osfstorage, default move is fine
        if dest_provider.NAME != 'osfstorage':
            return await super().move(dest_provider, src_path, dest_path, rename=rename,
                                      conflict=conflict, handle_naming=handle_naming)

        args = (dest_provider, src_path, dest_path)
        kwargs = {'rename': rename, 'conflict': conflict}

        self.provider_metrics.add('move', {
            'got_handle_naming': handle_naming,
            'conflict': conflict,
            'got_rename': rename is not None,
        })

        if handle_naming:
            dest_path = await dest_provider.handle_naming(
                src_path,
                dest_path,
                rename=rename,
                conflict=conflict,
            )
            args = (dest_provider, src_path, dest_path)
            kwargs = {}

        # files and folders shouldn't overwrite themselves
        if (
            self.shares_storage_root(dest_provider) and
            src_path.materialized_path == dest_path.materialized_path
        ):
            raise exceptions.OverwriteSelfError(src_path)

        self.provider_metrics.add('move.can_intra_move', False)
        if self.can_intra_move(dest_provider, src_path):
            self.provider_metrics.add('move.can_intra_move', True)
            return await self.intra_move(*args)

        if src_path.is_dir:
            meta_data, created = await self._folder_file_op(self.move, *args, **kwargs)  # type: ignore
            await self.delete(src_path)
        else:
            download_stream = await self.download(src_path)
            if getattr(download_stream, 'name', None):
                dest_path.rename(download_stream.name)

            _, _ = await dest_provider._send_to_storage_provider(download_stream,  # type: ignore
                                                                 dest_path, **kwargs)
            meta_data, created = await self.intra_move(dest_provider, src_path, dest_path)

        return meta_data, created

    async def copy(self,
                   dest_provider: provider.BaseProvider,
                   src_path: WaterButlerPath,
                   dest_path: WaterButlerPath,
                   rename: str=None,
                   conflict: str='replace',
                   handle_naming: bool=True) -> typing.Tuple[BaseMetadata, bool]:
        """Override parent's copy to support cross-region osfstorage copies. Delegates to
        :meth:`.BaseProvider.copy` when destination is not osfstorage. If both providers are in the
        same region (i.e. `.can_intra_copy` is true), call `.intra_copy`. Otherwise, grab a
        download stream from the source region, send it to the destination region, *then* execute
        an `.intra_copy` to make new file metadata entries in the OSF.

        This is needed because a same-region osfstorage copy will duplicate *all* the versions of
        the file, but `.BaseProvider.copy` will only copy the most recent version.
        """

        # when moving to non-osfstorage, default move is fine
        if dest_provider.NAME != 'osfstorage':
            return await super().copy(dest_provider, src_path, dest_path, rename=rename,
                                      conflict=conflict, handle_naming=handle_naming)

        args = (dest_provider, src_path, dest_path)
        kwargs = {'rename': rename, 'conflict': conflict}

        self.provider_metrics.add('copy', {
            'got_handle_naming': handle_naming,
            'conflict': conflict,
            'got_rename': rename is not None,
        })

        if handle_naming:
            dest_path = await dest_provider.handle_naming(
                src_path,
                dest_path,
                rename=rename,
                conflict=conflict,
            )
            args = (dest_provider, src_path, dest_path)
            kwargs = {}

        # files and folders shouldn't overwrite themselves
        if (
            self.shares_storage_root(dest_provider) and
            src_path.materialized_path == dest_path.materialized_path
        ):
            raise exceptions.OverwriteSelfError(src_path)

        self.provider_metrics.add('copy.can_intra_copy', False)
        if self.can_intra_copy(dest_provider, src_path):
            self.provider_metrics.add('copy.can_intra_copy', True)
            return await self.intra_copy(*args)

        if src_path.is_dir:
            meta_data, created = await self._folder_file_op(self.copy, *args, **kwargs)  # type: ignore
        else:
            download_stream = await self.download(src_path)
            if getattr(download_stream, 'name', None):
                dest_path.rename(download_stream.name)

            await dest_provider._send_to_storage_provider(download_stream,  # type: ignore
                                                          dest_path, **kwargs)
            meta_data, created = await self.intra_copy(dest_provider, src_path, dest_path)

        return meta_data, created

    # ========== private ==========

    async def _item_metadata(self, path, revision=None):
        resp = await self.make_signed_request(
            'GET',
            self.build_url(path.identifier, revision=revision),
            expects=(200, )
        )
        return OsfStorageFileMetadata((await resp.json()), str(path))

    async def _children_metadata(self, path):
        resp = await self.make_signed_request(
            'GET',
            self.build_url(path.identifier, 'children', user_id=self.auth.get('id')),
            expects=(200, )
        )
        resp_json = await resp.json()

        ret = []
        for item in resp_json:
            if item['kind'] == 'folder':
                ret.append(OsfStorageFolderMetadata(item, str(path.child(item['name'], folder=True))))
            else:
                ret.append(OsfStorageFileMetadata(item, str(path.child(item['name']))))
        return ret

    async def _delete_folder_contents(self, path, **kwargs):
        """Delete the contents of a folder. For use against provider root.

        :param OsfStoragePath path: OsfStoragePath path object for folder
        """
        meta = (await self.metadata(path))
        for child in meta:
            osf_path = await self.validate_path(child.path)
            await self.delete(osf_path)

    async def _do_intra_move_or_copy(self, action: str, dest_provider, src_path, dest_path):
        """Update files and folders on osfstorage with a single request.

        If the data of the file or the folder's children doesn't need to be copied to another
        bucket, then doing an intra-move or intra-copy is just a matter of updating the entity
        metadata in the OSF.  If something already exists at ``dest_path``, it must be deleted
        before relocating the source to the new path.
        """

        created = True
        if dest_path.identifier:
            created = False
            await dest_provider.delete(dest_path)

        resp = await self.make_signed_request(
            'POST',
            self.build_url('hooks', action),
            data=json.dumps({
                'user': self.auth['id'],
                'source': src_path.identifier,
                'destination': {
                    'name': dest_path.name,
                    'node': dest_provider.nid,
                    'parent': dest_path.parent.identifier
                }
            }),
            headers={'Content-Type': 'application/json'},
            expects=(200, 201)
        )
        data = await resp.json()

        if data['kind'] == 'file':
            return OsfStorageFileMetadata(data, str(dest_path)), dest_path.identifier is None

        folder_meta = OsfStorageFolderMetadata(data, str(dest_path))
        dest_path = await dest_provider.validate_v1_path(data['path'])
        folder_meta.children = await dest_provider._children_metadata(dest_path)

        return folder_meta, created

    async def _send_to_storage_provider(self, stream: SimpleStreamWrapper, path,
                                        **kwargs) -> typing.Tuple[BaseMetadata, dict]:
        """Send uploaded file data to the storage provider, where it will be stored w/o metadata
        in a content-addressable format.

        :return: metadata of the file as it exists on the storage provider
        """

        logger.info('????? alpha')
        digest_objs = {
            'md5': streams.HashStreamWriter(hashlib.md5),
            'sha1': streams.HashStreamWriter(hashlib.sha1),
            'sha256': streams.HashStreamWriter(hashlib.sha256),
        }

        digest_stream = DigestStreamWrapper(stream, digest_objs)

        logger.info('????? beta')
        pending_name = str(uuid.uuid4())
        provider = self.make_provider(self.settings)
        remote_pending_path = await provider.validate_path('/' + pending_name)
        logger.debug('upload: remote_pending_path::{}'.format(remote_pending_path))

        logger.info('????? gamma')
        await provider.upload(digest_stream, remote_pending_path, check_created=False,
                              fetch_metadata=False, **kwargs)

        logger.info('????? delta')
        complete_name = digest_stream.writers['sha256'].hexdigest
        remote_complete_path = await provider.validate_path('/' + complete_name)

        logger.info('????? epsilon')
        logger.info('=== remote_complete_path:({})'.format(remote_complete_path))
        try:
            metadata = await provider.metadata(remote_complete_path)
        except exceptions.MetadataError as e:
            if e.code != 404:
                raise
            metadata, _ = await provider.move(provider, remote_pending_path, remote_complete_path)
        else:
            await provider.delete(remote_pending_path)

        logger.info('????? zeta')
        digests = {}
        for key in digest_objs.keys():
            digests[key] = digest_objs[key].hexdigest

        logger.info('$$$$ inside _send_to_storage_provider: digests:({})'.format(digests))
        return metadata, digests

    async def _send_to_metadata_provider(self, path, metadata, digests: dict,
                                         **kwargs) -> typing.Tuple[dict, dict]:
        """Send metadata about the uploaded file (including its location on the storage provider) to
        the OSF.

        :return: metadata of the file and a bool indicating if the file was newly created
        """

        resp = await self.make_signed_request(
            'POST',
            self.build_url(path.parent.identifier, 'children'),
            expects=(200, 201),
            data=json.dumps({
                'name': path.name,
                'user': self.auth['id'],
                'settings': self.settings['storage'],
                'metadata': metadata,
                'hashes': digests,
                'worker': {
                    'host': os.uname()[1],
                    # TODO: Include additional information
                    'address': None,
                    'version': self.__version__,
                },
            }),
            headers={'Content-Type': 'application/json'},
        )
        created = resp.status == 201
        data = await resp.json()

        return data, created
