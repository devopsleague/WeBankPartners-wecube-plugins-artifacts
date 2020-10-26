# coding=utf-8

from __future__ import absolute_import

import datetime
import hashlib
from logging import root
import os
import logging
import collections
import re
import tempfile
import os.path
from talos.core import config
from talos.utils import http
from talos.core import utils
from talos.core.i18n import _
from talos.utils import scoped_globals

from artifacts_corepy.common import exceptions
from artifacts_corepy.common import nexus
from artifacts_corepy.common import s3
from artifacts_corepy.common import wecmdb
from artifacts_corepy.common import utils as artifact_utils

LOG = logging.getLogger(__name__)
CONF = config.CONF


def is_upload_local_enabled():
    return CONF.wecube.upload_enabled


def is_upload_nexus_enabled():
    return CONF.wecube.upload_nexus_enabled


def calculate_md5(fileobj):
    m = hashlib.md5()
    chunk_size = 64 * 1024
    fileobj.seek(0)
    chunk = fileobj.read(chunk_size)
    while chunk:
        m.update(chunk)
        chunk = fileobj.read(chunk_size)
    return m.hexdigest()


def calculate_file_md5(filepath):
    with open(filepath, 'rb') as fileobj:
        return calculate_md5(fileobj)


class WeCubeResource(object):
    def __init__(self, server=None, token=None):
        self.server = server or CONF.wecube.server
        self.token = token or CONF.wecube.token

    def get_cmdb_client(self):
        return wecmdb.WeCMDBClient(self.server, self.token)

    def list(self, params):
        pass

    def list_by_post(self, filters):
        pass


class SystemDesign(WeCubeResource):
    def list(self, params):
        cmdb_client = self.get_cmdb_client()
        query = {
            "dialect": {
                "showCiHistory": True
            },
            "filters": [{
                "name": "fixed_date",
                "operator": "notNull1",
                "value": None
            }, {
                "name": "fixed_date",
                "operator": "ne",
                "value": ""
            }],
            "paging":
            False,
            "sorting": {
                "asc": False,
                "field": "fixed_date"
            }
        }
        resp_json = cmdb_client.retrieve(CONF.wecube.wecmdb.citypes.system_design, query)
        last_version = collections.OrderedDict()
        for content in resp_json['data']['contents']:
            r_guid = content['data']['r_guid']
            if (r_guid not in last_version):
                last_version[r_guid] = content
        return {
            'data': {
                'contents': list(last_version.values()),
                'pageInfo': None,
            }
        }

    def get(self, rid):
        cmdb_client = self.get_cmdb_client()
        query = {
            "dialect": {
                "showCiHistory": True
            },
            "filters": [{
                "name": "guid",
                "operator": "eq",
                "value": rid
            }],
            "paging": False
        }
        resp_json = cmdb_client.retrieve(CONF.wecube.wecmdb.citypes.system_design, query)
        if not resp_json.get('data', {}).get('contents', []):
            raise exceptions.PluginError(message=_("Can not find ci data for guid [%(rid)s]") % {'rid': rid})
        fixed_date = resp_json['data']['contents'][0]['data']['fixed_date']
        results = []
        if fixed_date:
            query = {"dialect": {"showCiHistory": False}, "filters": [], "paging": False}
            resp_json = cmdb_client.version_tree(CONF.wecube.wecmdb.citypes.system_design,
                                                 CONF.wecube.wecmdb.citypes.unit_design, fixed_date, query)
            for i in resp_json['data']:
                if rid == i.get('data', {}).get('guid', None):
                    results.append(i)
        return results


class SpecialConnector(WeCubeResource):
    def list(self, params):
        cmdb_client = self.get_cmdb_client()
        resp_json = cmdb_client.special_connector()
        return resp_json['data']


class CiTypes(WeCubeResource):
    def list(self, params):
        cmdb_client = self.get_cmdb_client()
        with_attributes = utils.bool_from_string(params.get('with-attributes', 'no'))
        status = params.get('status', '').split(',')
        query = {"filters": [], "paging": False, "refResources": [], "sorting": {"asc": True, "field": "seqNo"}}
        if status:
            query['filters'].append({"name": "status", "operator": "in", "value": status})
        if with_attributes:
            query['refResources'].append('attributes')
            if status:
                query['filters'].append({"name": "attributes.status", "operator": "in", "value": status})
        resp_json = cmdb_client.citypes(query)
        return resp_json['data']['contents']


