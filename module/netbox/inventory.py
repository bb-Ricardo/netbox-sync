
import logging
import json

import pprint

from module.netbox.object_classes import *
from module.common.logging import get_logger

log = get_logger()

class NetBoxInventorySearchResult:
    members = list()

class NetBoxInventory:

    base_structure = dict()
    resolved_dependencies = list()
    
    primary_tag = None

    def __init__(self):
        for object_type in NetBoxObject.__subclasses__():
     
            self.base_structure[object_type.name] = list()
    
    
    def get_by_id(self, object_type, id=None):

        if object_type not in NetBoxObject.__subclasses__():
            raise AttributeError("'%s' object must be a sub class of '%s'." %
                                 (object_type.__name__, NetBoxObject.__name__))
    
        if id is None or self.base_structure[object_type.name] is None:
            return None
                    
        for object in self.base_structure[object_type.name]:
        
            if object.nb_id == id:
                return object


    def get_by_data(self, object_type, data=None):

        if object_type not in NetBoxObject.__subclasses__():
            raise AttributeError("'%s' object must be a sub class of '%s'." %
                                 (object_type.__name__, NetBoxObject.__name__))


        if data is None:
            return None

        if self.base_structure[object_type.name] is None:
            return None
        
        if not isinstance(data, dict):
            # ToDo:
            # * proper handling
            log.error("data is not dict")
            pprint.pprint(data)
            exit(0)
            
        # shortcut if data contains valid id
        data_id = data.get("id")
        if data_id is not None and data_id != 0:
            return self.get_by_id(object_type, id=data_id)

        # try to find by name
        if data.get(object_type.primary_key) is not None:
            object_name_to_find = None
            results = list()
            for object in self.base_structure[object_type.name]:
            
                # Todo:
                #   * try to compare second key if present.
                
                if object_name_to_find is None:
                    object_name_to_find = object.get_display_name(data)
                    #print(f"get_by_data(): Object Display Name: {object_name_to_find}")
                    
                if object_name_to_find == object.get_display_name():
                    results.append(object)
                    
            # found exactly one match
            # ToDo:
            # * add force secondary key if one object has a secondary key
            
            if len(results) == 1:
                #print(f"found exact match: {object_name_to_find}")
                return results[0]
                
            # compare secondary key
            elif len(results) > 1:
            
                object_name_to_find = None
                for object in results:
                
                    if object_name_to_find is None:
                        object_name_to_find = object.get_display_name(data, including_second_key=True)
                        #print(f"get_by_data(): Object Display Name: {object_name_to_find}")
                    
                    if object_name_to_find == object.get_display_name(including_second_key=True):
                        return object

        # try to match all data attributes
        else:
        
            for object in self.base_structure[object_type.name]:
                all_items_match = True
                for attr_name, attr_value in data.items():
            
                    if object.data.get(attr_name) != attr_value:
                        all_items_match = False
                        break
                
                if all_items_match == True:
                    return object

                """
                if data.get(object_type.primary_key) is not None and \
                    object.resolve_attribute(object_type.primary_key) == object.resolve_attribute(object_type.primary_key, data=data):
                   
                    # object type has a secondary key, lets check if it matches
                    if getattr(object_type, "secondary_key", None) is not None and data.get(object_type.secondary_key) is not None:
                   
                        if object.resolve_attribute(object_type.secondary_key) == object.resolve_attribute(object_type.secondary_key, data=data):
                            return_data.append(object)
                    
                    # object has no secondary key but the same name, add to list
                    else:
                        return_data.append(object)
                """
        return None

    def add_item_from_netbox(self, object_type, data=None):
        """
        only to be used if data is read from NetBox and added to inventory
        """
        
        # create new object
        new_object = object_type(data, read_from_netbox=True, inventory=self)
        
        # add to inventory
        self.base_structure[object_type.name].append(new_object)
        
        return

    def add_update_object(self, object_type, data=None, read_from_netbox=False, source=None):
    
        if data is None:
            # ToDo:
            #   * proper error handling
            log.error("NO DATA")
            return

        this_object = self.get_by_data(object_type, data=data)

        if this_object is None:
            this_object = object_type(data, read_from_netbox=read_from_netbox, inventory=self, source=source)
            self.base_structure[object_type.name].append(this_object)
            if read_from_netbox is False:
                log.debug(f"Created new {this_object.name} object: {this_object.get_display_name()}")

        else:
            # ToDo:
            #   * resolve relations if updated from netbox
            this_object.update(data, read_from_netbox=read_from_netbox, source=source)
            log.debug("Updated %s object: %s" % (this_object.name, this_object.get_display_name()))

        return this_object

    def resolve_relations(self):
    
        log.debug("Start resolving relations")
        for object_type in NetBoxObject.__subclasses__():
                
            for object in self.base_structure.get(object_type.name, list()):
                
                object.resolve_relations()
                
        log.debug("Finished resolving relations")
        
    def get_all_items(self, object_type):
    
        if object_type not in NetBoxObject.__subclasses__():
            raise AttributeError("'%s' object must be a sub class of '%s'." %
                                 (object_type.__name__, NetBoxObject.__name__))

        return self.base_structure.get(object_type.name, list())

        
    def tag_all_the_things(self, sources, netbox_handler):

        # ToDo:
        # * DONE: add main tag to all objects retrieved from a source
        # * Done: add source tag all objects of that source
        # * check for orphaned objects
        #   * DONE: objects tagged by a source but not present in source anymore (add)
        #   * DONE: objects tagged as orphaned but are present again (remove)

        source_tags = [x.source_tag for x in sources]
        
        for object_type in NetBoxObject.__subclasses__():
        
            if self.base_structure[object_type.name] is None:
                continue
                
            for object in self.base_structure[object_type.name]:
                
                # if object was found in source
                if object.source is not None:
                    object.add_tags([netbox_handler.primary_tag, object.source.source_tag])
                    
                    # if object was orphaned remove tag again
                    if netbox_handler.orphaned_tag in object.get_tags():
                        object.remove_tags(netbox_handler.orphaned_tag)
                        
                # if object was tagged by a source in previous runs but is not present
                # anymore then add the orphaned tag
                else:
                    for source_tag in source_tags:
                        if source_tag in object.get_tags():
                            object.add_tags(netbox_handler.orphaned_tag)
                
    def to_dict(self):
    
        output = dict()
        for nb_object_class in NetBoxObject.__subclasses__():
        
            output[nb_object_class.name] = list()
            
            for object in self.base_structure[nb_object_class.name]:
                output[nb_object_class.name].append(object.to_dict())
        
        return output

    def __str__(self):
        
        return json.dumps(self.to_dict(), sort_keys=True, indent=4)

# EOF
