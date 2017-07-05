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
import time
import logging
import traceback
import sys

import drydock_provisioner.error as errors
import drydock_provisioner.config as config
import drydock_provisioner.drivers as drivers
import drydock_provisioner.objects.fields as hd_fields
import drydock_provisioner.objects.task as task_model

from drydock_provisioner.drivers.node import NodeDriver
from .api_client import MaasRequestFactory

import drydock_provisioner.drivers.node.maasdriver.models.fabric as maas_fabric
import drydock_provisioner.drivers.node.maasdriver.models.vlan as maas_vlan
import drydock_provisioner.drivers.node.maasdriver.models.subnet as maas_subnet
import drydock_provisioner.drivers.node.maasdriver.models.machine as maas_machine

class MaasNodeDriver(NodeDriver):

    def __init__(self, **kwargs):
        super(MaasNodeDriver, self).__init__(**kwargs)
	
        self.driver_name = "maasdriver"
        self.driver_key = "maasdriver"
        self.driver_desc = "MaaS Node Provisioning Driver"

        self.config = config.DrydockConfig.node_driver[self.driver_key]

        self.logger = logging.getLogger('drydock.nodedriver.maasdriver')

    def execute_task(self, task_id):
        task = self.state_manager.get_task(task_id)

        if task is None:
            raise errors.DriverError("Invalid task %s" % (task_id))

        if task.action not in self.supported_actions:
            raise errors.DriverError("Driver %s doesn't support task action %s"
                % (self.driver_desc, task.action))

        if task.action == hd_fields.OrchestratorAction.ValidateNodeServices:
            self.orchestrator.task_field_update(task.get_id(),
                                status=hd_fields.TaskStatus.Running)
            maas_client = MaasRequestFactory(self.config['api_url'], self.config['api_key']) 

            try:
                if maas_client.test_connectivity():
                    if maas_client.test_authentication():
                        self.orchestrator.task_field_update(task.get_id(),
                            status=hd_fields.TaskStatus.Complete,
                            result=hd_fields.ActionResult.Success)
                        return
            except errors.TransientDriverError(ex):
                result = {
                    'retry': True,
                    'detail':  str(ex),
                }
                self.orchestrator.task_field_update(task.get_id(),
                            status=hd_fields.TaskStatus.Complete,
                            result=hd_fields.ActionResult.Failure,
                            result_details=result)
                return
            except errors.PersistentDriverError(ex):
                result = {
                    'retry': False,
                    'detail':  str(ex),
                }
                self.orchestrator.task_field_update(task.get_id(),
                            status=hd_fields.TaskStatus.Complete,
                            result=hd_fields.ActionResult.Failure,
                            result_details=result)
                return
            except Exception(ex):
                result = {
                    'retry': False,
                    'detail':  str(ex),
                }
                self.orchestrator.task_field_update(task.get_id(),
                            status=hd_fields.TaskStatus.Complete,
                            result=hd_fields.ActionResult.Failure,
                            result_details=result)
                return

        design_id = getattr(task, 'design_id', None)

        if design_id is None:
            raise errors.DriverError("No design ID specified in task %s" %
                                     (task_id))


        if task.site_name is None:
            raise errors.DriverError("No site specified for task %s." %
                                    (task_id))

        self.orchestrator.task_field_update(task.get_id(),
                            status=hd_fields.TaskStatus.Running)

        site_design = self.orchestrator.get_effective_site(design_id)

        if task.action == hd_fields.OrchestratorAction.CreateNetworkTemplate:

            self.orchestrator.task_field_update(task.get_id(), status=hd_fields.TaskStatus.Running)

            subtask = self.orchestrator.create_task(task_model.DriverTask,
                        parent_task_id=task.get_id(), design_id=design_id,
                        action=task.action, site_name=task.site_name,
                        task_scope={'site': task.site_name})
            runner = MaasTaskRunner(state_manager=self.state_manager,
                        orchestrator=self.orchestrator,
                        task_id=subtask.get_id(),config=self.config)

            self.logger.info("Starting thread for task %s to create network templates" % (subtask.get_id()))

            runner.start()

            # TODO Figure out coherent system for putting all the timeouts in
            # the config

            runner.join(timeout=120)

            if runner.is_alive():
                result =  {
                    'retry': False,
                    'detail': 'MaaS Network creation timed-out'
                }

                self.logger.warning("Thread for task %s timed out after 120s" % (subtask.get_id()))

                self.orchestrator.task_field_update(task.get_id(),
                            status=hd_fields.TaskStatus.Complete,
                            result=hd_fields.ActionResult.Failure,
                            result_detail=result)
            else:
                subtask = self.state_manager.get_task(subtask.get_id())

                self.logger.info("Thread for task %s completed - result %s" % (subtask.get_id(), subtask.get_result()))
                self.orchestrator.task_field_update(task.get_id(),
                            status=hd_fields.TaskStatus.Complete,
                            result=subtask.get_result())

            return
        elif task.action == hd_fields.OrchestratorAction.IdentifyNode:
            self.orchestrator.task_field_update(task.get_id(),
                                status=hd_fields.TaskStatus.Running)

            subtasks = []

            result_detail = {
                'detail': [],
                'failed_nodes': [],
                'successful_nodes': [],
            }

            for n in task.node_list:
                subtask = self.orchestrator.create_task(task_model.DriverTask,
                        parent_task_id=task.get_id(), design_id=design_id,
                        action=hd_fields.OrchestratorAction.IdentifyNode,
                        site_name=task.site_name,
                        task_scope={'site': task.site_name, 'node_names': [n]})
                runner = MaasTaskRunner(state_manager=self.state_manager,
                        orchestrator=self.orchestrator,
                        task_id=subtask.get_id(),config=self.config)

                self.logger.info("Starting thread for task %s to identify node %s" % (subtask.get_id(), n))

                runner.start()
                subtasks.append(subtask.get_id())

            running_subtasks = len(subtasks)
            attempts = 0
            worked = failed = False

            #TODO Add timeout to config
            while running_subtasks > 0 and attempts < 3:
                for t in subtasks:
                    subtask = self.state_manager.get_task(t)

                    if subtask.status == hd_fields.TaskStatus.Complete:
                        self.logger.info("Task %s to identify node %s complete - status %s" %
                                        (subtask.get_id(), n, subtask.get_result()))
                        running_subtasks = running_subtasks - 1

                        if subtask.result == hd_fields.ActionResult.Success:
                            result_detail['successful_nodes'].extend(subtask.node_list)
                            worked = True
                        elif subtask.result == hd_fields.ActionResult.Failure:
                            result_detail['failed_nodes'].extend(subtask.node_list)
                            failed = True
                        elif subtask.result == hd_fields.ActionResult.PartialSuccess:
                            worked = failed = True

                time.sleep(1 * 60)
                attempts = attempts + 1

            if running_subtasks > 0:
                self.logger.warning("Time out for task %s before all subtask threads complete" % (task.get_id()))
                result = hd_fields.ActionResult.DependentFailure
                result_detail['detail'].append('Some subtasks did not complete before the timeout threshold')
            elif worked and failed:
                result = hd_fields.ActionResult.PartialSuccess
            elif worked:
                result = hd_fields.ActionResult.Success
            else:
                result = hd_fields.ActionResult.Failure

            self.orchestrator.task_field_update(task.get_id(),
                                status=hd_fields.TaskStatus.Complete,
                                result=result,
                                result_detail=result_detail)
        elif task.action == hd_fields.OrchestratorAction.ConfigureHardware:
            self.orchestrator.task_field_update(task.get_id(),
                                status=hd_fields.TaskStatus.Running)

            self.logger.debug("Starting subtask to commissiong %s nodes." % (len(task.node_list)))

            subtasks = []

            result_detail = {
                'detail': [],
                'failed_nodes': [],
                'successful_nodes': [],
            }

            for n in task.node_list:
                subtask = self.orchestrator.create_task(task_model.DriverTask,
                        parent_task_id=task.get_id(), design_id=design_id,
                        action=hd_fields.OrchestratorAction.ConfigureHardware,
                        site_name=task.site_name,
                        task_scope={'site': task.site_name, 'node_names': [n]})
                runner = MaasTaskRunner(state_manager=self.state_manager,
                        orchestrator=self.orchestrator,
                        task_id=subtask.get_id(),config=self.config)

                self.logger.info("Starting thread for task %s to commission node %s" % (subtask.get_id(), n))

                runner.start()
                subtasks.append(subtask.get_id())

            running_subtasks = len(subtasks)
            attempts = 0
            worked = failed = False

            #TODO Add timeout to config
            while running_subtasks > 0 and attempts < 20:
                for t in subtasks:
                    subtask = self.state_manager.get_task(t)

                    if subtask.status == hd_fields.TaskStatus.Complete:
                        self.logger.info("Task %s to commission node %s complete - status %s" %
                                        (subtask.get_id(), n, subtask.get_result()))
                        running_subtasks = running_subtasks - 1

                        if subtask.result == hd_fields.ActionResult.Success:
                            result_detail['successful_nodes'].extend(subtask.node_list)
                            worked = True
                        elif subtask.result == hd_fields.ActionResult.Failure:
                            result_detail['failed_nodes'].extend(subtask.node_list)
                            failed = True
                        elif subtask.result == hd_fields.ActionResult.PartialSuccess:
                            worked = failed = True

                time.sleep(1 * 60)
                attempts = attempts + 1

            if running_subtasks > 0:
                self.logger.warning("Time out for task %s before all subtask threads complete" % (task.get_id()))
                result = hd_fields.ActionResult.DependentFailure
                result_detail['detail'].append('Some subtasks did not complete before the timeout threshold')
            elif worked and failed:
                result = hd_fields.ActionResult.PartialSuccess
            elif worked:
                result = hd_fields.ActionResult.Success
            else:
                result = hd_fields.ActionResult.Failure

            self.orchestrator.task_field_update(task.get_id(),
                                status=hd_fields.TaskStatus.Complete,
                                result=result,
                                result_detail=result_detail)
        elif task.action == hd_fields.OrchestratorAction.ApplyNodeNetworking:
            self.orchestrator.task_field_update(task.get_id(),
                                status=hd_fields.TaskStatus.Running)

            self.logger.debug("Starting subtask to configure networking on %s nodes." % (len(task.node_list)))

            subtasks = []

            result_detail = {
                'detail': [],
                'failed_nodes': [],
                'successful_nodes': [],
            }

            for n in task.node_list:
                subtask = self.orchestrator.create_task(task_model.DriverTask,
                        parent_task_id=task.get_id(), design_id=design_id,
                        action=hd_fields.OrchestratorAction.ApplyNodeNetworking,
                        site_name=task.site_name,
                        task_scope={'site': task.site_name, 'node_names': [n]})
                runner = MaasTaskRunner(state_manager=self.state_manager,
                        orchestrator=self.orchestrator,
                        task_id=subtask.get_id(),config=self.config)

                self.logger.info("Starting thread for task %s to configure networking on node %s" % (subtask.get_id(), n))

                runner.start()
                subtasks.append(subtask.get_id())

            running_subtasks = len(subtasks)
            attempts = 0
            worked = failed = False

            #TODO Add timeout to config
            while running_subtasks > 0 and attempts < 2:
                for t in subtasks:
                    subtask = self.state_manager.get_task(t)

                    if subtask.status == hd_fields.TaskStatus.Complete:
                        self.logger.info("Task %s to apply networking on node %s complete - status %s" %
                                        (subtask.get_id(), n, subtask.get_result()))
                        running_subtasks = running_subtasks - 1

                        if subtask.result == hd_fields.ActionResult.Success:
                            result_detail['successful_nodes'].extend(subtask.node_list)
                            worked = True
                        elif subtask.result == hd_fields.ActionResult.Failure:
                            result_detail['failed_nodes'].extend(subtask.node_list)
                            failed = True
                        elif subtask.result == hd_fields.ActionResult.PartialSuccess:
                            worked = failed = True

                time.sleep(1 * 60)
                attempts = attempts + 1

            if running_subtasks > 0:
                self.logger.warning("Time out for task %s before all subtask threads complete" % (task.get_id()))
                result = hd_fields.ActionResult.DependentFailure
                result_detail['detail'].append('Some subtasks did not complete before the timeout threshold')
            elif worked and failed:
                result = hd_fields.ActionResult.PartialSuccess
            elif worked:
                result = hd_fields.ActionResult.Success
            else:
                result = hd_fields.ActionResult.Failure

            self.orchestrator.task_field_update(task.get_id(),
                                status=hd_fields.TaskStatus.Complete,
                                result=result,
                                result_detail=result_detail)
