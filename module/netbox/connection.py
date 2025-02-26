# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

import json
import os
import pickle
import pprint
from datetime import datetime
from http.client import HTTPConnection

import urllib3
import requests
from packaging import version

from module.common.logging import get_logger, DEBUG3
from module.common.misc import grab, do_error_exit, plural
from module.netbox import *
from module.netbox.inventory import NetBoxInventory
from module.netbox.config import NetBoxConfig
from module import __version__

log = get_logger()

# test for necessary requests exception class
try:
    from requests.exceptions import JSONDecodeError as RequestsJSONDecodeError
except ImportError:
    log.error(f"Discovered outdated 'requests' version '{requests.__version__}'. Update of virtual environment needed.")
    exit(1)


class NetBoxHandler:
    """
    This class handles all connections to NetBox
    """

    # minimum API version necessary
    minimum_api_version = "2.9"

    # This tag gets added to all objects create/updated/inherited by this program
    primary_tag = primary_tag_name

    # all objects which have a primary tag but not present in any source anymore will get this tag assigned
    orphaned_tag = f"{primary_tag}: Orphaned"

    # cache directory path
    cache_directory = None

    # this is only used to speed up testing, NEVER SET TO True IN PRODUCTION
    testing_cache = False

    # keep track of already resolved dependencies
    resolved_dependencies = set()

    def __init__(self):

        self.settings = NetBoxConfig().parse()
        self.inventory = NetBoxInventory()

        # flood the console
        if log.level == DEBUG3:
            log.warning("Log level is set to DEBUG3, Request logs will only be printed to console")

            HTTPConnection.debuglevel = 1

        proto = "https"
        if bool(self.settings.disable_tls) is True:
            proto = "http"

        # disable TLS insecure warnings if user explicitly switched off validation
        if bool(self.settings.validate_tls_certs) is False:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        port = ""
        if self.settings.port is not None:
            port = f":{self.settings.port}"

        self.url = f"{proto}://{self.settings.host_fqdn}{port}/api/"

        self.session = self.create_session()

        # check for minimum version
        api_version = self.get_api_version()
        if api_version == "None":
            do_error_exit("Unable to determine NetBox version, "
                          "HTTP header 'API-Version' missing.")

        if version.parse(api_version) < version.parse(self.minimum_api_version):
            do_error_exit(f"NetBox API version '{api_version}' not supported. "
                          f"Minimum API version: {self.minimum_api_version}")

        self.inventory.netbox_api_version = api_version

        self.setup_caching()

    def setup_caching(self):
        """
        Validate if all requirements are met to cache NetBox data.
        If a condition fails, caching is switched off.
        """

        if self.settings.use_caching is False:
            return

        cache_folder_name = self.settings.cache_directory_location

        base_dir = os.sep.join(__file__.split(os.sep)[0:-3])
        if cache_folder_name[0] != os.sep:
            cache_folder_name = f"{base_dir}{os.sep}{cache_folder_name}"

        self.cache_directory = os.path.realpath(cache_folder_name)

        # check if directory is a file
        if os.path.isfile(self.cache_directory):
            log.warning(f"The cache directory ({self.cache_directory}) seems to be file.")
            self.settings.use_caching = False

        # check if directory exists
        if not os.path.exists(self.cache_directory):
            # try to create directory
            try:
                os.makedirs(self.cache_directory, 0o700)
            except OSError:
                log.warning(f"Unable to create cache directory: {self.cache_directory}")
                self.settings.use_caching = False
            except Exception as e:
                log.warning(f"Unknown exception while creating cache directory {self.cache_directory}: {e}")
                self.settings.use_caching = False

        # check if directory is writable
        if not os.access(self.cache_directory, os.X_OK | os.W_OK):
            log.warning(f"Error writing to cache directory: {self.cache_directory}")
            self.settings.use_caching = False

        if self.settings.use_caching is False:
            log.warning("NetBox caching DISABLED")
        else:
            log.debug(f"Successfully configured cache directory: {self.cache_directory}")

    def create_session(self) -> requests.Session:
        """
        Create a new NetBox session using api_token

        Returns
        -------
        requests.Session: session handler of new NetBox session
        """

        header = {
            "Authorization": f"Token {self.settings.api_token}",
            "User-Agent": f"netbox-sync/{__version__}",
            "Content-Type": "application/json"
        }

        session = requests.Session()
        session.headers.update(header)

        # adds proxy to the session
        if self.settings.proxy is not None:
            session.proxies.update({
                "http": self.settings.proxy,
                "https": self.settings.proxy
            })

        # adds client cert to session
        if self.settings.client_cert is not None:
            if self.settings.client_cert_key is not None:
                session.cert = (self.settings.client_cert, self.settings.client_cert_key)
            else:
                session.cert = self.settings.client_cert

        log.debug("Created new requests Session for NetBox.")

        return session

    def finish(self):

        # closing NetBox connection
        try:
            self.session.close()
        except Exception as e:
            log.error(f"unable to close NetBox connection: {e}")

    def get_api_version(self):
        """
        Perform a basic GET request to extract NetBox API version from header

        Returns
        -------
        str: NetBox API version
        """
        response = None
        try:
            response = self.session.get(
                self.url,
                timeout=self.settings.timeout,
                verify=self.settings.validate_tls_certs)
        except Exception as e:
            do_error_exit(f"NetBox connection: {e}")

        result = str(response.headers.get("API-Version"))

        log.info(f"Successfully connected to NetBox '{self.settings.host_fqdn}'")
        log.debug(f"Detected NetBox API version: {result}")

        return result

    def request(self, object_class, req_type="GET", data=None, params=None, nb_id=None):
        """
        Perform a NetBox request for a certain object.

        Parameters
        ----------
        object_class: NetBoxObject subclass
            class definition of the desired NetBox object
        req_type: str
            GET, PATCH, PUT, DELETE
        data: dict
            data which shall be sent to NetBox
        params: dict
            dictionary of URL params which should be passed to NetBox
        nb_id: int
            ID of the NetBox object which will be appended to the requested NetBox URL

        Returns
        -------
        dict, bool, None: of returned NetBox data. If object was requested to be deleted, and it was
                            successful then True will be returned. None if request failed or was empty
        """

        result = None

        request_url = f"{self.url}{object_class.api_path}/"

        # append NetBox ID
        if nb_id is not None:
            request_url += f"{nb_id}/"

        if params is not None and not isinstance(params, dict):
            log.debug(f"Params passed to NetBox request need to be a dict, got: {params}")
            params = dict()

        if req_type == "GET":

            if params is None:
                params = dict()

            if "limit" not in params.keys():
                params["limit"] = self.settings.default_netbox_result_limit

            # always exclude config context
            params["exclude"] = "config_context"

        # prepare request
        this_request = self.session.prepare_request(
                            requests.Request(req_type, request_url, params=params, json=data)
                       )

        # issue request
        response = self.single_request(this_request)

        try:
            result = response.json()
        except (json.decoder.JSONDecodeError, RequestsJSONDecodeError):
            pass

        if response.status_code == 200:

            # retrieve paginated results
            if this_request.method == "GET" and result is not None:
                while response.json().get("next") is not None:
                    this_request.url = response.json().get("next")
                    log.debug2("NetBox results are paginated. Getting next page")

                    response = self.single_request(this_request)
                    result["results"].extend(response.json().get("results"))

        elif response.status_code in [201, 204]:

            action = "created" if response.status_code == 201 else "deleted"

            if req_type == "DELETE":
                object_name = self.inventory.get_by_id(object_class, nb_id)
                if object_name is not None:
                    object_name = object_name.get_display_name()
            else:
                object_name = result.get(object_class.primary_key)

            log.info(f"NetBox successfully {action} {object_class.name} object '{object_name}'.")

            if response.status_code == 204:
                result = True

        # token issues
        elif response.status_code == 403:

            do_error_exit("NetBox returned: %s: %s" % (response.reason, grab(result, "detail")))

        # we screw up something else
        elif 400 <= response.status_code < 500:

            log.error(f"NetBox returned: {this_request.method} {this_request.path_url} {response.reason}")
            log.error(f"NetBox returned body: {result}")
            result = None

        elif response.status_code >= 500:

            do_error_exit(f"NetBox returned: {response.status_code} {response.reason}")

        return result

    def single_request(self, this_request):
        """
        Actually perform the request and retry x times if request times out.
        Program will exit if all retries failed!

        Parameters
        ----------
        this_request: requests.session.prepare_request
            object of the prepared request

        Returns
        -------
        requests.Response: response for this request
        """

        response = None

        if log.level == DEBUG3:
            pprint.pprint(vars(this_request))

        for _ in range(self.settings.max_retry_attempts):

            log_message = f"Sending {this_request.method} to '{this_request.url}'"

            if this_request.body is not None:
                log_message += f" with data '{this_request.body}'."

                log.debug2(log_message)

            try:
                response = self.session.send(this_request,
                                             timeout=self.settings.timeout,
                                             verify=self.settings.validate_tls_certs)

            except (ConnectionError, requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout):
                log.warning(f"Request failed, trying again: {log_message}")
                continue
            else:
                break
        else:
            do_error_exit(f"Giving up after {self.settings.max_retry_attempts} retries.")

        log.debug2("Received HTTP Status %s.", response.status_code)

        # print debugging information
        if log.level == DEBUG3:
            log.debug("Response Body:")
            try:
                pprint.pprint(response.json())
            except (json.decoder.JSONDecodeError, RequestsJSONDecodeError) as e:
                log.error(e)

        return response

    def query_current_data(self, netbox_objects_to_query=None):
        """
        Request all current NetBox objects. Use caching whenever possible.
        Objects must provide "last_updated" attribute to support caching for this object type.
        Otherwise, it's not possible to query only changed objects since last run. If attribute is
        not present all objects will be requested (looking at you *Interfaces)

        Parameters
        ----------
        netbox_objects_to_query: list of NetBoxObject subclasses
            NetBox items to query

        """

        if netbox_objects_to_query is None:
            raise AttributeError(f"Attribute netbox_objects_to_query is: '{netbox_objects_to_query}'")

        # try to read cached file witch stores the NetBox version the cache was build upon
        cache_tainted = True
        cached_version_file_name = f"{self.cache_directory}{os.sep}cached_version"
        cached_version = None
        # noinspection PyBroadException
        try:
            with open(cached_version_file_name) as file:
                cached_version = version.parse(file.read())
        except Exception:
            pass

        if cached_version is not None and cached_version == version.parse(self.inventory.netbox_api_version):
            cache_tainted = False
        else:
            if cached_version is not None:
                log.info(f"Cache was build for NetBox version {cached_version} "
                         f"which does not match discovered NetBox version {self.inventory.netbox_api_version}. "
                         f"Rebuilding cache.")

        # query all dependencies
        for nb_object_class in netbox_objects_to_query:

            if nb_object_class not in NetBoxObject.__subclasses__():
                raise AttributeError(f"Class '{nb_object_class.__name__}' must be a "
                                     f"subclass of '{NetBoxObject.__name__}'")

            # if objects are multiple times requested but already retrieved
            if nb_object_class in self.resolved_dependencies:
                continue

            # make sure to query only available object types
            if version.parse(self.inventory.netbox_api_version) < version.parse(nb_object_class.min_netbox_version):
                continue

            # initialize cache variables
            cached_nb_data = list()
            cache_file = f"{self.cache_directory}{os.sep}{nb_object_class.__name__}.cache"
            cache_this_class = False
            latest_update = None

            # check if cache file is accessible
            if self.settings.use_caching is True:
                cache_this_class = True

                if os.path.exists(cache_file) and not os.access(cache_file, os.R_OK):
                    log.warning(f"Got no permission to read existing cache file: {cache_file}")
                    cache_this_class = False

                if os.path.exists(cache_file) and not os.access(cache_file, os.W_OK):
                    log.warning(f"Got no permission to write to existing cache file: {cache_file}")
                    cache_this_class = False

            # read data from cache file
            if cache_this_class is True and cache_tainted is False:
                # noinspection PyBroadException
                try:
                    cached_nb_data = pickle.load(open(cache_file, "rb"))
                except Exception:
                    pass

                if cached_nb_data is None:
                    cached_nb_data = list()

                # get date of latest update in cache file
                if len(cached_nb_data) > 0:
                    latest_update_list = \
                        [x.get("last_updated") for x in cached_nb_data if x.get("last_updated") is not None]

                    if len(latest_update_list) > 0:
                        latest_update = sorted(latest_update_list)[-1]

                        log.debug(f"Successfully read cached data with {len(cached_nb_data)} '{nb_object_class.name}%s'"
                                  f", last updated '{latest_update}'" % plural(len(cached_nb_data)))

                    elif self.testing_cache is False:
                        cache_this_class = False

            if self.testing_cache is True and len(cached_nb_data) > 0:
                for object_data in cached_nb_data:
                    self.inventory.add_object(nb_object_class, data=object_data, read_from_netbox=True)

                # mark this object class as retrieved
                self.resolved_dependencies.add(nb_object_class)

                continue

            full_nb_data = None
            brief_nb_data = None
            updated_nb_data = None

            # no cache data found
            if latest_update is None:

                # get all objects of this class
                log.debug(f"Requesting all {nb_object_class.name}s from NetBox")
                full_nb_data = self.request(nb_object_class)

                if full_nb_data.get("results") is None:
                    log.error(f"Result data from NetBox for object {nb_object_class.__name__} missing!")
                    do_error_exit("Reading data from NetBox failed.")

            else:

                # request a brief list of existing objects
                log.debug(f"Requesting a brief list of {nb_object_class.name}s from NetBox")
                brief_params = {"brief": 1, "limit": 500}
                if version.parse(self.inventory.netbox_api_version) >= version.parse("4.0"):
                    brief_params["fields"] = "id"
                brief_nb_data = self.request(nb_object_class, params=brief_params)
                log.debug("NetBox returned %d results." % len(brief_nb_data.get("results", list())))

                log.debug(f"Requesting the last updates since {latest_update} of {nb_object_class.name}s from NetBox")
                updated_nb_data = self.request(nb_object_class, params={"last_updated__gte": latest_update})
                log.debug("NetBox returned %d results." % len(updated_nb_data.get("results", list())))

                if brief_nb_data.get("results") is None or updated_nb_data.get("results") is None:
                    log.error(f"Result data from NetBox for object {nb_object_class.__name__} missing!")
                    do_error_exit("Reading data from NetBox failed.")

            # read a full set from NetBox
            nb_objects = list()
            if isinstance(full_nb_data, dict):
                nb_objects = full_nb_data.get("results")

            elif self.testing_cache is True:
                nb_objects = cached_nb_data

            # read the delta from NetBox and
            else:

                currently_existing_ids = [x.get("id") for x in brief_nb_data.get("results")]
                changed_ids = [x.get("id") for x in updated_nb_data.get("results")]

                for this_object in cached_nb_data:

                    if this_object.get("id") in currently_existing_ids and this_object.get("id") not in changed_ids:
                        nb_objects.append(this_object)

                nb_objects.extend(updated_nb_data.get("results"))

            if self.settings.use_caching is True:
                try:
                    pickle.dump(nb_objects, open(cache_file, "wb"))
                    if cache_this_class is True:
                        log.debug("Successfully cached %d objects." % (len(nb_objects)))
                except Exception as e:
                    log.warning(f"Failed to write NetBox data to cache file: {e}")

            log.debug(f"Processing %s returned {nb_object_class.name}%s" % (len(nb_objects), plural(len(nb_objects))))

            for object_data in nb_objects:
                self.inventory.add_object(nb_object_class, data=object_data, read_from_netbox=True)

            # mark this object class as retrieved
            self.resolved_dependencies.add(nb_object_class)

        # noinspection PyBroadException
        try:
            with open(cached_version_file_name, "w") as file:
                file.write(self.inventory.netbox_api_version)
        except Exception:
            pass

    def initialize_basic_data(self):
        """
        Adds the two basic tags to keep track of objects and see which
        objects are no longer exists in source to automatically remove them
        """

        log.debug("Checking/Adding NetBox Sync dependencies")

        prune_text = f"Pruning is enabled and Objects will be automatically " \
                     f"removed after {self.settings.prune_delay_in_days} days"

        if self.settings.prune_enabled is False:
            prune_text = f"Objects would be automatically removed after {self.settings.prune_delay_in_days} days " \
                         f"but pruning is currently disabled."

        orphaned_tag_object = self.inventory.add_update_object(NBTag, data={"name": self.orphaned_tag})

        if orphaned_tag_object is not None:
            orphaned_tag_data = {
                "name": self.orphaned_tag,
                "description": "A source which has previously provided this object no "
                               f"longer states it exists. {prune_text}"
            }
            if orphaned_tag_object.is_new is True:
                orphaned_tag_data["color"] = "607d8a"

            orphaned_tag_object.update(data=orphaned_tag_data)

        self.inventory.add_update_object(NBTag, data={
            "name": self.primary_tag,
            "description": "Created and used by NetBox Sync Script to keep track of created items. "
                           "DO NOT change this tag, otherwise syncing can't keep track of deleted objects."
        })

    def update_object(self, nb_object_sub_class, unset=False, last_run=False):
        """
        Iterate over all objects of a certain NetBoxObject subclass and add/update them.
        But first update objects which this object class depends on.
        If some dependencies are unresolvable then these will be removed from the request
        and re added later to the object to try update object in a third run.

        Parameters
        ----------
        nb_object_sub_class: NetBoxObject subclass
            NetBox objects to update
        unset: bool
            True if only unset items should be deleted
        last_run: bool
            True if this will be the last update run. Needed to assign primary_ip4/6 properly

        """

        # make sure to query only available object types
        if version.parse(self.inventory.netbox_api_version) < version.parse(nb_object_sub_class.min_netbox_version):
            return

        for this_object in self.inventory.get_all_items(nb_object_sub_class):

            # unset data if requested
            if unset is True:

                if len(this_object.unset_items) == 0:
                    continue

                unset_data = dict()
                for unset_item in this_object.unset_items:

                    key_data_type = grab(this_object, f"data_model.{unset_item}")
                    if key_data_type in NBObjectList.__subclasses__():
                        unset_data[unset_item] = []
                    else:
                        unset_data[unset_item] = None

                log.info("Updating NetBox '%s' object '%s' with data: %s" %
                         (this_object.name, this_object.get_display_name(), unset_data))

                returned_object_data = self.request(nb_object_sub_class, req_type="PATCH",
                                                    data=unset_data, nb_id=this_object.nb_id)

                if returned_object_data is None:
                    log.error(f"Request Failed for {nb_object_sub_class.name}. Used data: {unset_data}")

                continue

            # resolve dependencies
            for dependency in this_object.get_dependencies():
                if dependency not in self.resolved_dependencies:
                    log.debug2("Resolving dependency: %s" % dependency.name)
                    self.update_object(dependency)

            data_to_patch = dict()
            unresolved_dependency_data = dict()

            for key, value in this_object.data.items():
                if key in this_object.updated_items:

                    if isinstance(value, (NetBoxObject, NBObjectList)):

                        # resolve dependency issues in last run
                        # primary IP always set in last run
                        if value.get_nb_reference() is None or \
                                (key.startswith("primary_ip") and last_run is False) or \
                                (key.startswith("primary_mac_address") and last_run is False):
                            unresolved_dependency_data[key] = value
                        else:
                            data_to_patch[key] = value.get_nb_reference()

                    else:
                        data_to_patch[key] = value

            # special case for IP and MAC addresses
            if isinstance(this_object, (NBIPAddress, NBMACAddress)):
                # if object is new and has no id, then we need to remove assigned_object_type from data_to_patch
                if "assigned_object_id" in unresolved_dependency_data.keys() and \
                        "assigned_object_type" in data_to_patch.keys():
                    del data_to_patch["assigned_object_type"]

            issued_request = False
            returned_object_data = None
            if len(data_to_patch.keys()) > 0:

                # default is a new object
                nb_id = None
                req_type = "POST"
                action = "Creating new"

                # if it's not a new object then update it
                if this_object.is_new is False:
                    nb_id = this_object.nb_id
                    req_type = "PATCH"
                    action = "Updating"

                log.info("%s NetBox '%s' object '%s' with data: %s" %
                         (action, this_object.name, this_object.get_display_name(), data_to_patch))

                returned_object_data = self.request(nb_object_sub_class, req_type=req_type,
                                                    data=data_to_patch, nb_id=nb_id)

                issued_request = True

            if returned_object_data is not None:

                this_object.update(data=returned_object_data, read_from_netbox=True)

                this_object.resolve_relations()

            elif issued_request is True:
                log.error(f"Request Failed for {nb_object_sub_class.name}. Used data: {data_to_patch}")

            # add unresolved dependencies back to object
            if len(unresolved_dependency_data.keys()) > 0:
                log.debug2("Adding unresolved dependencies back to object: %s" %
                           list(unresolved_dependency_data.keys()))
                this_object.update(data=unresolved_dependency_data)

            this_object.resolve_relations()

            if last_run is True and getattr(this_object, "deleted", False) is True:
                self.request(nb_object_sub_class, req_type="DELETE", nb_id=this_object.nb_id)

        # add class to resolved dependencies
        self.resolved_dependencies.add(nb_object_sub_class)

    def update_instance(self):
        """
        Add/Update all items in local inventory to NetBox in three runs.

        1. update all objects with "unset_attributes"
        2. regular run to add update objects
        3. update all objects with unresolved dependencies in previous runs

        At the end check if any unresolved dependencies are still left
        """

        log.info("Updating changed data in NetBox")

        # update all items in NetBox but unset items first
        log.debug("First run, unset attributes if necessary.")
        self.resolved_dependencies = set()
        for nb_object_sub_class in NetBoxObject.__subclasses__():
            self.update_object(nb_object_sub_class, unset=True)

        # update all items
        log.debug("Second run, update all items")
        self.resolved_dependencies = set()
        for nb_object_sub_class in NetBoxObject.__subclasses__():
            self.update_object(nb_object_sub_class)

        # run again to updated objects with previous unresolved dependencies
        log.debug("Third run, update all items with previous unresolved items")
        self.resolved_dependencies = set()
        for nb_object_sub_class in NetBoxObject.__subclasses__():
            self.update_object(nb_object_sub_class, last_run=True)

        # check that all updated items are resolved relations
        for nb_object_sub_class in NetBoxObject.__subclasses__():
            for this_object in self.inventory.get_all_items(nb_object_sub_class):
                for key, value in this_object.data.items():
                    if key in this_object.updated_items:

                        if isinstance(value, (NetBoxObject, NBObjectList)) and value.get_nb_reference() is None:
                            log.error(f"Unfortunately updated item {key} for object "
                                      f"{this_object.get_display_name()} could not be fully resolved: {repr(value)}")

    def prune_data(self):
        """
        Prune objects in NetBox if they are no longer present in any source.
        First they will be marked as Orphaned and after X days they will be
        deleted from NetBox.
        """

        if self.settings.prune_enabled is False:
            log.debug("Pruning disabled. Skipping")
            return

        log.info("Pruning orphaned data in NetBox")

        disabled_sources_tags = \
            [x.source_tag for x in self.inventory.source_list if grab(x, "settings.enabled", fallback=False) is False]

        # update all items in NetBox accordingly
        today = datetime.now()
        for nb_object_sub_class in reversed(NetBoxObject.__subclasses__()):

            if getattr(nb_object_sub_class, "prune", False) is False:
                continue

            for this_object in self.inventory.get_all_items(nb_object_sub_class):

                if this_object.source is not None:
                    continue

                this_object_tags = this_object.get_tags()

                if self.orphaned_tag not in this_object_tags:
                    continue

                date_last_update = grab(this_object, "data.last_updated")

                if date_last_update is None:
                    continue

                if bool(set(this_object_tags).intersection(disabled_sources_tags)) is True:
                    log.debug2(f"Object '{this_object.get_display_name()}' was added "
                               f"from a currently disabled source. Skipping pruning.")
                    continue

                # already deleted
                if getattr(this_object, "deleted", False) is True:
                    continue

                # only need the date including seconds
                date_last_update = date_last_update[0:19]

                log.debug2(f"Object '{this_object.name}' '{this_object.get_display_name()}' is Orphaned. "
                           f"Last time changed: {date_last_update}")

                # check prune delay.
                # noinspection PyBroadException
                try:
                    last_updated = datetime.strptime(date_last_update, "%Y-%m-%dT%H:%M:%S")
                except Exception:
                    continue

                days_since_last_update = (today - last_updated).days

                # it seems we need to delete this object
                if last_updated is not None and days_since_last_update >= self.settings.prune_delay_in_days:

                    log.info(f"{nb_object_sub_class.name.capitalize()} '{this_object.get_display_name()}' is orphaned "
                             f"for {days_since_last_update} days and will be deleted.")

                    # delete device/VM interfaces first. interfaces have no last_updated attribute
                    if isinstance(this_object, (NBVM, NBDevice)):

                        log.info(f"Before the '{this_object.name}' can be deleted, all interfaces must be deleted.")

                        for object_interface in self.inventory.get_all_interfaces(this_object):

                            # already deleted
                            if getattr(object_interface, "deleted", False) is True:
                                continue

                            log.info(f"Deleting interface '{object_interface.get_display_name()}'")

                            ret = self.request(object_interface.__class__, req_type="DELETE",
                                               nb_id=object_interface.nb_id)

                            if ret is True:
                                object_interface.deleted = True

                    ret = self.request(nb_object_sub_class, req_type="DELETE", nb_id=this_object.nb_id)

                    if ret is True:
                        this_object.deleted = True

        return

    def just_delete_all_the_things(self):
        """
        Using a brute force approach. Try to delete everything which is tagged
        with the primary tag (NetBox: Synced) 10 times.
        This way we don't need to care about dependencies.
        """

        log.info("Querying necessary objects from NetBox. This might take a while.")
        self.query_current_data(NetBoxObject.__subclasses__())
        log.info("Finished querying necessary objects from NetBox")

        self.inventory.resolve_relations()

        log.warning(f"Starting purge now. All objects with the tag '{self.primary_tag}' will be deleted!!!")

        for iteration in range(10):

            log.debug("Iteration %d trying to deleted all the objects." % (iteration + 1))

            found_objects_to_delete = False

            for nb_object_sub_class in reversed(NetBoxObject.__subclasses__()):

                if getattr(nb_object_sub_class, "prune", False) is False:
                    continue

                # tags need to be deleted at the end
                if nb_object_sub_class == NBTag:
                    continue

                for this_object in self.inventory.get_all_items(nb_object_sub_class):

                    # already deleted
                    if getattr(this_object, "deleted", False) is True:
                        continue

                    found_objects_to_delete = True

                    if self.primary_tag in this_object.get_tags():
                        log.info(f"{nb_object_sub_class.name} '{this_object.get_display_name()}' will be deleted now")

                        result = self.request(nb_object_sub_class, req_type="DELETE", nb_id=this_object.nb_id)

                        if result is not None:
                            this_object.deleted = True

            if found_objects_to_delete is False:

                # get tag objects
                primary_tag = self.inventory.get_by_data(NBTag, data={"name": self.primary_tag})
                orphaned_tag = self.inventory.get_by_data(NBTag, data={"name": self.orphaned_tag})

                # try to delete them
                log.info(f"{NBTag.name} '{primary_tag.get_display_name()}' will be deleted now")
                self.request(NBTag, req_type="DELETE", nb_id=primary_tag.nb_id)

                log.info(f"{NBTag.name} '{orphaned_tag.get_display_name()}' will be deleted now")
                self.request(NBTag, req_type="DELETE", nb_id=orphaned_tag.nb_id)

                log.info("Successfully deleted all objects which were synced and tagged by this program.")
                break
        else:

            log.warning("Unfortunately we were not able to delete all objects. Sorry")

        return

    def delete_unused_tags(self):
        """
        deletes all tags with primary tag in description and the attribute 'tagged_items' returned 0
        """

        self.query_current_data([NBTag])

        for this_tag in self.inventory.get_all_items(NBTag):

            tag_description = grab(this_tag, "data.description")
            tag_tagged_items = grab(this_tag, "data.tagged_items")

            if tag_description is None or not tag_description.startswith(self.primary_tag):
                continue

            if tag_tagged_items is None or tag_tagged_items != 0 or this_tag.used is True:
                continue

            log.info(f"Deleting unused tag '{this_tag.get_display_name()}'")
            self.request(NBTag, req_type="DELETE", nb_id=this_tag.nb_id)

# EOF
