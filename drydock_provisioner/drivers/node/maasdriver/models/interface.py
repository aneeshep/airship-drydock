# Copyright 2017 AT&T Intellectual Property.  All other rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging

import drydock_provisioner.drivers.node.maasdriver.models.base as model_base
import drydock_provisioner.drivers.node.maasdriver.models.fabric as maas_fabric
import drydock_provisioner.drivers.node.maasdriver.models.subnet as maas_subnet
import drydock_provisioner.drivers.node.maasdriver.models.vlan as maas_vlan

import drydock_provisioner.error as errors

class Interface(model_base.ResourceBase):

    resource_url = 'nodes/{system_id}/interfaces/{resource_id}/'
    fields = ['resource_id', 'system_id', 'name', 'type', 'mac_address', 'vlan',
              'links', 'effective_mtu', 'fabric_id']
    json_fields = ['name', 'type', 'mac_address', 'vlan', 'links', 'effective_mtu']

    def __init__(self, api_client, **kwargs):
        super(Interface, self).__init__(api_client, **kwargs)
        self.logger = logging.getLogger('drydock.nodedriver.maasdriver')

    def attach_fabric(self, fabric_id=None, fabric_name=None):
        """
        Attach this interface to a MaaS fabric. Only one of fabric_id
        or fabric_name should be specified. If both are, fabric_id rules

        :param fabric_id: The MaaS resource ID of a network Fabric to connect to
        :param fabric_name: The name of a MaaS fabric to connect to
        """

        fabric = None
        
        fabrics = maas_fabric.Fabrics(self.api_client)
        fabrics.refresh()

        if fabric_id is not None:
            fabric = fabrics.select(fabric_id)
        elif fabric_name is not None:
            fabric = fabrics.singleton({'name': fabric_name})
        else:
            self.logger.warning("Must specify fabric_id or fabric_name")
            raise ValueError("Must specify fabric_id or fabric_name")

        if fabric is None:
            self.logger.warning("Fabric not found in MaaS for fabric_id %s, fabric_name %s" %
                                 (fabric_id, fabric_name))
            raise errors.DriverError("Fabric not found in MaaS for fabric_id %s, fabric_name %s" %
                                    (fabric_id, fabric_name))

        # Locate the untagged VLAN for this fabric.
        fabric_vlan = fabric.vlans.singleton({'vid': 0})

        if fabric_vlan is None:
            self.logger.warning("Cannot locate untagged VLAN on fabric %s" % (fabric_id))
            raise errors.DriverError("Cannot locate untagged VLAN on fabric %s" % (fabric_id))

        self.vlan = fabric_vlan.resource_id
        self.logger.info("Attaching interface %s on system %s to VLAN %s on fabric %s" %
                         (self.resource_id, self.system_id, fabric_vlan.resource_id, fabric.resource_id))
        self.update()

    def is_linked(self, subnet_id):
        for l in self.links:
            if l.get('subnet_id', None) == subnet_id:
                return True

        return False

    def link_subnet(self, subnet_id=None, subnet_cidr=None, ip_address=None, primary=False):
        """
        Link this interface to a MaaS subnet. One of subnet_id or subnet_cidr
        should be specified. If both are, subnet_id rules.

        :param subnet_id: The MaaS resource ID of a network subnet to connect to
        :param subnet_cidr: The CIDR of a MaaS subnet to connect to
        :param ip_address: The IP address to assign this interface. Should be a string with
                           a static IP or None. If None, DHCP will be used.
        :param primary: Boolean of whether this interface is the primary interface of the node. This
                        sets the node default gateway to the gateway of the subnet
        """

        subnet = None

        subnets = maas_subnet.Subnets(self.api_client)
        subnets.refresh()

        if subnet_id is not None:
            subnet = subnets.select(subnet_id)
        elif subnet_cidr is not None:
            subnet = subnets.singleton({'cidr': subnet_cidr})
        else:
            self.logger.warning("Must specify subnet_id or subnet_cidr")
            raise ValueError("Must specify subnet_id or subnet_cidr")

        if subnet is None:
            self.logger.warning("Subnet not found in MaaS for subnet_id %s, subnet_cidr %s" %
                                 (subnet_id, subnet_cidr))
            raise errors.DriverError("Subnet not found in MaaS for subnet_id %s, subnet_cidr %s" %
                                 (subnet_id, subnet_cidr))

        # TODO Possibly add logic to true up link attributes, may be overkill
        if self.is_linked(subnet.resource_id):
            self.logger.info("Interface %s already linked to subnet %s, skipping." %
                            (self.resource_id, subnet.resource_id))
            return

        url = self.interpolate_url()

        # TODO Probably need to enumerate link mode
        options = { 'subnet': subnet.resource_id,
                    'mode': 'dhcp' if ip_address is None else 'static',
                    'default_gateway': primary,
                  }

        if ip_address is not None:
            options['ip_address'] = ip_address

        self.logger.debug("Linking interface %s to subnet: subnet=%s, mode=%s, address=%s, primary=%s" %
                          (self.resource_id, subnet.resource_id, options['mode'], ip_address, primary))
        
        resp = self.api_client.post(url, op='link_subnet', files=options)

        if not resp.ok:
            self.logger.error("Error linking interface %s to subnet %s - MaaS response %s: %s" %
                              (self.resouce_id, subnet.resource_id, resp.status_code, resp.text))
            raise errors.DriverError("Error linking interface %s to subnet %s - MaaS response %s" %
                              (self.resouce_id, subnet.resource_id, resp.status_code))

        self.refresh()

        return

    @classmethod
    def from_dict(cls, api_client, obj_dict):
        """
        Because MaaS decides to replace the resource ids with the
        representation of the resource, we must reverse it for a true
        representation of the Interface
        """
        refined_dict = {k: obj_dict.get(k, None) for k in cls.fields}
        if 'id' in obj_dict.keys():
            refined_dict['resource_id'] = obj_dict.get('id')

        if isinstance(refined_dict.get('vlan', None), dict):
            refined_dict['fabric_id'] = refined_dict['vlan']['fabric_id']
            refined_dict['vlan'] = refined_dict['vlan']['id']
        
        link_list = []
        if isinstance(refined_dict.get('links', None), list):
            for l in refined_dict['links']:
                if isinstance(l, dict):
                    link = { 'resource_id': l['id'],
                             'mode':    l['mode']
                        }

                    if l.get('subnet', None) is not None:
                        link['subnet_id'] = l['subnet']['id']
                        link['ip_address'] = l.get('ip_address', None)

                    link_list.append(link)

        refined_dict['links'] = link_list

        i = cls(api_client, **refined_dict)
        return i