class EnumCodes(WeCubeResource):
    def list_by_post(self, query):
        cmdb_client = self.get_cmdb_client()
        query.setdefault('filters', [])
        query.setdefault('paging', False)
        query.setdefault('refResources', [])
        query['filters'].append({"name": "cat.catType", "operator": "eq", "value": 1})
        query['refResources'].append('cat')
        query['refResources'].append('cat.catType')
        resp_json = cmdb_client.enumcodes(query)
        return resp_json['data']


class UnitDesignPackages(WeCubeResource):
    def list_by_post(self, query, unit_design_id):
        cmdb_client = self.get_cmdb_client()
        query.setdefault('filters', [])
        query.setdefault('paging', False)
        query['filters'].append({"name": "unit_design", "operator": "eq", "value": unit_design_id})
        resp_json = cmdb_client.retrieve(CONF.wecube.wecmdb.citypes.deploy_package, query)
        for i in resp_json['data']['contents']:
            i['data']['deploy_file_path'] = self.build_file_object(i['data']['deploy_file_path'] or '')
            i['data']['start_file_path'] = self.build_file_object(i['data']['start_file_path'] or '')
            i['data']['stop_file_path'] = self.build_file_object(i['data']['stop_file_path'] or '')
            i['data']['diff_conf_file'] = self.build_file_object(i['data']['diff_conf_file'] or '')
        return resp_json['data']

    def build_file_object(self, filenames, spliter='|'):
        return [{
            'comparisonResult': None,
            'configKeyInfos': [],
            'filename': f,
            'isDir': None,
            'md5': None
        } for f in filenames.split(spliter)]

    def build_local_nexus_path(self, unit_design):
        return unit_design['data']['key_name']

    def get_unit_design_artifact_path(self, unit_design):
        artifact_path = unit_design['data'].get(CONF.wecube.wecmdb.artifact_field, None)
        artifact_path = artifact_path or '/'
        return artifact_path

    def download_url_parse(self, url):
        ret = {}
        results = url.split('/repository/', 1)
        ret['server'] = results[0]
        ret['fullpath'] = '/repository/' + results[1]
        results = results[1].split('/', 1)
        ret['repository'] = results[0]
        ret['filename'] = results[1].split('/')[-1]
        ret['group'] = '/' + results[1].rsplit('/', 1)[0]
        return ret

    def upload(self, filename, filetype, fileobj, unit_design_id):
        if not is_upload_local_enabled():
            raise exceptions.PluginError(message=_("Package uploading is disabled!"))
        cmdb_client = self.get_cmdb_client()
        query = {"filters": [{"name": "guid", "operator": "eq", "value": unit_design_id}], "paging": False}
        resp_json = cmdb_client.retrieve(CONF.wecube.wecmdb.citypes.unit_design, query)
        if not resp_json.get('data', {}).get('contents', []):
            raise exceptions.PluginError(message=_("Can not find ci data for guid [%(rid)s]") % {'rid': unit_design_id})
        unit_design = resp_json['data']['contents'][0]
        nexus_server = None
        if CONF.use_remote_nexus_only:
            nexus_server = CONF.wecube.nexus.server.rstrip('/')
            nexus_client = nexus.NeuxsClient(CONF.wecube.nexus.server, CONF.wecube.nexus.username,
                                             CONF.wecube.nexus.password)
            artifact_path = self.get_unit_design_artifact_path(unit_design)
        else:
            nexus_server = CONF.nexus.server.rstrip('/')
            nexus_client = nexus.NeuxsClient(CONF.nexus.server, CONF.nexus.username, CONF.nexus.password)
            artifact_path = self.build_local_nexus_path(unit_design)
        upload_result = nexus_client.upload(CONF.nexus.repository, artifact_path, filename, filetype, fileobj)
        new_download_url = upload_result['downloadUrl'].replace(nexus_server,
                                                                CONF.wecube.server.rstrip('/') + '/artifacts')
        package_rows = [{
            'name': filename,
            'deploy_package_url': new_download_url,
            'description': filename,
            'md5_value': calculate_md5(fileobj),
            'upload_user': scoped_globals.GLOBALS.request.auth_user,
            'upload_time': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'unit_design': unit_design_id
        }]
        package_result = self.create(package_rows)
        return package_result['data']

    def upload_from_nexus(self, download_url, unit_design_id):
        if not is_upload_nexus_enabled():
            raise exceptions.PluginError(message=_("Package uploading is disabled!"))
        cmdb_client = self.get_cmdb_client()
        query = {"filters": [{"name": "guid", "operator": "eq", "value": unit_design_id}], "paging": False}
        resp_json = cmdb_client.retrieve(CONF.wecube.wecmdb.citypes.unit_design, query)
        if not resp_json.get('data', {}).get('contents', []):
            raise exceptions.PluginError(message=_("Can not find ci data for guid [%(rid)s]") % {'rid': unit_design_id})
        unit_design = resp_json['data']['contents'][0]
        if CONF.use_remote_nexus_only:
            # 更新unit_design.artifact_path && package.create 即上传成功
            url_info = self.download_url_parse(download_url)
            r_nexus_client = nexus.NeuxsClient(CONF.wecube.nexus.server, CONF.wecube.nexus.username,
                                               CONF.wecube.nexus.password)
            nexus_files = r_nexus_client.list(url_info['repository'], url_info['group'])
            nexus_md5 = None
            for f in nexus_files:
                if f['name'] == url_info['filename']:
                    nexus_md5 = f['md5']
            update_unit_design = {}
            update_unit_design['guid'] = unit_design['data']['guid']
            update_unit_design[CONF.wecube.wecmdb.artifact_field] = url_info['group']
            cmdb_client.update(CONF.wecube.wecmdb.citypes.unit_design, [update_unit_design])

            package_rows = [{
                'name': url_info['filename'],
                'deploy_package_url': CONF.wecube.server.rstrip('/') + '/artifacts' + url_info['fullpath'],
                'description': url_info['filename'],
                'md5_value': nexus_md5,
                'upload_user': scoped_globals.GLOBALS.request.auth_user,
                'upload_time': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'unit_design': unit_design_id
            }]
            package_result = self.create(package_rows)
            return package_result['data']
        else:
            # 从本地Nexus下载并上传到远端Nexus中
            l_nexus_client = nexus.NeuxsClient(CONF.nexus.server, CONF.nexus.username, CONF.nexus.password)
            l_artifact_path = self.build_local_nexus_path(unit_design)
            r_nexus_client = nexus.NeuxsClient(CONF.wecube.nexus.server, CONF.wecube.nexus.username,
                                               CONF.wecube.nexus.password)
            with r_nexus_client.download_stream(url=download_url) as resp:
                stream = resp.raw
                chunk_size = 1024 * 1024
                with tempfile.TemporaryFile() as tmp_file:
                    chunk = stream.read(chunk_size)
                    while chunk:
                        tmp_file.write(chunk)
                        chunk = stream.read(chunk_size)
                    tmp_file.seek(0)

                    filetype = resp.headers.get('Content-Type', 'application/octet-stream')
                    fileobj = tmp_file
                    filename = download_url.split('/')[-1]
                    upload_result = l_nexus_client.upload(CONF.nexus.repository, l_artifact_path, filename, filetype,
                                                          fileobj)
                    package_rows = [{
                        'name':
                        filename,
                        'deploy_package_url':
                        upload_result['downloadUrl'].replace(CONF.nexus.server.rstrip('/'),
                                                             CONF.wecube.server.rstrip('/') + '/artifacts'),
                        'description':
                        filename,
                        'md5_value':
                        calculate_md5(fileobj),
                        'upload_user':
                        scoped_globals.GLOBALS.request.auth_user,
                        'upload_time':
                        datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'unit_design':
                        unit_design_id
                    }]
                    package_result = self.create(package_rows)
                    return package_result['data']

    def create(self, data):
        cmdb_client = self.get_cmdb_client()
        return cmdb_client.create(CONF.wecube.wecmdb.citypes.deploy_package, data)

    def get(self, unit_design_id, deploy_package_id):
        cmdb_client = self.get_cmdb_client()
        query = {"filters": [{"name": "guid", "operator": "eq", "value": deploy_package_id}], "paging": False}
        resp_json = cmdb_client.retrieve(CONF.wecube.wecmdb.citypes.deploy_package, query)
        if not resp_json.get('data', {}).get('contents', []):
            raise exceptions.PluginError(message=_("Can not find ci data for guid [%(rid)s]") %
                                         {'rid': deploy_package_id})
        deploy_package = resp_json['data']['contents'][0]
        baseline_package = (deploy_package['data'].get('baseline_package', None) or {})
        empty_query = {"filters": [], "paging": False}
        resp_json = cmdb_client.retrieve(CONF.wecube.wecmdb.citypes.diff_config, empty_query)
        all_diff_configs = resp_json['data']['contents']
        result = {}
        result['packageId'] = deploy_package_id
        result['baseline_package'] = baseline_package

        result['is_decompression'] = utils.bool_from_string(deploy_package['data']['is_decompression'])
        # |切割为列表
        result['deploy_file_path'] = self.build_file_object(deploy_package['data']['deploy_file_path'])
        result['start_file_path'] = self.build_file_object(deploy_package['data']['start_file_path'])
        result['stop_file_path'] = self.build_file_object(deploy_package['data']['stop_file_path'])
        result['diff_conf_file'] = self.build_file_object(deploy_package['data']['diff_conf_file'])
        result['diff_conf_variable'] = deploy_package['data']['diff_conf_variable']
        # 文件对比[same, changed, new, deleted]
        baseline_cached_dir = None
        package_cached_dir = None
        # 确认baselin和package文件已下载并解压缓存在本地(加锁)
        if baseline_package:
            baseline_cached_dir = self.ensure_package_cached(baseline_package['guid'],
                                                             baseline_package['deploy_package_url'])
        package_cached_dir = self.ensure_package_cached(deploy_package['data']['guid'],
                                                        deploy_package['data']['deploy_package_url'])
        # 更新文件的md5,comparisonResult,isDir
        self.update_file_status(baseline_cached_dir, package_cached_dir, result['deploy_file_path'])
        self.update_file_status(baseline_cached_dir, package_cached_dir, result['start_file_path'])
        self.update_file_status(baseline_cached_dir, package_cached_dir, result['stop_file_path'])
        self.update_file_status(baseline_cached_dir, package_cached_dir, result['diff_conf_file'])
        # 更新差异化配置文件的变量列表
        self.update_file_variable(package_cached_dir, result['diff_conf_file'])
        package_diff_configs = []
        for conf_file in result['diff_conf_file']:
            package_diff_configs.extend(conf_file['configKeyInfos'])
        # 更新差异化变量bound/diffConfigGuid/diffExpr/fixedDate/key/type
        result['diff_conf_variable'] = self.update_diff_conf_variable(all_diff_configs, package_diff_configs,
                                                                      result['diff_conf_variable'])

        return result

    def baseline_compare(self, unit_design_id, deploy_package_id, baseline_package_id):
        cmdb_client = self.get_cmdb_client()
        query = {"filters": [{"name": "guid", "operator": "eq", "value": deploy_package_id}], "paging": False}
        resp_json = cmdb_client.retrieve(CONF.wecube.wecmdb.citypes.deploy_package, query)
        if not resp_json.get('data', {}).get('contents', []):
            raise exceptions.PluginError(message=_("Can not find ci data for guid [%(rid)s]") %
                                         {'rid': deploy_package_id})
        deploy_package = resp_json['data']['contents'][0]
        query = {"filters": [{"name": "guid", "operator": "eq", "value": baseline_package_id}], "paging": False}
        resp_json = cmdb_client.retrieve(CONF.wecube.wecmdb.citypes.deploy_package, query)
        if not resp_json.get('data', {}).get('contents', []):
            raise exceptions.PluginError(message=_("Can not find ci data for guid [%(rid)s]") %
                                         {'rid': baseline_package_id})
        baseline_package = resp_json['data']['contents'][0]

        result = {}
        # |切割为列表
        result['deploy_file_path'] = self.build_file_object(baseline_package['data']['deploy_file_path'])
        result['start_file_path'] = self.build_file_object(baseline_package['data']['start_file_path'])
        result['stop_file_path'] = self.build_file_object(baseline_package['data']['stop_file_path'])
        result['diff_conf_file'] = self.build_file_object(baseline_package['data']['diff_conf_file'])
        # 文件对比[same, changed, new, deleted]
        baseline_cached_dir = None
        package_cached_dir = None
        # 确认baselin和package文件已下载并解压缓存在本地(加锁)
        baseline_cached_dir = self.ensure_package_cached(baseline_package['data']['guid'],
                                                         baseline_package['data']['deploy_package_url'])
        package_cached_dir = self.ensure_package_cached(deploy_package['data']['guid'],
                                                        deploy_package['data']['deploy_package_url'])
        # 更新文件的md5,comparisonResult,isDir
        self.update_file_status(baseline_cached_dir, package_cached_dir, result['deploy_file_path'])
        self.update_file_status(baseline_cached_dir, package_cached_dir, result['start_file_path'])
        self.update_file_status(baseline_cached_dir, package_cached_dir, result['stop_file_path'])
        self.update_file_status(baseline_cached_dir, package_cached_dir, result['diff_conf_file'])
        return result

    def update_tree_status(self, baseline_path, package_path, nodes):
        self.update_file_status(baseline_path, package_path, nodes, file_key='name')
        for n in nodes:
            subpath = n['name']
            if n['children'] and n['isDir']:
                self.update_tree_status(None if not baseline_path else os.path.join(baseline_path, subpath),
                                        os.path.join(package_path, subpath), n['children'])

    def filetree(self, unit_design_id, deploy_package_id, baseline_package_id, expand_all, files):
        def _scan_dir(basepath, subpath):
            results = []
            path = os.path.join(basepath, subpath)
            if os.path.exists(path):
                for e in os.scandir(path):
                    results.append({
                        'children': [],
                        'comparisonResult': None,
                        'exists': None,
                        'isDir': e.is_dir(),
                        'md5': None,
                        'name': e.name,
                        'path': e.path[len(basepath) + 1:],
                    })
            return results

        def _add_children_node(filename, subpath, file_list, is_dir=False):
            node = None
            if filename not in [i['name'] for i in file_list]:
                node = {
                    'children': [],
                    'comparisonResult': None,
                    'exists': None,
                    'isDir': None,
                    'md5': None,
                    'name': filename,
                    'path': os.path.join(subpath, filename),
                }
                file_list.append(node)
            else:
                for i in file_list:
                    if filename == i['name']:
                        node = i
            node['isDir'] = is_dir
            return node

        def _generate_tree_from_list(basepath, file_list):
            expanded_dirs = set()
            root_nodes = []
            for f in file_list:
                new_f = f.lstrip('/')
                parts = new_f.split('/')
                # filename on root
                if len(parts) == 1:
                    subpath = ''
                    scan_results = []
                    if (basepath, subpath) not in expanded_dirs:
                        scan_results = _scan_dir(basepath, subpath)
                        expanded_dirs.add((basepath, subpath))
                    root_nodes.extend(scan_results)
                    _add_children_node(parts[0], subpath, root_nodes)
                # ends with a/b/c/
                else:
                    filename = parts.pop(-1)
                    path_nodes = root_nodes
                    subpath = ''
                    for idx in range(len(parts)):
                        # sec protection: you can not list dir out of basepath
                        if parts[idx] not in ('', '.', '..'):
                            scan_results = []
                            if (basepath, subpath) not in expanded_dirs:
                                scan_results = _scan_dir(basepath, subpath)
                                expanded_dirs.add((basepath, subpath))
                            path_nodes.extend(scan_results)
                            node = _add_children_node(parts[idx], subpath, path_nodes, True)
                            path_nodes = node['children']
                            subpath = os.path.join(subpath, parts[idx])
                    scan_results = []
                    if (basepath, subpath) not in expanded_dirs:
                        scan_results = _scan_dir(basepath, subpath)
                        expanded_dirs.add((basepath, subpath))
                    path_nodes.extend(scan_results)
                    if filename:
                        _add_children_node(filename, subpath, path_nodes)
            return root_nodes

        def _get_file_list(baseline_path, package_path, file_list):
            results = []
            for f in file_list:
                new_f = f.lstrip('/')
                parts = new_f.split('/')
                subpath = os.path.join(*[p for p in parts if p not in ('', '.', '..')])
                new_file_list = _scan_dir(package_path, subpath)
                self.update_file_status(None if not baseline_path else os.path.join(baseline_path, subpath),
                                        os.path.join(package_path, subpath),
                                        new_file_list,
                                        file_key='name')
                results.extend(new_file_list)
            return results

        cmdb_client = self.get_cmdb_client()
        query = {"filters": [{"name": "guid", "operator": "eq", "value": deploy_package_id}], "paging": False}
        resp_json = cmdb_client.retrieve(CONF.wecube.wecmdb.citypes.deploy_package, query)
        if not resp_json.get('data', {}).get('contents', []):
            raise exceptions.PluginError(message=_("Can not find ci data for guid [%(rid)s]") %
                                         {'rid': deploy_package_id})
        deploy_package = resp_json['data']['contents'][0]
        baseline_package = None
        if baseline_package_id:
            query = {"filters": [{"name": "guid", "operator": "eq", "value": baseline_package_id}], "paging": False}
            resp_json = cmdb_client.retrieve(CONF.wecube.wecmdb.citypes.deploy_package, query)
            if not resp_json.get('data', {}).get('contents', []):
                raise exceptions.PluginError(message=_("Can not find ci data for guid [%(rid)s]") %
                                             {'rid': baseline_package_id})
            baseline_package = resp_json['data']['contents'][0]
        baseline_cached_dir = None
        package_cached_dir = None
        if baseline_package:
            baseline_cached_dir = self.ensure_package_cached(baseline_package['data']['guid'],
                                                             baseline_package['data']['deploy_package_url'])
        package_cached_dir = self.ensure_package_cached(deploy_package['data']['guid'],
                                                        deploy_package['data']['deploy_package_url'])
        if expand_all:
            expand_tree = _generate_tree_from_list(package_cached_dir, files)
            self.update_tree_status(baseline_cached_dir, package_cached_dir, expand_tree)
            return expand_tree
        else:
            expand_list = _get_file_list(baseline_cached_dir, package_cached_dir, files)
            return expand_list

    def update_file_status(self, baseline_cached_dir, package_cached_dir, files, file_key='filename'):
        '''
        更新文件内容：存在性，md5，文件/目录
        '''
        for i in files:
            b_filepath = os.path.join(baseline_cached_dir, i[file_key]) if baseline_cached_dir else None
            filepath = os.path.join(package_cached_dir, i[file_key])
            b_exists = os.path.exists(b_filepath) if baseline_cached_dir else None
            exists = os.path.exists(filepath)
            b_md5 = None
            md5 = None
            if b_exists:
                i['isDir'] = os.path.isdir(b_filepath)
                if not os.path.isdir(b_filepath):
                    b_md5 = calculate_file_md5(b_filepath)
            if exists:
                i['isDir'] = os.path.isdir(filepath)
                if not i['isDir']:
                    md5 = calculate_file_md5(filepath)
            i['exists'] = exists
            i['md5'] = md5
            # check only baseline_cached_dir is valid
            if baseline_cached_dir:
                # file type
                if not i['isDir']:
                    # same
                    if exists and b_exists and b_md5 == md5:
                        i['comparisonResult'] = 'same'
                    # changed
                    elif exists and b_exists and b_md5 != md5:
                        i['comparisonResult'] = 'changed'
                    # new
                    elif exists and not b_exists:
                        i['comparisonResult'] = 'new'
                    # deleted
                    elif not exists and b_exists:
                        i['comparisonResult'] = 'deleted'
                    else:
                        i['comparisonResult'] = 'deleted'
                else:
                    # dir type
                    # same
                    if exists and b_exists:
                        i['comparisonResult'] = 'same'
                    # new
                    elif exists and not b_exists:
                        i['comparisonResult'] = 'new'
                    # deleted
                    elif not exists and b_exists:
                        i['comparisonResult'] = 'deleted'
                    else:
                        i['comparisonResult'] = 'deleted'
            # fix all not exist to deleted
            if not exists:
                i['comparisonResult'] = 'deleted'
        return files

    def update_file_variable(self, package_cached_dir, files):
        '''
        解析文件差异化变量
        '''
        spliters = [s.strip() for s in CONF.encrypt_variable_prefix.split(',')]
        spliters.extend([s.strip() for s in CONF.file_variable_prefix.split(',')])
        spliters.extend([s.strip() for s in CONF.default_special_replace.split(',')])
        for i in files:
            filepath = os.path.join(package_cached_dir, i['filename'])
            if os.path.exists(filepath):
                with open(filepath, errors='replace') as f:
                    content = f.read()
                    i['configKeyInfos'] = artifact_utils.variable_parse(content, spliters)

    def update_diff_conf_variable(self, all_diff_configs, package_diff_configs, bounded_diff_configs):
        '''
        更新差异化变量绑定内容
        package_diff_configs 是所有差异化变量文件的解析结果列表，元素可以重复
        bounded_diff_configs 是物料包CI中差异化变量字段值（列表）
        '''
        results = []
        finder = artifact_utils.CaseInsensitiveDict()
        p_finder = artifact_utils.CaseInsensitiveDict()
        b_finder = artifact_utils.CaseInsensitiveDict()
        for conf in all_diff_configs:
            finder[conf['data']['variable_name']] = conf
        for pconf in package_diff_configs:
            p_finder[pconf['name']] = pconf
        for bconf in bounded_diff_configs:
            b_finder[bconf['variable_name']] = bconf
        for k, v in p_finder.items():
            conf = finder.get(k, None)
            p_conf = p_finder.get(k, None)
            results.append({
                'bound': k in b_finder,
                'diffConfigGuid': None if conf is None else conf['data']['guid'],
                'diffExpr': None if conf is None else conf['data']['variable_value'],
                'fixedDate': None if conf is None else conf['data']['fixed_date'],
                'key': k if conf is None else conf['data']['variable_name'],
                'type': None if p_conf is None else p_conf['type']
            })
        return results

    def ensure_package_cached(self, guid, url):
        cache_dir = CONF.pakcage_cache_dir
        file_cache_dir = os.path.join(cache_dir, guid)
        with artifact_utils.lock(hashlib.sha1(file_cache_dir.encode()).hexdigest(), timeout=300) as locked:
            if locked:
                if os.path.exists(file_cache_dir):
                    LOG.info('using cache: %s for package: %s', file_cache_dir, guid)
                else:
                    with tempfile.TemporaryDirectory() as download_path:
                        LOG.info('download from: %s for pakcage: %s', url, guid)
                        filepath = self.download_from_url(download_path, url)
                        LOG.info('download complete')
                        LOG.info('unpack package: %s to %s', guid, file_cache_dir)
                        artifact_utils.unpack_file(filepath, file_cache_dir)
                        LOG.info('unpack complete')
            else:
                raise OSError(_('failed to acquire lock, package cache may not be available'))
        return file_cache_dir

    def download_from_url(self, dir_path, url, random_name=False):
        filename = url.rsplit('/', 1)[-1]
        if random_name:
            filename = '%s_%s' % (utils.generate_uuid(), filename)
        filepath = os.path.join(dir_path, filename)
        if url.startswith(CONF.wecube.server):
            # nexus url
            nexus_server = None
            nexus_username = None
            nexus_password = None
            if CONF.use_remote_nexus_only:
                nexus_server = CONF.wecube.nexus.server.rstrip('/')
                nexus_username = CONF.wecube.nexus.username
                nexus_password = CONF.wecube.nexus.password
            else:
                nexus_server = CONF.nexus.server.rstrip('/')
                nexus_username = CONF.nexus.username
                nexus_password = CONF.nexus.password
            # 替换外部下载地址为Nexus内部地址
            new_url = url.replace(CONF.wecube.server.rstrip('/') + '/artifacts', nexus_server)
            client = nexus.NeuxsClient(nexus_server, nexus_username, nexus_password)
            client.download_file(filepath, url=new_url)
        else:
            client = s3.S3Downloader(url)
            client.download_file(filepath, CONF.wecube.s3.access_key, CONF.wecube.s3.secret_key)
        return filepath


