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

from unittest.mock import Mock

import drydock_provisioner.objects as objects


class TestClass(object):
    def test_apply_logicalnames_else(self, input_files, deckhand_orchestrator,
                                     drydock_state, mock_get_build_data):
        """Test node apply_logicalnames hits the else block"""
        input_file = input_files.join("deckhand_fullsite.yaml")

        design_ref = "file://%s" % str(input_file)

        design_status, design_data = deckhand_orchestrator.get_effective_site(
            design_ref)

        def side_effect(**kwargs):
            return []

        drydock_state.get_build_data = Mock(side_effect=side_effect)

        nodes = design_data.baremetal_nodes
        for n in nodes or []:
            n.apply_logicalnames(design_data, state_manager=drydock_state)
            assert n.logicalnames == {}

    def test_apply_logicalnames_success(self, input_files,
                                        deckhand_orchestrator, drydock_state,
                                        mock_get_build_data):
        """Test node apply_logicalnames to get the proper dictionary"""
        input_file = input_files.join("deckhand_fullsite.yaml")

        design_ref = "file://%s" % str(input_file)

        xml_example = """
<?xml version="1.0" standalone="yes" ?>
<!-- generated by lshw-B.02.17 -->
<!-- GCC 5.4.0 20160609 -->
<!-- Linux 4.4.0-104-generic #127-Ubuntu SMP Mon Dec 11 12:16:42 UTC 2017 x86_64 -->
<!-- GNU libc 2 (glibc 2.23) -->
<list>
    <node id="cab23-r720-16" claimed="true" class="system" handle="DMI:0100">
        <description>Rack Mount Chassis</description>
        <product>PowerEdge R720xd (SKU=NotProvided;ModelName=PowerEdge R720xd)</product>
        <vendor>Dell Inc.</vendor>
        <serial>6H5LBY1</serial>
        <width units="bits">64</width>
        <configuration>
            <setting id="boot" value="normal" />
            <setting id="chassis" value="rackmount" />
            <setting id="sku" value="SKU=NotProvided;ModelName=PowerEdge R720xd" />
            <setting id="uuid" value="44454C4C-4800-1035-804C-B6C04F425931" />
        </configuration>
        <capabilities>
            <capability id="smbios-2.7">SMBIOS version 2.7</capability>
            <capability id="dmi-2.7">DMI version 2.7</capability>
            <capability id="vsyscall32">32-bit processes</capability>
        </capabilities>
        <node id="core" claimed="true" class="bus" handle="DMI:0200">
            <node id="pci:1" claimed="true" class="bridge" handle="PCIBUS:0000:03">
                <description>PCI bridge</description>
                <product>Xeon E5/Core i7 IIO PCI Express Root Port 2a</product>
                <vendor>Intel Corporation</vendor>
                <physid>2</physid>
                <businfo>pci@0000:00:02.0</businfo>
                <version>07</version>
                <width units="bits">32</width>
                <clock units="Hz">33000000</clock>
                <configuration>
                    <setting id="driver" value="pcieport" />
                </configuration>
                <capabilities>
                    <capability id="pci" />
                    <capability id="msi">Message Signalled Interrupts</capability>
                    <capability id="pciexpress">PCI Express</capability>
                    <capability id="pm">Power Management</capability>
                    <capability id="normal_decode" />
                    <capability id="bus_master">bus mastering</capability>
                    <capability id="cap_list">PCI capabilities listing</capability>
                </capabilities>
                <resources>
                    <resource type="irq" value="26" />
                </resources>
                <node id="network:0" claimed="true" class="network" handle="PCI:0000:00:03.0">
                    <description>Ethernet interface</description>
                    <product>I350 Gigabit Network Connection</product>
                    <vendor>Intel Corporation</vendor>
                    <physid>0</physid>
                    <businfo>pci@0000:00:03.0</businfo>
                    <logicalname>eno1</logicalname>
                    <version>01</version>
                    <serial>b8:ca:3a:65:7d:d8</serial>
                    <size units="bit/s">1000000000</size>
                    <capacity>1000000000</capacity>
                    <width units="bits">32</width>
                    <clock units="Hz">33000000</clock>
                </node>
            </node>
        </node>
        <node id="disk:0" claimed="true" class="disk" handle="SCSI:00:02:00:00">
            <description>SCSI Disk</description>
            <product>PERC H710P</product>
            <vendor>DELL</vendor>
            <physid>2.0.0</physid>
            <businfo>scsi@2:0.0.0</businfo>
            <logicalname>/dev/sda</logicalname>
            <dev>8:0</dev>
            <version>3.13</version>
            <serial>0044016c12771be71900034cfba0a38c</serial>
            <size units="bytes">299439751168</size>
        </node>
    </node>
</list>
"""
        xml_example = xml_example.replace('\n', '')

        def side_effect(**kwargs):
            build_data = objects.BuildData(
                node_name="controller01",
                task_id="tid",
                generator="lshw",
                data_format="text/plain",
                data_element=xml_example)
            return [build_data]

        drydock_state.get_build_data = Mock(side_effect=side_effect)

        design_status, design_data = deckhand_orchestrator.get_effective_site(
            design_ref)

        nodes = design_data.baremetal_nodes
        nodes[0].apply_logicalnames(design_data, state_manager=drydock_state)

        expected = {
            'primary_boot': 'sda',
            'prim_nic02': 'prim_nic02',
            'prim_nic01': 'eno1'
        }
        # Tests the whole dictionary
        assert nodes[0].logicalnames == expected
        # Makes sure the path and / are both removed from primary_boot
        assert nodes[0].logicalnames['primary_boot'] == 'sda'
        assert nodes[0].get_logicalname('primary_boot') == 'sda'
        # A simple logicalname
        assert nodes[0].logicalnames['prim_nic01'] == 'eno1'
        assert nodes[0].get_logicalname('prim_nic01') == 'eno1'
        # Logicalname is not found, returns the alias
        assert nodes[0].logicalnames['prim_nic02'] == 'prim_nic02'
        assert nodes[0].get_logicalname('prim_nic02') == 'prim_nic02'