class Interfaces(model_base.ResourceCollectionBase):

    collection_url = 'nodes/{system_id}/interfaces/'
    collection_resource = Interface

    def __init__(self, api_client, **kwargs):
        super(Interfaces, self).__init__(api_client)
        self.system_id = kwargs.get('system_id', None)

    def create_vlan(self, vlan_tag, parent_name, mtu=None, tags=[]):
        """
        Create a new VLAN interface on this node

        :param vlan_tag: The VLAN ID (not MaaS resource id of a VLAN) to create interface for
        :param parent_name: The name of a MaaS interface to build the VLAN interface on top of
        :param mtu: Optional configuration of the interface MTU
        :param tags: Optional list of string tags to apply to the VLAN interface
        """

        self.refresh()

        parent_iface = self.singleton({'name': parent_name})

        if parent_iface is None:
            self.logger.error("Cannot locate parent interface %s" % (parent_name))
            raise errors.DriverError("Cannot locate parent interface %s" % (parent_name))

        if parent_iface.type != 'physical':
            self.logger.error("Cannot create VLAN interface on parent of type %s" % (parent_iface.type))
            raise errors.DriverError("Cannot create VLAN interface on parent of type %s" % (parent_iface.type))

        if parent_iface.vlan is None:
            self.logger.error("Cannot create VLAN interface on disconnected parent %s" % (parent_iface.resource_id))
            raise errors.DriverError("Cannot create VLAN interface on disconnected parent %s" % (parent_iface.resource_id))

        vlans = maas_vlan.Vlans(self.api_client, fabric_id=parent_iface.fabric_id)
        vlans.refresh()

        vlan = vlans.singleton({'vid': vlan_tag})

        if vlan is None:
            self.logger.error("Cannot locate VLAN %s on fabric %s to attach interface" %
                              (vlan_tag, parent_iface.fabric_id))

        exists = self.singleton({'vlan': vlan.resource_id})

        if exists is not None:
            self.logger.info("Interface for VLAN %s already exists on node %s, skipping" %
                             (vlan_tag, self.system_id))
            return None

        url = self.interpolate_url()

        
        options = { 'tags': ','.join(tags),
                    'vlan': vlan.resource_id,
                    'parent': parent_iface.resource_id,
                  }

        if mtu is not None:
            options['mtu'] = mtu

        resp = self.api_client.post(url, op='create_vlan', files=options)


        if resp.status_code == 200:
            resp_json = resp.json()
            vlan_iface = Interface.from_dict(self.api_client, resp_json)
            self.logger.debug("Created VLAN interface %s for parent %s attached to VLAN %s" %
                              (vlan_iface.resource_id, parent_iface.resource_id, vlan.resource_id))
            return vlan_iface
        else:
            self.logger.error("Error creating VLAN interface to VLAN %s on system %s - MaaS response %s: %s" %
                              (vlan.resource_id, self.system_id, resp.status_code, resp.text))
            raise errors.DriverError("Error creating VLAN interface to VLAN %s on system %s - MaaS response %s" %
                              (vlan.resource_id, self.system_id, resp.status_code))

        self.refresh()
        
        return