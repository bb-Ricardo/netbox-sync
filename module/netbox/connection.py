
import json
from datetime import datetime
import requests
from http.client import HTTPConnection
import urllib3
import pickle

from packaging import version

import pprint

from module import plural
from module.common.misc import grab, do_error_exit, dump
from module.netbox.object_classes import *
from module.common.logging import get_logger, DEBUG3

log = get_logger()

# ToDo:
#   * primary ip

class NetBoxHandler:
    """
    Handles NetBox connection state and interaction with API


    """
    minimum_api_version = "2.9"

    # permitted settings and defaults
    settings = {
        "api_token": None,
        "host_fqdn": None,
        "port": None,
        "disable_tls": False,
        "validate_tls_certs": True,
        "prune_enabled": False,
        "prune_delay_in_days": 30,
        "default_netbox_result_limit": 200,
        "timeout": 30,
        "max_retry_attempts": 4
    }

    primary_tag = "NetBox-synced"
    orphaned_tag = f"{primary_tag}: Orphaned"

    inventory = None

    instance_tags = None
    instance_interfaces = {}
    instance_virtual_interfaces = {}

    # testing option
    use_netbox_caching_for_testing = False

    def __init__(self, settings=None, inventory=None):

        self.settings = settings
        self.inventory = inventory

        # set primary tag
        setattr(self.inventory, "primary_tag", self.primary_tag)

        self.parse_config_settings(settings)

        # flood the console
        if log.level == DEBUG3:
            log.warning("Log level is set to DEBUG3, Request logs will only be printed to console")

            HTTPConnection.debuglevel = 1

        proto = "https"
        if bool(self.disable_tls) is True:
            proto = "http"

        # disable TLS insecure warnings if user explicitly switched off validation
        if bool(self.validate_tls_certs) is False:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        port = ""
        if self.port is not None:
            port = f":{self.port}"

        self.url = f"{proto}://{self.host_fqdn}{port}/api/"

        self.session = self.create_session()

        # check for minimum version
        if version.parse(self.get_api_version()) < version.parse(self.minimum_api_version):
            do_error_exit(f"Netbox API version '{self.api_version}' not supported. "
                          f"Minimum API version: {self.minimum_api_version}")

    def parse_config_settings(self, config_settings):

        validation_failed = False
        for setting in ["host_fqdn", "api_token"]:
            if config_settings.get(setting) is None:
                log.error(f"Config option '{setting}' in 'netbox' can't be empty/undefined")
                validation_failed = True

        for setting in ["prune_delay_in_days", "default_netbox_result_limit", "timeout", "max_retry_attempts"]:
            if not isinstance(config_settings.get(setting), int):
                log.error(f"Config option '{setting}' in 'netbox' must be an integer.")
                validation_failed = True

        if validation_failed is True:
            log.error("Config validation failed. Exit!")
            exit(1)

        for setting in self.settings.keys():
            setattr(self, setting, config_settings.get(setting))

    def create_session(self):
        """
        Creates a session with NetBox

        :return: `True` if session created else `False`
        :rtype: bool
        """
        header = {
            "Authorization": f"Token {self.api_token}",
            "User-Agent": "netbox-sync/0.0.1"
        }

        session = requests.Session()
        session.headers.update(header)

        log.debug("Created new requests Session for NetBox.")

        return session

    def get_api_version(self):
        """
        Determines the current NetBox API Version

        :return: NetBox API version
        :rtype: float
        """
        response = None
        try:
            response = self.session.get(
                self.url,
                timeout=self.timeout,
                verify=self.validate_tls_certs)
        except Exception as e:
            do_error_exit(str(e))

        result = str(response.headers["API-Version"])

        log.info(f"Successfully connected to NetBox '{self.host_fqdn}'")
        log.debug(f"Detected NetBox API version: {result}")

        return result

    def request(self, object_class, req_type="GET", data=None, params=None, nb_id=None):


        result = None

        request_url = f"{self.url}{object_class.api_path}/"

        # append NetBox ID
        if nb_id is not None:
            request_url += f"{nb_id}/"

        if params is None:
            params = dict()

        if req_type == "GET":
            params["limit"] = self.default_netbox_result_limit
            params["exclude"] = "config_context"

        # prepare request
        this_request = self.session.prepare_request(
                            requests.Request(req_type, request_url, params=params, json=data)
                       )

        # issue request
        response = self.single_request(this_request)


        try:
            result = response.json()
        except json.decoder.JSONDecodeError:
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

            object_name = None
            if req_type == "DELETE":
                object_name = self.inventory.get_by_id(object_class, nb_id)
                if object_name is not None:
                    object_name = object_name.get_display_name()
            else:
                object_name = result.get(object_class.primary_key)

            log.info(f"NetBox successfully {action} {object_class.name} object '{object_name}'.")

        # token issues
        elif response.status_code == 403:

            do_error_exit("NetBox returned: %s: %s" % (response.reason, grab(result, "detail")))

        # we screw up something else
        elif response.status_code >= 400 and response.status_code < 500:

            log.error(f"NetBox returned: {this_request.method} {this_request.path_url} {response.reason}")
            log.error(f"NetBox returned body: {result}")
            result = None

        elif response.status_code >= 500:

            do_error_exit(f"NetBox returned: {response.status_code} {response.reason}")

        return result

    def single_request(self, this_request):

        response = None

        if log.level == DEBUG3:
            pprint.pprint(vars(this_request))

        for _ in range(self.max_retry_attempts):

            log_message = f"Sending {this_request.method} to '{this_request.url}'"

            if this_request.body is not None:
                log_message += f" with data '{this_request.body}'."

                log.debug2(log_message)

            try:
                response = self.session.send(this_request,
                    timeout=self.timeout, verify=self.validate_tls_certs)

            except (ConnectionError, requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout):
                log.warning(f"Request failed, trying again: {log_message}")
                continue
            else:
                break
        else:
            do_error_exit(f"Giving up after {self.max_retry_attempts} retries.")

        log.debug2("Received HTTP Status %s.", response.status_code)

        # print debugging information
        if log.level == DEBUG3:
            log.debug("Response Body:")
            try:
                pprint.pprint(response.json())
            except json.decoder.JSONDecodeError as e:
                log.error(e)

        return response

    def query_current_data(self, netbox_objects_to_query=None):

        if netbox_objects_to_query is None:
            raise AttributeError(f"Attribute netbox_objects_to_query is: '{netbox_objects_to_query}'")

        # query all dependencies
        for nb_object_class in netbox_objects_to_query:

            if nb_object_class not in NetBoxObject.__subclasses__():
                raise AttributeError(f"Class '{nb_object_class.__name__}' must be a subclass of '{NetBoxObject.__name__}'")

            cached_nb_data = None
            if self.use_netbox_caching_for_testing is True:
                try:
                    cached_nb_data = pickle.load( open( f"cache/{nb_object_class.__name__}.cache", "rb" ) )
                except Exception:
                    pass

            nb_data = dict()
            if cached_nb_data is None:
                # get all objects of this class
                log.debug(f"Requesting {nb_object_class.name}s from NetBox")
                nb_data = self.request(nb_object_class)

                if self.use_netbox_caching_for_testing is True:
                    pickle.dump(nb_data.get("results"), open( f"cache/{nb_object_class.__name__}.cache", "wb" ) )
            else:
                nb_data["results"] = cached_nb_data


            if nb_data.get("results") is None:
                log.warning(f"Result data from NetBox for object {nb_object_class.__name__} missing!")
                continue

            log.debug(f"Processing %s returned {nb_object_class.name}%s" % (len(nb_data.get("results")),plural(len(nb_data.get("results")))))

            for object_data in nb_data.get("results"):
                self.inventory.add_item_from_netbox(nb_object_class, data=object_data)

        return

    def inizialize_basic_data(self):
        """
            Adds the two basic tags to keep track of objects and see which
            objects are no longer exists in source to automatically remove them
        """

        log.debug("Checking/Adding NetBox Sync dependencies")

        prune_text = f"Pruning is enabled and Objects will be automatically removed after {self.prune_delay_in_days} days"

        if self.prune_enabled is False:
            prune_text = f"Objects would be automatically removed after {self.prune_delay_in_days} days but pruning is currently disabled."

        self.inventory.add_update_object(NBTags, data = {
            "name": self.orphaned_tag,
            "color": "607d8b",
            "description": "A source which has previously provided this object no "
                          f"longer states it exists. {prune_text}"
        })

        self.inventory.add_update_object(NBTags, data = {
            "name": self.primary_tag,
            "description": "Created and used by NetBox Sync Script to keep track of created items. "
                           "DO NOT change this tag, otherwise syncing can't keep track of deleted objects."
        })

    def update_object(self, nb_object_sub_class):

        for object in self.inventory.get_all_items(nb_object_sub_class):

            # resolve dependencies
            for dependency in object.get_dependencies():
                if dependency not in self.inventory.resolved_dependencies:
                    log.debug2("Resolving dependency: %s" % (dependency.name))
                    self.update_object(dependency)


            returned_object_data = None

            patch_issue = False
            data_to_patch = dict()

            if object.is_new is True:
                object.updated_items = object.data.keys()

            for key, value in object.data.items():
                if key in object.updated_items:

                    if key == "tags":
                        data_to_patch[key] = [{"name": d.get_display_name()} for d in value]

                    elif isinstance(value, NetBoxObject):
                        data_to_patch[key] = value.get_nb_reference()

                        if value.nb_id == 0:
                            log.error(f"Unable to find a NetBox reference to {value.name} '{value.get_display_name()}'. Might be a dependency issue.")
                            patch_issue = True

                    else:
                        data_to_patch[key] = value

            if patch_issue == True:
                continue

            issued_request = False
            if object.is_new is True:
                log.info("Creating new NetBox '%s' object: %s" % (object.name, object.get_display_name()))

                returned_object_data = self.request(nb_object_sub_class, req_type="POST", data=data_to_patch)

                issued_request = True

            if object.is_new is False and len(object.updated_items) > 0:

                log.info("Updating NetBox '%s' object '%s' with data: %s" % (object.name, object.get_display_name(), data_to_patch))

                returned_object_data = self.request(nb_object_sub_class, req_type="PATCH", data=data_to_patch, nb_id=object.nb_id)

                issued_request = True

            if returned_object_data is not None:

                object.update(data = returned_object_data, read_from_netbox=True)
                object.resolve_relations()

            elif issued_request is True:
                log.error(f"Request Failed for {nb_object_sub_class.name}. Used data: {data_to_patch}")

        # add class to resolved dependencies
        self.inventory.resolved_dependencies = list(set(self.inventory.resolved_dependencies + [nb_object_sub_class] ))

    def update_instance(self):

        log.info("Updating changed data in NetBox")

        # update all items in NetBox accordingly
        for nb_object_sub_class in NetBoxObject.__subclasses__():
            self.update_object(nb_object_sub_class)

    def prune_data(self):

        if self.prune_enabled == False:
            log.debug("Pruning disabled. Skipping")
            return

        log.info("Pruning orphaned data in NetBox")

        # update all items in NetBox accordingly
        today = datetime.now()
        for nb_object_sub_class in reversed(self.inventory.resolved_dependencies):

            for object in self.inventory.get_all_items(nb_object_sub_class):

                if object.source is not None:
                    continue

                if self.orphaned_tag not in object.get_tags():
                    continue

                date_last_update = grab(object, "data.last_updated")

                if date_last_update is None:
                    continue

                # only need the date including seconds
                date_last_update = date_last_update[0:19]

                log.debug2(f"Object '{object.get_display_name()}' is Orphaned. Last time changed: {date_last_update}")

                # check prune delay.
                last_updated = None
                try:
                    last_updated = datetime.strptime(date_last_update,"%Y-%m-%dT%H:%M:%S")
                except Exception:
                    continue

                days_since_last_update = (today - last_updated).days

                # it seems we need to delete this object
                if last_updated is not None and days_since_last_update >= self.prune_delay_in_days:

                    log.info(f"{nb_object_sub_class.name.capitalize()} '{object.get_display_name()}' is orphaned for {days_since_last_update} days and will be deleted.")

                    self.request(nb_object_sub_class, req_type="DELETE", nb_id=object.nb_id)

        return

    def just_delete_all_the_things(self):
        """
        Using a brute force approach. Try to delete everything 10 times.
        This way we don't need to care about dependencies.
        """

        log.info("Querying necessary objects from Netbox. This might take a while.")
        self.query_current_data(NetBoxObject.__subclasses__())
        log.info("Finished querying necessary objects from Netbox")

        self.inventory.resolve_relations()

        log.warning(f"Starting purge now. All objects with the tag '{self.primary_tag}' will be deleted!!!")

        for iteration in range(10):

            log.debug("Iteration %d trying to deleted all the objects." % (iteration + 1))

            found_objects_to_delete = False

            for nb_object_sub_class in reversed(NetBoxObject.__subclasses__()):

                # tags need to be deleted at the end
                if nb_object_sub_class == NBTags:
                    continue

                # object has no tags so we can't be sure it was created with this tool
                if NBTags not in nb_object_sub_class.data_model.values():
                    continue

                for object in self.inventory.get_all_items(nb_object_sub_class):

                    # already deleted
                    if getattr(object, "deleted", False) is True:
                        continue


                    found_objects_to_delete = True

                    if self.primary_tag in object.get_tags():
                        log.info(f"{nb_object_sub_class.name} '{object.get_display_name()}' will be deleted now")

                        """
                        # Todo:
                        # * Needs testing
                        result = self.request(nb_object_sub_class, req_type="DELETE", nb_id=object.nb_id)

                        if result is not None:
                            object.deleted = True
                        """


            if found_objects_to_delete is False:

                # get tag objects
                primary_tag = self.inventory.add_update_object(NBTags, data = {"name": self.primary_tag})
                orpahned_tag = self.inventory.get_by_data(NBTags, data = {"name": self.orphaned_tag})

                # try to delete them
                log.info(f"{NBTags.name} '{primary_tag.get_display_name()}' will be deleted now")
                #self.request(NBTags, req_type="DELETE", nb_id=primary_tag.nb_id)

                log.info(f"{NBTags.name} '{orpahned_tag.get_display_name()}' will be deleted now")
                #self.request(NBTags, req_type="DELETE", nb_id=orpahned_tag.nb_id)

                log.info("Successfully deleted all objects which were sync by this program.")
                break
        else:

            log.warning("Unfortunately we were not able to delete all objects. Sorry")

        return
# EOF