class UnitDesignNexusPackages(WeCubeResource):
    def get_unit_design_artifact_path(self, unit_design):
        artifact_path = unit_design['data'].get(CONF.wecube.wecmdb.artifact_field, None)
        artifact_path = artifact_path or '/'
        return artifact_path

    def list(self, params, unit_design_id):
        if not is_upload_nexus_enabled():
            raise exceptions.PluginError(message=_("Package uploading is disabled!"))
        cmdb_client = self.get_cmdb_client()
        query = {"filters": [{"name": "guid", "operator": "eq", "value": unit_design_id}], "paging": False}
        resp_json = cmdb_client.retrieve(CONF.wecube.wecmdb.citypes.unit_design, query)
        if not resp_json.get('data', {}).get('contents', []):
            raise exceptions.PluginError(message=_("Can not find ci data for guid [%(rid)s]") % {'rid': unit_design_id})
        unit_design = resp_json['data']['contents'][0]
        nexus_client = nexus.NeuxsClient(CONF.wecube.nexus.server, CONF.wecube.nexus.username,
                                         CONF.wecube.nexus.password)
        return nexus_client.list(CONF.wecube.nexus.repository,
                                 self.build_artifact_path(unit_design),
                                 extensions=['.zip', '.tar', '.tar.gz', 'tgz', '.jar'])
