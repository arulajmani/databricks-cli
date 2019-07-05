# Databricks CLI
# Copyright 2017 Databricks, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"), except
# that the use of services to which certain application programming
# interfaces (each, an "API") connect requires that the user first obtain
# a license for the use of the APIs from Databricks, Inc. ("Databricks"),
# by creating an account at www.databricks.com and agreeing to either (a)
# the Community Edition Terms of Service, (b) the Databricks Terms of
# Service, or (c) another written agreement between Licensee and Databricks
# for the use of the APIs.
#
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from hashlib import sha1
import os

from six.moves import urllib

from databricks_cli.sdk import DeltaPipelinesService
from databricks_cli.dbfs.api import DbfsApi
from databricks_cli.dbfs.dbfs_path import DbfsPath


# These imports are specific to the credentials part
from databricks_cli.configure.config import get_profile_from_context
from databricks_cli.configure.provider import get_config, ProfileConfigProvider
from databricks_cli.utils import InvalidConfigurationError

BUFFER_SIZE = 1024 * 64
base_pipelines_dir = 'dbfs:/pipelines/code/'


class PipelinesApi(object):
    def __init__(self, api_client):
        self.client = DeltaPipelinesService(api_client)
        self.dbfs_client = DbfsApi(api_client)

    def deploy(self, spec, headers=None):
        lib_objects = LibraryObject.convert_from_libraries(spec.get('libraries', []))
        local_lib_objects, rest_lib_objects = \
            self._partition_libraries_and_extract_local_paths(lib_objects)
        remote_lib_objects = list(map(lambda llo:
                                      LibraryObject(llo.lib_type, self._get_hashed_path(llo.path)),
                                      local_lib_objects))
        upload_files = self._get_files_to_upload(local_lib_objects, remote_lib_objects)

        for llo, rlo in upload_files:
            try:
                self.dbfs_client.put_file(llo.path, rlo.path, False)
            except Exception as e:
                raise RuntimeError('Error \'{}\' while uploading {}'.format(e, llo.path))

        spec['libraries'] = LibraryObject.convert_to_libraries(rest_lib_objects +
                                                               remote_lib_objects)
        spec['credentials'] = self._get_credentials_for_request()
        self.client.client.perform_query('PUT',
                                         '/pipelines/{}'.format(spec['id']),
                                         data=spec,
                                         headers=headers)

    def delete(self, pipeline_id, headers=None):
        self.client.delete(pipeline_id, self._get_credentials_for_request(), headers)

    @staticmethod
    def _partition_libraries_and_extract_local_paths(lib_objects):
        """
        Partitions the given set of libraries into local and remote by checking uri scheme
        :param lib_objects: List[LibraryObject]
        :return: List[List[LibraryObject], List[LibraryObject]] [Local, Remote]
        """
        local_lib_objects, rest_lib_objects = [], []
        for lib_object in lib_objects:
            uri_scheme = urllib.parse.urlsplit(lib_object.path).scheme
            if lib_object.lib_type == 'jar' and uri_scheme == '':
                local_lib_objects.append(lib_object)
            elif lib_object.lib_type == 'jar' and uri_scheme.lower() == 'file':
                if lib_object.path[4:].startswith(':////'):
                    raise RuntimeError('Invalid file uri scheme')
                local_lib_objects.append(LibraryObject(lib_object.lib_type, lib_object.path[5:]))
            else:
                rest_lib_objects.append(lib_object)
        return local_lib_objects, rest_lib_objects

    @staticmethod
    def _get_hashed_path(path):
        """
        Finds the corresponding dbfs file path for the file located at the supplied path by
        calculating its hash.
        :param path: Local File Path
        :return: Remote Path (pipeline_base_dir + file_hash (dot) file_extension)
        """
        hash_buffer = sha1()
        try:
            with open(path, 'rb') as f:
                while True:
                    data = f.read(BUFFER_SIZE)
                    if not data:
                        break
                    hash_buffer.update(data)
        except Exception as e:
            raise RuntimeError('Error \'{}\' while processing {}'.format(e, path))

        file_hash = hash_buffer.hexdigest()
        path = '{}{}{}'.format(base_pipelines_dir, file_hash, os.path.splitext(path)[1])
        return path

    def _get_files_to_upload(self, local_lib_objects, remote_lib_objects):
        """
        Returns a Local/Remote pair for every file that needs to be uploaded to dbfs. Files are
        stored under base_pipelines_dir defined above, and we only upload files that don't already
        exist in dbfs.
        :param local_lib_objects: List[LibraryObject]
        :param remote_lib_objects: List[LibraryObject]
        :return: List[(LibraryObject, LibraryObject)]
        """
        transformed_remote_lib_objects = map(
            lambda rlo: LibraryObject(rlo.lib_type, DbfsPath(rlo.path)),
            remote_lib_objects)
        return list(filter(lambda lo_tuple: not self.dbfs_client.file_exists(lo_tuple[1].path),
                           zip(local_lib_objects, transformed_remote_lib_objects)))

    @staticmethod
    def _get_credentials_for_request():
        """
        Only required while the deploy/delete APIs require credentials in the body as well
        as the header. Once the API requirement is relaxed, this function can be stripped out and
        includes for this function removed.
        """
        profile = get_profile_from_context()
        if profile:
            config = ProfileConfigProvider.get_config(profile)
        else:
            config = get_config()
        if not config or not config.is_valid:
            raise InvalidConfigurationError.for_profile(profile)

        if config.is_valid_with_token:
            return {'token': config.token}
        else:
            return {'user': config.username, 'password': config.password}


class LibraryObject(object):
    def __init__(self, lib_type, lib_path):
        self.path = lib_path
        self.lib_type = lib_type

    @classmethod
    def convert_from_libraries(cls, libraries):
        """
        Serialize Libraries into LibraryObjects
        :param libraries: List[Dictionary{String, String}]
        :return: List[LibraryObject]
        """
        lib_objects = []
        for library in libraries:
            for lib_type, path in library.items():
                lib_objects.append(LibraryObject(lib_type, path))
        return lib_objects

    @classmethod
    def convert_to_libraries(cls, lib_objects):
        """
        Deserialize LibraryObjects
        :param lib_objects: List[LibraryObject]
        :return: List[Dictionary{String, String}]
        """
        libraries = []
        for lib_object in lib_objects:
            libraries.append({lib_object.lib_type: lib_object.path})
        return libraries

    def __eq__(self, other):
        if not isinstance(other, LibraryObject):
            return NotImplemented
        return self.path == other.path and self.lib_type == other.lib_type
