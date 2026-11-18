# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
Bulk retrieval of vCenter managed object properties.

pyVmomi requests a property of a managed object from vCenter each time it is read, one SOAP round
trip per read. The PropertyCache retrieves the properties of many objects with one PropertyCollector
request and hands out managed objects which read these properties from the cache instead. Properties
which were not retrieved in advance are requested from vCenter as usual, so reading a property which
is missing from the prefetch list costs time, but never returns wrong data.
"""

from pyVmomi import vim, vmodl, SoapAdapter
from pyVmomi.StubAdapterAccessorImpl import StubAdapterAccessorMixin

from module.common.logging import get_logger

log = get_logger()


class PropertyCache:
    """
    A pyVmomi stub which wraps the stub of the vCenter session.

    Every managed object returned by a request through this stub (the objects of a prefetch, their
    parents, the host of a VM, ...) uses this stub as well. Reading a property of such an object is
    answered from the cache if the property was prefetched for this object.

    Cached values are stored per managed object id under the property path they were requested with.
    pyVmomi reads properties top level only ('config'), a top level path is answered by the stub
    directly. Nested paths ('config.network') are read with get().
    """

    def __init__(self, stub):
        self.stub = stub
        self.cache = dict()

    @classmethod
    def for_stub(cls, stub):
        """
        Return a PropertyCache for the stub of a vCenter session or None if the stub can't be wrapped

        Parameters
        ----------
        stub: pyVmomi stub
            stub of the vCenter session

        Returns
        -------
        PropertyCache, None: the PropertyCache
        """

        # handing out objects bound to the wrapper relies on the 'outerStub' of SoapStubAdapter
        if not isinstance(stub, SoapAdapter.SoapStubAdapter):
            log.debug(f"Unable to retrieve vCenter object properties in bulk with a '{type(stub).__name__}'")
            return None

        return cls(stub)

    def __getattr__(self, name):
        # everything pyVmomi expects from a stub (version, SupportServerGUIDs, ...)
        return getattr(self.stub, name)

    def InvokeMethod(self, mo, info, args):
        """pyVmomi stub interface: invoke a method, returned managed objects are bound to this stub"""

        status, result = self.stub.InvokeMethod(mo, info, args, self)

        if status == 200:
            return result

        if isinstance(result, Exception):
            raise result

        raise Exception(f"vCenter returned status {status} for '{info.wsdlName}': {result}")

    def InvokeAccessor(self, mo, info):
        """pyVmomi stub interface: read a property of a managed object"""

        properties = self.cache.get(mo._moId)

        if properties is not None and info.name in properties:
            return properties[info.name]

        value = StubAdapterAccessorMixin.InvokeAccessor(self, mo, info)

        # remember properties of prefetched objects which were read in addition
        if properties is not None:
            properties[info.name] = value

        return value

    def get(self, obj, path):
        """
        Return a prefetched property of an object

        Parameters
        ----------
        obj: pyVmomi managed object
            object to return the property for
        path: str
            the property path exactly as it was prefetched

        Returns
        -------
        tuple: (bool, value) True and the value if the property was prefetched, (False, None) otherwise
        """

        if getattr(obj, "_stub", None) is not self:
            return False, None

        properties = self.cache.get(obj._moId)

        if properties is None or path not in properties:
            return False, None

        return True, properties[path]

    def prefetch(self, service_content, objects, object_type, paths):
        """
        Retrieve properties of a list of objects from vCenter with one PropertyCollector request

        Parameters
        ----------
        service_content: vim.ServiceInstanceContent
            content of the vCenter session
        objects: list
            managed objects of type 'object_type'
        object_type: type
            the pyVmomi type of the objects
        paths: list
            property paths to retrieve for each object

        Returns
        -------
        list: the objects in the same order, bound to this stub
        """

        if len(objects) == 0:
            return list()

        retrieved = dict()

        # a ListView drops objects which got deleted in the meantime, explicit object specs would fail
        list_view = service_content.viewManager.CreateListView(obj=objects)

        try:
            traversal_spec = vmodl.query.PropertyCollector.TraversalSpec(
                name="traverseList", path="view", skip=False, type=vim.view.ListView)

            filter_spec = vmodl.query.PropertyCollector.FilterSpec(
                objectSet=[vmodl.query.PropertyCollector.ObjectSpec(obj=list_view, skip=True,
                                                                    selectSet=[traversal_spec])],
                propSet=[vmodl.query.PropertyCollector.PropertySpec(type=object_type, pathSet=list(paths))])

            collector = vmodl.query.PropertyCollector(service_content.propertyCollector._moId, self)

            result = collector.RetrievePropertiesEx([filter_spec], vmodl.query.PropertyCollector.RetrieveOptions())

            while result is not None:
                for object_content in result.objects or list():
                    # a property which is unset (or not readable) is not returned, reading it
                    # from vCenter directly yields no value either
                    properties = dict.fromkeys(paths)
                    properties.update({x.name: x.val for x in object_content.propSet or list()})
                    retrieved[object_content.obj._moId] = properties

                result = collector.ContinueRetrievePropertiesEx(result.token) if result.token else None

        finally:
            list_view.Destroy()

        # only now, a request which failed half way through leaves no partial data behind
        self.cache.update(retrieved)

        return [type(x)(x._moId, stub=self, serverGuid=x._serverGuid) for x in objects]

    def evict(self, objects, keep=("name", "parent")):
        """
        Drop prefetched properties of objects which are not needed anymore

        Parameters
        ----------
        objects: list
            managed objects
        keep: tuple
            properties to keep, small ones which are read again for other objects
        """

        for obj in objects:
            properties = self.cache.get(obj._moId)

            if properties is None:
                continue

            for path in list(properties.keys()):
                if path not in keep:
                    del properties[path]

# EOF
