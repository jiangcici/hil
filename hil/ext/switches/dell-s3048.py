# Copyright 2017 Massachusetts Open Cloud Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the
# License. You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS
# IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language
# governing permissions and limitations under the License.

"""A switch driver for Dell S3048-ON running Dell OS 9.

Uses the XML REST API for communicating with the switch.
"""

import logging
from lxml import etree
import re
import requests
import schema

from hil.model import db, Switch
from hil.errors import BadArgumentError
from hil.model import BigIntegerType

logger = logging.getLogger(__name__)

CONFIG = 'config-commands'
SHOW = 'show-command'


class DellNOS9(Switch):
    api_name = 'http://schema.massopencloud.org/haas/v0/switches/dellnos9'

    __mapper_args__ = {
        'polymorphic_identity': api_name,
    }

    id = db.Column(BigIntegerType,
                   db.ForeignKey('switch.id'), primary_key=True)
    hostname = db.Column(db.String, nullable=False)
    username = db.Column(db.String, nullable=False)
    password = db.Column(db.String, nullable=False)
    interface_type = db.Column(db.String, nullable=False)

    @staticmethod
    def validate(kwargs):
        schema.Schema({
            'hostname': basestring,
            'username': basestring,
            'password': basestring,
            'interface_type': basestring,
        }).validate(kwargs)

    def session(self):
        return self

    @staticmethod
    def validate_port_name(port):
        """Valid port names for this switch are of the form 1/0/1 or 1/2"""

        val = re.compile(r'^\d+/\d+(/\d+)?$')
        if not val.match(port):
            raise BadArgumentError("Invalid port name. Valid port names for "
                                   "this switch are of the from 1/0/1 or 1/2")
        return

    def disconnect(self):
        pass

    def modify_port(self, port, channel, network_id):
        (port,) = filter(lambda p: p.label == port, self.ports)
        interface = port.label

        if channel == 'vlan/native':
            if network_id is None:
                self._remove_native_vlan(interface)
            else:
                self._set_native_vlan(interface, network_id)
        else:
            match = re.match(re.compile(r'vlan/(\d+)'), channel)
            assert match is not None, "HIL passed an invalid channel to the" \
                " switch!"
            vlan_id = match.groups()[0]

            if network_id is None:
                self._remove_vlan_from_trunk(interface, vlan_id)
            else:
                assert network_id == vlan_id
                self._add_vlan_to_trunk(interface, vlan_id)

    def revert_port(self, port):
        """ Toggling the switchport resets the port to factory defaults"""
        self._port_shutdown(port)
        self._port_on(port)

    def get_port_networks(self, ports):
        """Get port configurations of the switch.

        Args:
            ports: List of ports to get the configuration for.

        Returns: Dictionary containing the configuration of the form:
        {
            Port<"port-3">: [("vlan/native", "23"), ("vlan/52", "52")],
            Port<"port-7">: [("vlan/23", "23")],
            Port<"port-8">: [("vlan/native", "52")],
            ...
        }

        """
        response = {}
        for port in ports:
            response[port] = filter(None,
                                    [self._get_native_vlan(port.label)]) \
                                    + self._get_vlans(port.label)
        return response

    def _get_vlans(self, interface):
        """ Return the vlans of a trunk port.

        Does not include the native vlan. Use _get_native_vlan.

        Args:
            interface: interface to return the vlans of

        Returns: List containing the vlans of the form:
        [('vlan/vlan1', vlan1), ('vlan/vlan2', vlan2)]

        It uses the REST API CLI which is slow but it is the only way to do it
        because the switch is VLAN centric. Doing a GET on interface won't
        return the VLANS on it, we would have to do get on all vlans and then
        find our interface there which is not feasible.
        """

        url = self._construct_url()
        command = 'interfaces switchport %s %s' % \
            (self.interface_type, interface)
        payload = self._make_payload(SHOW, command)
        response = self._make_request('POST', url, data=payload)
        # parse the output to get a list of all vlans.
        response = response.text.replace(' ', '').splitlines()
        index = response.index("Vlanmembership:") + 3
        vlan_list = response[index].replace('T', '').split(',')
        return [('vlan/%s' % x, x) for x in vlan_list]

    def _get_native_vlan(self, interface):
        """ Return the native vlan of an interface.

        Args:
            interface: interface to return the native vlan of

        Returns: Tuple of the form ('vlan/native', vlan) or None

        Similar to _get_vlans()
        """

        url = self._construct_url()
        command = 'interfaces switchport %s %s' % \
            (self.interface_type, interface)
        payload = self._make_payload(SHOW, command)
        response = self._make_request('POST', url, data=payload)
        # find the Native Vlan ID from the response
        response = response.text.replace(' ', '')
        begin = response.find('NativeVlanId:') + len('NativeVlanId:')
        end = response.find('.', begin)
        vlan = response[begin:end]
        return ('vlan/native', vlan)

    def _add_vlan_to_trunk(self, interface, vlan):
        """ Add a vlan to a trunk port.

        If the port is not trunked, its mode will be set to trunk.

        Args:
            interface: interface to add the vlan to
            vlan: vlan to add
        """
        url = self._construct_url()
        command = 'interface vlan ' + vlan + '\r\n tagged ' + \
            self.interface_type + ' ' + interface
        payload = self._make_payload(CONFIG, command)
        self._make_request('POST', url, data=payload)

    def _remove_vlan_from_trunk(self, interface, vlan):
        """ Remove a vlan from a trunk port.

        Args:
            interface: interface to remove the vlan from
            vlan: vlan to remove
        """
        url = self._construct_url()
        command = 'interface vlan ' + vlan + '\r\n no tagged ' + \
            self.interface_type + ' ' + interface
        payload = self._make_payload(CONFIG, command)
        self._make_request('POST', url, data=payload)

    def _set_native_vlan(self, interface, vlan):
        """ Set the native vlan of an interface.

        Args:
            interface: interface to set the native vlan to
            vlan: vlan to set as the native vlan

        Method relies on the REST API CLI which is slow
        """
        # TODO: turn on port first
        url = self._construct_url()
        command = 'interface vlan ' + vlan + '\r\n untagged ' + \
            self.interface_type + ' ' + interface
        payload = self._make_payload(CONFIG, command)
        self._make_request('POST', url, data=payload)

    def _remove_native_vlan(self, interface):
        """ Remove the native vlan from an interface.

        Args:
            interface: interface to remove the native vlan from.vlan
            vlan: the vlan id that's set as native. It is required because the
            rest API is vlan Centric.

        Method relies on the REST API CLI which is slow.
        """
        vlan = self._get_native_vlan(interface)[1]
        url = self._construct_url()
        command = 'interface vlan ' + vlan + '\r\n no untagged ' + \
            self.interface_type + ' ' + interface
        payload = self._make_payload(CONFIG, command)
        self._make_request('POST', url, data=payload)

    def _port_shutdown(self, interface):
        """ Shuts down <interface>

        Turn off portmode hybrid, disable switchport, and then shut down the
        port. All non-default vlans must be removed before calling this.

        Fast method that uses the REST API (no hackery).
        """

        url = self._construct_url(interface=interface)
        interface = self._convert_interface(self.interface_type) + \
            interface.replace('/', '-')
        payload = '<interface><name>%s</name><portmode><hybrid>false' \
                  '</hybrid></portmode><shutdown>true</shutdown>' \
                  '</interface>' % interface

        self._make_request('PUT', url, data=payload)

    def _port_on(self, interface):
        """ Turns on <interface>

        Turn on port and enable hybrid portmode and switchport.
        Fast method that uses the REST API (no hackery).
        """

        url = self._construct_url(interface=interface)
        interface = self._convert_interface(self.interface_type) + \
            interface.replace('/', '-')
        payload = '<interface><name>%s</name><portmode><hybrid>true' \
                  '</hybrid></portmode><switchport></switchport>' \
                  '<shutdown>false</shutdown></interface>' % interface

        self._make_request('PUT', url, data=payload)

    def _is_port_on(self, port):
        """ Returns a boolean that tells the status of a switchport
        Fast method that uses the REST API (no hackery)
        """

        url = self._construct_url(interface=port, suffix='\?with-defaults')
        response = self._make_request('GET', url)
        root = etree.fromstring(response.text)
        shutdown = root.find(self._construct_tag('shutdown')).text
        # is error handling needed here?
        return (shutdown == 'false')

    # HELPER METHODS *********************************************
    def _construct_url(self, interface=None, suffix=''):
        """ Construct the API url for a specific interface appending suffix.

        Args:
            interface: interface to construct the url for
            suffix: suffix to append at the end of the url (for get methods)

        Returns: string with the url for a specific interface and operation

        If interface is None, then it returns the URL for REST API CLI.
        """

        if interface is None:
            return '%s/api/running/dell/_operations/cli' % self.hostname

        val = re.compile(r'^\d+/\d+(/\d+)?$')
        if val.match(interface):
            # if `interface` refers to port name
            # the urls have dashes instead of slashes in interface names
            interface = interface.replace('/', '-')
            interface_type = self._convert_interface(self.interface_type)
        else:
            # interface refers to `vlan`
            interface_type = 'vlan-'

        return '%(hostname)s/api/running/dell/interfaces/interface/' \
            '%(interface_type)s%(interface)s%(suffix)s' \
            % {
                'hostname': self.hostname,
                'interface_type': interface_type,
                'interface': interface,
                'suffix': '/%s' % suffix if suffix else ''
            }

    @staticmethod
    def _convert_interface(interface_type):
        """ Convert the interface name from switch CLI form to what the API
        server understands.

        Args:
            interface: the interface in the CLI-form

        Returns: string interface
        """
        iftypes = {'GigabitEthernet': 'gige-', 'TenGigabitEthernet': 'tengig-',
                   'TwentyfiveGigabitEthernet': 'twentyfivegig-',
                   'fortyGigE': 'fortygig-', 'peGigabitEthernet': 'pegig-',
                   'FiftyGigabitEthernet': 'fiftygig-',
                   'HundredGigabitEthernet': 'hundredgig-'}

        return iftypes[interface_type]

    @property
    def _auth(self):
        return self.username, self.password

    @staticmethod
    def _make_payload(command, command_type):
        """Consutrcts tag for passing CLI commands using the REST API"""

        return '<input><%s>%s</%s></input>' % (command, command_type, command)

    @staticmethod
    def _construct_tag(name):
        """ Construct the xml tag by prepending the dell tag prefix. """
        return '{http://www.dell.com/ns/dell:0.1/root}%s' % name

    def _make_request(self, method, url, data=None,
                      acceptable_error_codes=()):
        r = requests.request(method, url, data=data, auth=self._auth)
        if r.status_code >= 400 and \
           r.status_code not in acceptable_error_codes:
            print r.text
            logger.error('Bad Request to switch. Response: %s', r.text)
        return r
