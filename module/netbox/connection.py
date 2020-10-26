
import requests
import json
import logging

import pickle

from packaging import version

import pprint

from module import plural
from module.common.misc import grab, do_error_exit, dump
from module.netbox.object_classes import *
from module.common.logging import get_logger

log = get_logger()

# ToDo:
#   * DNS lookup
#   * primary ip
#   * get vrf for IP

class NetBoxHandler:
    """
    Handles NetBox connection state and interaction with API


    """
    minimum_api_version = "2.9"

    # allowed settings and defaults
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

    def __init__(self, cli_args=None, settings=None, inventory=None):

        self.settings = settings
        self.inventory = inventory

        # set primary tag
        setattr(self.inventory, "primary_tag", self.primary_tag)
        
        self.parse_config_settings(settings)

        proto = "https"
        if bool(self.disable_tls) is True:
            proto = "http"

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
            do_error_exit("Config validation failed. Exit!")

        for setting in self.settings.keys():
            setattr(self, setting, config_settings.get(setting))

    def create_session(self):
        """
        Creates a session with NetBox

        :return: `True` if session created else `False`
        :rtype: bool
        """
        header = {"Authorization": f"Token {self.api_token}"}

        session = requests.Session()
        session.headers.update(header)

        log.debug("Created new Session for NetBox.")

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

        log.debug(f"Detected NetBox API v{result}.")

        return result

    def request(self, object_class, req_type="GET", data=None, params=None, nb_id=None):


        result = None

        request_url = f"{self.url}{object_class.api_path}/"

        # append NetBox ID
        if nb_id is not None:
            request_url += f"{nb_id}/"

        if params is None:
            params = dict()
        
        params["limit"] = self.default_netbox_result_limit
            
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
            #""" pagination disabled
            if this_request.method == "GET" and result is not None:
                while response.json().get("next") is not None:
                    this_request.url = response.json().get("next")
                    log.debug2("NetBox results are paginated. Getting next page")

                    response = self.single_request(this_request)
                    result["results"].extend(response.json().get("results"))
            #"""
        elif response.status_code in [201, 204]:

            action = "created" if response.status_code == 201 else "deleted"

            log.info(
                f"NetBox successfully {action} {object_class.name} object '%s'." % (result.get(object_class.primary_key))
            )

        # token issues
        elif response.status_code == 403:

            do_error_exit("NetBox returned: %s: %s" % (response.reason, grab(result, "detail")))

        # we screw up something else
        elif response.status_code >= 400 and response.status_code < 500:

            log.error(f"NetBox returned: {this_request.method} {this_request.path_url} {response.reason}")
            log.debug(f"NetBox returned body: {result}")
            result = None

        elif response.status_code >= 500:

            do_error_exit(f"NetBox returned: {response.status_code} {response.reason}")

        return result

    def single_request(self, this_request):

        req = None

        for _ in range(self.max_retry_attempts):

            log_message = f"Sending {this_request.method} to '{this_request.url}'"

            if this_request.body is not None:
                log_message += f" with data '{this_request.body}'."

                log.debug2(log_message)

            try:
                req = self.session.send(this_request,
                    timeout=self.timeout, verify=self.validate_tls_certs)

            except (ConnectionError, requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout):
                log.warning(f"Request failed, trying again: {log_message}")
                continue
            else:
                break
        else:
            do_error_exit(f"Giving up after {self.max_retry_attempts} retries.")

        log.debug2("Received HTTP Status %s.", req.status_code)
        
        return req

    def query_current_data(self, netbox_objects_to_query=None):

        if netbox_objects_to_query is None:
            raise AttributeError(f"Argument netbox_objects_to_query is: '{netbox_objects_to_query}'")

        # query all dependencies
        for nb_object_class in netbox_objects_to_query:

            if nb_object_class not in NetBoxObject.__subclasses__():
                raise AttributeError(f"Class '{nb_object_class.__name__}' must be a subclass of '{NetBoxObject.__name__}'")
            
            cached_nb_data = None
            try:
                cached_nb_data = pickle.load( open( f"cache/{nb_object_class.__name__}.cache", "rb" ) )
                #pprint.pprint(cached_nb_data)
            except Exception:
                pass
        
            nb_data = dict()
            if cached_nb_data is None:
                # get all objects of this class
                log.debug(f"Requesting {nb_object_class.name}s from NetBox")
                nb_data = self.request(nb_object_class)
                
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

        log.debug("Checking/Adding NetBox Sync dependencies")

        self.inventory.add_update_object(NBTags, data = {
            "name": self.orphaned_tag,
            "color": "607d8b",
            "description": "The source which has previously "
                        "provided the object no longer "
                        "states it exists.{}".format(
                        " An object with the 'Orphaned' tag will "
                        "remain in this state until it ages out "
                        "and is automatically removed."
                        ) if bool(self.settings.get("prune_enabled", False)) else ""
        })

        self.inventory.add_update_object(NBTags, data = {
            "name": self.primary_tag,
            "description": "Created and used by NetBox Sync Script "
                           "to keep track of created items."
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

                    object_type = object.data_model.get(key)

                    if object_type == NBTags:
                        data_to_patch[key] = [{"name": d.get_display_name()} for d in value]

                    elif object_type in NetBoxObject.__subclasses__():
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
                
            elif issued_request is True:
                log.error(f"Request Failed for {nb_object_sub_class.name}. Used data: {data_to_patch}")
                pprint.pprint(object.to_dict())

        # add class to resolved dependencies
        self.inventory.resolved_dependencies = list(set(self.inventory.resolved_dependencies + [nb_object_sub_class] ))

    def update_instance(self):

        log.info("Updating changed data in NetBox")

        # update all items in NetBox accordingly
        for nb_object_sub_class in NetBoxObject.__subclasses__():
            self.update_object(nb_object_sub_class)

        # prune objects
        #self.prune_instance()


        #print(self.inventory)


        return