class MaasTaskRunner(drivers.DriverTaskRunner):

    def __init__(self, config=None, **kwargs):
        super(MaasTaskRunner, self).__init__(**kwargs)

        self.driver_config = config
        self.logger = logging.getLogger('drydock.nodedriver.maasdriver')

    def execute_task(self):
        task_action = self.task.action

        self.orchestrator.task_field_update(self.task.get_id(),
                            status=hd_fields.TaskStatus.Running,
                            result=hd_fields.ActionResult.Incomplete)

        self.maas_client = MaasRequestFactory(self.driver_config['api_url'],
                                              self.driver_config['api_key'])

        site_design = self.orchestrator.get_effective_site(self.task.design_id)

        if task_action == hd_fields.OrchestratorAction.CreateNetworkTemplate:
            # Try to true up MaaS definitions of fabrics/vlans/subnets
            # with the networks defined in Drydock
            design_networks = site_design.networks
            design_links = site_design.network_links

            fabrics = maas_fabric.Fabrics(self.maas_client)
            fabrics.refresh()

            subnets = maas_subnet.Subnets(self.maas_client)
            subnets.refresh()

            result_detail = {
                'detail': []
            }

            for l in design_links:
                fabrics_found = set()

                # First loop through the possible Networks on this NetworkLink
                # and validate that MaaS's self-discovered networking matches
                # our design. This means all self-discovered networks that are matched
                # to a link need to all be part of the same fabric. Otherwise there is no
                # way to reconcile the discovered topology with the designed topology
                for net_name in l.allowed_networks:
                    n = site_design.get_network(net_name)

                    if n is None:
                        self.logger.warning("Network %s allowed on link %s, but not defined." % (net_name, l.name))
                        continue

                    maas_net = subnets.singleton({'cidr': n.cidr})

                    if maas_net is not None:
                        fabrics_found.add(maas_net.fabric)

                if len(fabrics_found) > 1:
                    self.logger.warning("MaaS self-discovered network incompatible with NetworkLink %s" % l.name)
                    continue
                elif len(fabrics_found) == 1:
                    link_fabric_id = fabrics_found.pop()
                    link_fabric = fabrics.select(link_fabric_id)
                    link_fabric.name = l.name
                    link_fabric.update()
                else:
                    link_fabric = fabrics.singleton({'name': l.name})

                    if link_fabric is None:
                        link_fabric = maas_fabric.Fabric(self.maas_client, name=l.name)
                        fabrics.add(link_fabric)


                # Now that we have the fabrics sorted out, check
                # that VLAN tags and subnet attributes are correct
                for net_name in l.allowed_networks:
                    n = site_design.get_network(net_name)

                    if n is None:
                        continue

                    try:
                        subnet = subnets.singleton({'cidr': n.cidr})

                        if subnet is None:
                            self.logger.info("Subnet for network %s not found, creating..." % (n.name))

                            fabric_list = maas_fabric.Fabrics(self.maas_client)
                            fabric_list.refresh()
                            fabric = fabric_list.singleton({'name': l.name})
                        
                            if fabric is not None:
                                vlan_list = maas_vlan.Vlans(self.maas_client, fabric_id=fabric.resource_id)
                                vlan_list.refresh()
                            
                                vlan = vlan_list.singleton({'vid': n.vlan_id if n.vlan_id is not None else 0})

                                if vlan is not None:
                                    vlan.name = n.name

                                    if getattr(n, 'mtu', None) is not None:
                                        vlan.mtu = n.mtu

                                    vlan.update()
                                    result_detail['detail'].append("VLAN %s found for network %s, updated attributes"
                                                    % (vlan.resource_id, n.name))
                                else:
                                    # Create a new VLAN in this fabric and assign subnet to it
                                    vlan = maas_vlan.Vlan(self.maas_client, name=n.name, vid=vlan_id,
                                                        mtu=getattr(n, 'mtu', None),fabric_id=fabric.resource_id)
                                    vlan = vlan_list.add(vlan)

                                    result_detail['detail'].append("VLAN %s created for network %s"
                                                                    % (vlan.resource_id, n.name))
                    
                                # If subnet did not exist, create it here and attach it to the fabric/VLAN
                                subnet = maas_subnet.Subnet(self.maas_client, name=n.name, cidr=n.cidr, fabric=fabric.resource_id,
                                                            vlan=vlan.resource_id, gateway_ip=n.get_default_gateway())

                                subnet_list = maas_subnet.Subnets(self.maas_client)
                                subnet = subnet_list.add(subnet)
                                self.logger.info("Created subnet %s for CIDR %s on VLAN %s" %
                                                (subnet.resource_id, subnet.cidr, subnet.vlan))

                                result_detail['detail'].append("Subnet %s created for network %s" % (subnet.resource_id, n.name))
                            else:
                                self.logger.error("Fabric %s should be created, but cannot locate it." % (l.name))
                        else:
                            subnet.name = n.name
                            subnet.dns_servers = n.dns_servers

                            result_detail['detail'].append("Subnet %s found for network %s, updated attributes"
                                                    % (subnet.resource_id, n.name))
                            self.logger.info("Updating existing MaaS subnet %s" % (subnet.resource_id))

                            vlan_list = maas_vlan.Vlans(self.maas_client, fabric_id=subnet.fabric)
                            vlan_list.refresh()

                            vlan = vlan_list.select(subnet.vlan)

                            if vlan is not None:
                                vlan.name = n.name
                                vlan.set_vid(n.vlan_id)

                                if getattr(n, 'mtu', None) is not None:
                                    vlan.mtu = n.mtu

                                vlan.update()
                                result_detail['detail'].append("VLAN %s found for network %s, updated attributes"
                                                                        % (vlan.resource_id, n.name))
                            else:
                                self.logger.error("MaaS subnet %s does not have a matching VLAN" % (subnet.resource_id))
                                continue
                
                        # Check if the routes have a default route
                        subnet.gateway_ip = n.get_default_gateway()
                        subnet.update()

                        dhcp_on = False

                        for r in n.ranges:
                            subnet.add_address_range(r)
                            if r.get('type', None) == 'dhcp':
                                dhcp_on = True

                        vlan_list = maas_vlan.Vlans(self.maas_client, fabric_id=subnet.fabric)
                        vlan_list.refresh()
                        vlan = vlan_list.select(subnet.vlan)

                        if dhcp_on and not vlan.dhcp_on:
                            self.logger.info("DHCP enabled for subnet %s, activating in MaaS" % (subnet.name))


                            # TODO Ugly hack assuming a single rack controller for now until we implement multirack
                            resp = self.maas_client.get("rackcontrollers/")

                            if resp.ok:
                                resp_json = resp.json()

                                if not isinstance(resp_json, list):
                                    self.logger.warning("Unexpected response when querying list of rack controllers")
                                    self.logger.debug("%s" % resp.text)
                                else:
                                    if len(resp_json) > 1:
                                        self.logger.warning("Received more than one rack controller, defaulting to first")

                                    rackctl_id = resp_json[0]['system_id']

                                    vlan.dhcp_on = True
                                    vlan.primary_rack = rackctl_id
                                    vlan.update()
                                    self.logger.debug("Enabling DHCP on VLAN %s managed by rack ctlr %s" %
                                                      (vlan.resource_id, rackctl_id))
                        elif dhcp_on and vlan.dhcp_on:
                            self.logger.info("DHCP already enabled for subnet %s" % (subnet.resource_id))


                        # TODO sort out static route support as MaaS seems to require the destination
                        # network be defined in MaaS as well

                    except ValueError as vex:
                        raise errors.DriverError("Inconsistent data from MaaS")
                    
            subnet_list = maas_subnet.Subnets(self.maas_client)
            subnet_list.refresh()

            action_result = hd_fields.ActionResult.Incomplete

            success_rate = 0

            for n in design_networks:
                exists = subnet_list.query({'cidr': n.cidr})
                if len(exists) > 0:
                    subnet = exists[0]
                    if subnet.name == n.name:
                        success_rate = success_rate + 1
                    else:
                        success_rate = success_rate + 1
                else:
                    success_rate = success_rate + 1

            if success_rate == len(design_networks):
                action_result = hd_fields.ActionResult.Success
            elif success_rate == - (len(design_networks)):
                action_result = hd_fields.ActionResult.Failure
            else:
                action_result = hd_fields.ActionResult.PartialSuccess

            self.orchestrator.task_field_update(self.task.get_id(),
                            status=hd_fields.TaskStatus.Complete,
                            result=action_result,
                            result_detail=result_detail)
        elif task_action == hd_fields.OrchestratorAction.IdentifyNode:
            try:
                machine_list = maas_machine.Machines(self.maas_client)
                machine_list.refresh()
            except:
                self.orchestrator.task_field_update(self.task.get_id(),
                            status=hd_fields.TaskStatus.Complete,
                            result=hd_fields.ActionResult.Failure,
                            result_detail={'detail': 'Error accessing MaaS Machines API', 'retry': True})
                return

            nodes = self.task.node_list

            result_detail = {'detail': []}

            worked = failed = False

            for n in nodes:
                try:
                    node = site_design.get_baremetal_node(n)
                    machine = machine_list.identify_baremetal_node(node)
                    if machine is not None:
                        worked = True
                        result_detail['detail'].append("Node %s identified in MaaS" % n)
                    else:
                        failed = True
                        result_detail['detail'].append("Node %s not found in MaaS" % n)
                except Exception as ex:
                    failed = True
                    result_detail['detail'].append("Error identifying node %s: %s" % (n, str(ex)))

            result = None
            if worked and failed:
                result = hd_fields.ActionResult.PartialSuccess
            elif worked:
                result = hd_fields.ActionResult.Success
            elif failed:
                result = hd_fields.ActionResult.Failure

            self.orchestrator.task_field_update(self.task.get_id(),
                                                status=hd_fields.TaskStatus.Complete,
                                                result=result,
                                                result_detail=result_detail)
        elif task_action == hd_fields.OrchestratorAction.ConfigureHardware:
            try:
                machine_list = maas_machine.Machines(self.maas_client)
                machine_list.refresh()
            except:
                self.orchestrator.task_field_update(self.task.get_id(),
                            status=hd_fields.TaskStatus.Complete,
                            result=hd_fields.ActionResult.Failure,
                            result_detail={'detail': 'Error accessing MaaS Machines API', 'retry': True})
                return

            nodes = self.task.node_list

            result_detail = {'detail': []}

            worked = failed = False

            # TODO Better way of representing the node statuses than static strings
            for n in nodes:
                try:
                    self.logger.debug("Locating node %s for commissioning" % (n))
                    node = site_design.get_baremetal_node(n)
                    machine = machine_list.identify_baremetal_node(node, update_name=False)
                    if machine is not None:
                        if machine.status_name == ['New', 'Broken']:
                            self.logger.debug("Located node %s in MaaS, starting commissioning" % (n))
                            machine.commission()

                            # Poll machine status
                            attempts = 0

                            while attempts < 20 and machine.status_name != 'Ready':
                                attempts = attempts + 1
                                time.sleep(1 * 60)
                                try:
                                    machine.refresh()
                                    self.logger.debug("Polling node %s status attempt %d: %s" % (n, attempts, machine.status_name))
                                except:
                                    self.logger.warning("Error updating node %s status during commissioning, will re-attempt." %
                                                     (n))
                            if machine.status_name == 'Ready':
                                self.logger.info("Node %s commissioned." % (n))
                                result_detail['detail'].append("Node %s commissioned" % (n))
                                worked = True
                        elif machine.status_name == 'Commissioning':
                            self.logger.info("Located node %s in MaaS, node already being commissioned. Skipping..." % (n))
                            result_detail['detail'].append("Located node %s in MaaS, node already being commissioned. Skipping..." % (n))
                            worked = True
                        elif machine.status_name == 'Ready':
                            self.logger.info("Located node %s in MaaS, node commissioned. Skipping..." % (n))
                            result_detail['detail'].append("Located node %s in MaaS, node commissioned. Skipping..." % (n))
                            worked = True
                        else:
                            self.logger.warning("Located node %s in MaaS, unknown status %s. Skipping..." % (n, machine.status_name))
                            result_detail['detail'].append("Located node %s in MaaS, node commissioned. Skipping..." % (n))
                            failed = True
                    else:
                        self.logger.warning("Node %s not found in MaaS" % n)
                        failed = True
                        result_detail['detail'].append("Node %s not found in MaaS" % n)

                except Exception as ex:
                    failed = True
                    result_detail['detail'].append("Error commissioning node %s: %s" % (n, str(ex)))

            result = None
            if worked and failed:
                result = hd_fields.ActionResult.PartialSuccess
            elif worked:
                result = hd_fields.ActionResult.Success
            elif failed:
                result = hd_fields.ActionResult.Failure

            self.orchestrator.task_field_update(self.task.get_id(),
                                                status=hd_fields.TaskStatus.Complete,
                                                result=result,
                                                result_detail=result_detail)
        elif task_action == hd_fields.OrchestratorAction.ApplyNodeNetworking:
            try:
                machine_list = maas_machine.Machines(self.maas_client)
                machine_list.refresh()
 
                fabrics = maas_fabric.Fabrics(self.maas_client)
                fabrics.refresh()

                subnets = maas_subnet.Subnets(self.maas_client)
                subnets.refresh()
            except Exception as ex:
                self.logger.error("Error applying node networking, cannot access MaaS: %s" % str(ex))
                traceback.print_tb(sys.last_traceback)
                self.orchestrator.task_field_update(self.task.get_id(),
                            status=hd_fields.TaskStatus.Complete,
                            result=hd_fields.ActionResult.Failure,
                            result_detail={'detail': 'Error accessing MaaS API', 'retry': True})
                return

            nodes = self.task.node_list

            result_detail = {'detail': []}

            worked = failed = False

            # TODO Better way of representing the node statuses than static strings
            for n in nodes:
                try:
                    self.logger.debug("Locating node %s for network configuration" % (n))

                    node = site_design.get_baremetal_node(n)
                    machine = machine_list.identify_baremetal_node(node, update_name=False)

                    if machine is not None:
                        if machine.status_name == 'Ready':
                            self.logger.debug("Located node %s in MaaS, starting interface configuration" % (n))
                            
                            for i in node.interfaces:
                                nl = site_design.get_network_link(i.network_link)

                                fabric = fabrics.singleton({'name': nl.name})

                                if fabric is None:
                                    self.logger.error("No fabric found for NetworkLink %s" % (nl.name))
                                    failed = True
                                    continue

                                # TODO HardwareProfile device alias integration
                                iface = machine.get_network_interface(i.device_name)

                                if iface is None:
                                    self.logger.warning("Interface %s not found on node %s, skipping configuration" %
                                                        (i.device_name, machine.resource_id))
                                    continue

                                if iface.fabric_id == fabric.resource_id:
                                    self.logger.debug("Interface %s already attached to fabric_id %s" %
                                                        (i.device_name, fabric.resource_id))
                                else:
                                    self.logger.debug("Attaching node %s interface %s to fabric_id %s" %
                                                  (node.name, i.device_name, fabric.resource_id))
                                    iface.attach_fabric(fabric_id=fabric.resource_id)

                                for iface_net in getattr(i, 'networks', []):
                                    dd_net = site_design.get_network(iface_net)

                                    if dd_net is not None:
                                        link_iface = None
                                        if iface_net == getattr(nl, 'native_network', None):
                                            # If a node interface is attached to the native network for a link
                                            # then the interface itself should be linked to network, not a VLAN
                                            # tagged interface
                                            self.logger.debug("Attaching node %s interface %s to untagged VLAN on fabric %s" %
                                                              (node.name, i.device_name, fabric.resource_id))
                                            link_iface = iface
                                        else:
                                            # For non-native networks, we create VLAN tagged interfaces as children
                                            # of this interface                                
                                            vlan_options = { 'vlan_tag': dd_net.vlan_id,
                                                             'parent_name': iface.name,
                                                           }

                                            if dd_net.mtu is not None:
                                                vlan_options['mtu'] = dd_net.mtu

                                            self.logger.debug("Creating tagged interface for VLAN %s on system %s interface %s" %
                                                              (dd_net.vlan_id, node.name, i.device_name))

                                            link_iface = machine.interfaces.create_vlan(**vlan_options)

                                        link_options = {}
                                        link_options['primary'] = True if iface_net == getattr(node, 'primary_network', None) else False
                                        link_options['subnet_cidr'] = dd_net.cidr

                                        found = False
                                        for a in getattr(node, 'addressing', []):
                                            if a.network == iface_net:
                                                link_options['ip_address'] = None if a.address == 'dhcp' else a.address
                                                found = True

                                        if not found:
                                            self.logger.error("No addressed assigned to network %s for node %s, cannot link." %
                                                               (iface_net, node.name))
                                            continue

                                        self.logger.debug("Linking system %s interface %s to subnet %s" %
                                                          (node.name, i.device_name, dd_net.cidr))

                                        link_iface.link_subnet(**link_options)
                                        worked = True
                                    else:
                                        failed=True
                                        self.logger.error("Did not find a defined Network %s to attach to interface" % iface_net)

                        elif machine.status_name == 'Broken':
                            self.logger.info("Located node %s in MaaS, status broken. Run ConfigureHardware before configurating network" % (n))
                            result_detail['detail'].append("Located node %s in MaaS, status 'Broken'. Skipping..." % (n))
                            failed = True
                        else:
                            self.logger.warning("Located node %s in MaaS, unknown status %s. Skipping..." % (n, machine.status_name))
                            result_detail['detail'].append("Located node %s in MaaS, unknown status %s. Skipping..." % (n, machine.status_name))
                            failed = True
                    else:
                        self.logger.warning("Node %s not found in MaaS" % n)
                        failed = True
                        result_detail['detail'].append("Node %s not found in MaaS" % n)

                except Exception as ex:
                    failed = True
                    self.logger.error("Error configuring network for node %s: %s" % (n, str(ex)))
                    result_detail['detail'].append("Error configuring network for node %s: %s" % (n, str(ex)))

            if failed:
                final_result = hd_fields.ActionResult.Failure
            else:
                final_result = hd_fields.ActionResult.Success

            self.orchestrator.task_field_update(self.task.get_id(),
                                status=hd_fields.TaskStatus.Complete,
                                result=final_result,
                                result_detail=result_detail)