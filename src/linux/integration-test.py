#!/usr/bin/python3

# upower integration test suite
#
# Run in built tree to test local built binaries, or from anywhere else to test
# system installed binaries.
#
# Copyright: (C) 2011 Martin Pitt <martin.pitt@ubuntu.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

import os
import sys
import dbus
import tempfile
import subprocess
import unittest
import time
from packaging.version import parse as parse_version

try:
    import dbusmock
except ImportError:
    sys.stderr.write('Skipping tests, python-dbusmock not available (http://pypi.python.org/pypi/python-dbusmock).\n')
    sys.exit(77)

UP = 'org.freedesktop.UPower'
UP_DEVICE = 'org.freedesktop.UPower.Device'
UP_DISPLAY_OBJECT_PATH = '/org/freedesktop/UPower/devices/DisplayDevice'
UP_DAEMON_ACTION_DELAY = 20

DEVICE_IFACE = 'org.bluez.Device1'
BATTERY_IFACE = 'org.bluez.Battery1'

(UP_DEVICE_STATE_UNKNOWN,
 UP_DEVICE_STATE_CHARGING,
 UP_DEVICE_STATE_DISCHARGING,
 UP_DEVICE_STATE_EMPTY,
 UP_DEVICE_STATE_FULLY_CHARGED,
 UP_DEVICE_STATE_PENDING_CHARGE,
 UP_DEVICE_STATE_PENDING_DISCHARGE) = range(7)

(UP_DEVICE_LEVEL_UNKNOWN,
 UP_DEVICE_LEVEL_NONE,
 UP_DEVICE_LEVEL_DISCHARGING,
 UP_DEVICE_LEVEL_LOW,
 UP_DEVICE_LEVEL_CRITICAL,
 UP_DEVICE_LEVEL_ACTION,
 UP_DEVICE_LEVEL_NORMAL,
 UP_DEVICE_LEVEL_HIGH,
 UP_DEVICE_LEVEL_FULL) = range(9)

(UP_DEVICE_KIND_UNKNOWN,
 UP_DEVICE_KIND_LINE_POWER,
 UP_DEVICE_KIND_BATTERY,
 UP_DEVICE_KIND_UPS,
 UP_DEVICE_KIND_MONITOR,
 UP_DEVICE_KIND_MOUSE,
 UP_DEVICE_KIND_KEYBOARD,
 UP_DEVICE_KIND_PDA,
 UP_DEVICE_KIND_PHONE,
 UP_DEVICE_KIND_MEDIA_PLAYER,
 UP_DEVICE_KIND_TABLET,
 UP_DEVICE_KIND_COMPUTER,
 UP_DEVICE_KIND_GAMING_INPUT,
 UP_DEVICE_KIND_PEN,
 UP_DEVICE_KIND_TOUCHPAD,
 UP_DEVICE_KIND_MODEM,
 UP_DEVICE_KIND_NETWORK,
 UP_DEVICE_KIND_HEADSET,
 UP_DEVICE_KIND_SPEAKERS,
 UP_DEVICE_KIND_HEADPHONES,
 UP_DEVICE_KIND_VIDEO,
 UP_DEVICE_KIND_OTHER_AUDIO,
 UP_DEVICE_KIND_REMOTE_CONTROL,
 UP_DEVICE_KIND_PRINTER,
 UP_DEVICE_KIND_SCANNER,
 UP_DEVICE_KIND_CAMERA,
 UP_DEVICE_KIND_WEARABLE,
 UP_DEVICE_KIND_TOY,
 UP_DEVICE_KIND_BLUETOOTH_GENERIC) = range(29)

class Tests(dbusmock.DBusTestCase):
    @classmethod
    def setUpClass(cls):
        # run from local build tree if we are in one, otherwise use system instance
        builddir = os.getenv('top_builddir', '.')
        if os.access(os.path.join(builddir, 'src', 'upowerd'), os.X_OK):
            cls.daemon_path = os.path.join(builddir, 'src', 'upowerd')
            print('Testing binaries from local build tree')
            cls.local_daemon = True
        elif os.environ.get('UNDER_JHBUILD', False):
            jhbuild_prefix = os.environ['JHBUILD_PREFIX']
            cls.daemon_path = os.path.join(jhbuild_prefix, 'libexec', 'upowerd')
            print('Testing binaries from JHBuild')
            cls.local_daemon = False
        else:
            print('Testing installed system binaries')
            cls.daemon_path = None
            with open('/usr/share/dbus-1/system-services/org.freedesktop.UPower.service') as f:
                for line in f:
                    if line.startswith('Exec='):
                        cls.daemon_path = line.split('=', 1)[1].strip()
                        break
            assert cls.daemon_path, 'could not determine daemon path from D-BUS .service file'
            cls.local_daemon = False

        # fail on CRITICALs on client side
        GLib.log_set_always_fatal(GLib.LogLevelFlags.LEVEL_WARNING |
                                  GLib.LogLevelFlags.LEVEL_ERROR |
                                  GLib.LogLevelFlags.LEVEL_CRITICAL)

        # set up a fake system D-BUS
        cls.test_bus = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
        cls.test_bus.up()
        try:
            del os.environ['DBUS_SESSION_BUS_ADDRESS']
        except KeyError:
            pass
        os.environ['DBUS_SYSTEM_BUS_ADDRESS'] = cls.test_bus.get_bus_address()

        cls.dbus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        cls.dbus_con = cls.get_dbus(True)

    @classmethod
    def tearDownClass(cls):
        cls.test_bus.down()
        dbusmock.DBusTestCase.tearDownClass()

    def setUp(self):
        '''Set up a local umockdev testbed.

        The testbed is initially empty.
        '''
        self.testbed = UMockdev.Testbed.new()

        self.proxy = None
        self.log = None
        self.daemon = None
        self.logind = None

    def tearDown(self):
        del self.testbed
        self.stop_daemon()

        if self.logind:
            self.logind.stdout.close()
            self.logind.terminate()
            self.logind.wait()

        try:
            if self.bluez:
                self.bluez.stdout.close()
                self.bluez.terminate()
                self.bluez.wait()
        except:
            pass

        # on failures, print daemon log
        errors = [x[1] for x in self._outcome.errors if x[1]]
        if errors and self.log:
            with open(self.log.name) as f:
                sys.stderr.write('\n-------------- daemon log: ----------------\n')
                sys.stderr.write(f.read())
                sys.stderr.write('------------------------------\n')

    #
    # Daemon control and D-BUS I/O
    #

    def start_daemon(self, cfgfile=None):
        '''Start daemon and create DBus proxy.

        Do this after adding the devices you want to test with. At the moment
        this only works with coldplugging, as we do not currently have a way to
        inject simulated uevents.

        When done, this sets self.proxy as the Gio.DBusProxy for upowerd.
        '''
        env = os.environ.copy()
        if cfgfile is not None:
            env['UPOWER_CONF_FILE_NAME'] = cfgfile
        env['G_DEBUG'] = 'fatal-criticals'
        # note: Python doesn't propagate the setenv from Testbed.new(), so we
        # have to do that ourselves
        env['UMOCKDEV_DIR'] = self.testbed.get_root_dir()
        self.log = tempfile.NamedTemporaryFile()
        if os.getenv('VALGRIND') != None:
            if self.local_daemon:
                daemon_path = ['libtool', '--mode=execute', 'valgrind', self.daemon_path, '-v']
            else:
                daemon_path = ['valgrind', self.daemon_path, '-v']
        else:
            daemon_path = [self.daemon_path, '-v']
        self.daemon = subprocess.Popen(daemon_path,
                                       env=env, stdout=self.log,
                                       stderr=subprocess.STDOUT)

        # wait until the daemon gets online
        timeout = 100
        while timeout > 0:
            time.sleep(0.1)
            timeout -= 1
            try:
                self.get_dbus_property('DaemonVersion')
                break
            except GLib.GError:
                pass
        else:
            self.fail('daemon did not start in 10 seconds')

        self.proxy = Gio.DBusProxy.new_sync(
            self.dbus, Gio.DBusProxyFlags.DO_NOT_AUTO_START, None, UP,
            '/org/freedesktop/UPower', UP, None)

        self.assertEqual(self.daemon.poll(), None, 'daemon crashed')

    def stop_daemon(self):
        '''Stop the daemon if it is running.'''

        if self.daemon:
            try:
                self.daemon.kill()
            except OSError:
                pass
            self.daemon.wait()
        self.daemon = None
        self.proxy = None

    def get_dbus_property(self, name):
        '''Get property value from daemon D-Bus interface.'''

        proxy = Gio.DBusProxy.new_sync(
            self.dbus, Gio.DBusProxyFlags.DO_NOT_AUTO_START, None, UP,
            '/org/freedesktop/UPower', 'org.freedesktop.DBus.Properties', None)
        return proxy.Get('(ss)', UP, name)

    def get_dbus_display_property(self, name):
        '''Get property value from display device D-Bus interface.'''

        proxy = Gio.DBusProxy.new_sync(
            self.dbus, Gio.DBusProxyFlags.DO_NOT_AUTO_START, None, UP,
            UP_DISPLAY_OBJECT_PATH, 'org.freedesktop.DBus.Properties', None)
        return proxy.Get('(ss)', UP + '.Device', name)

    def get_dbus_dev_property(self, device, name):
        '''Get property value from an upower device D-Bus path.'''

        proxy = Gio.DBusProxy.new_sync(
            self.dbus, Gio.DBusProxyFlags.DO_NOT_AUTO_START, None, UP, device,
            'org.freedesktop.DBus.Properties', None)
        return proxy.Get('(ss)', UP + '.Device', name)

    def start_logind(self, parameters=None):
        self.logind, self.logind_obj = self.spawn_server_template('logind',
                                                                  parameters or {},
                                                                  stdout=subprocess.PIPE)

    def start_bluez(self, parameters=None):
        self.bluez, self.bluez_obj = self.spawn_server_template('bluez5',
                                                                  parameters or {},
                                                                  stdout=subprocess.PIPE)

    def have_text_in_log(self, text):
        return self.count_text_in_log(text) > 0

    def count_text_in_log(self, text):
        with open(self.log.name) as f:
            return f.read().count(text)

    def assertEventually(self, condition, message=None, timeout=50, value=True):
        '''Assert that condition function eventually returns True.

        Timeout is in deciseconds, defaulting to 50 (5 seconds). message is
        printed on failure.
        '''
        while timeout >= 0:
            context = GLib.MainContext.default()
            while context.iteration(False):
                pass
            if condition() == value:
                break
            timeout -= 1
            time.sleep(0.1)
        else:
            self.fail(message or 'timed out waiting for ' + str(condition))

    #
    # Actual test cases
    #

    def test_daemon_version(self):
        '''DaemonVersion property'''

        self.start_daemon()
        self.assertEqual(self.proxy.EnumerateDevices(), [])
        self.assertRegex(self.get_dbus_property('DaemonVersion'), '^[0-9.]+$')

        # without any devices we should assume AC
        self.assertEqual(self.get_dbus_property('OnBattery'), False)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_NONE)
        self.stop_daemon()

    def test_no_devices(self):
        '''no devices'''

        # without any devices we should assume AC
        self.start_daemon()
        self.assertEqual(self.get_dbus_property('OnBattery'), False)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_NONE)
        self.stop_daemon()

    def test_props_online_ac(self):
        '''properties with online AC'''

        ac = self.testbed.add_device('power_supply', 'AC', None,
                                     ['type', 'Mains', 'online', '1'], [])

        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        ac_up = devs[0]
        self.assertTrue('line_power_AC' in ac_up)
        self.assertEqual(self.get_dbus_property('OnBattery'), False)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_NONE)
        self.assertEqual(self.get_dbus_dev_property(ac_up, 'PowerSupply'), True)
        self.assertEqual(self.get_dbus_dev_property(ac_up, 'Type'), UP_DEVICE_KIND_LINE_POWER)
        self.assertEqual(self.get_dbus_dev_property(ac_up, 'Online'), True)
        self.assertEqual(self.get_dbus_dev_property(ac_up, 'NativePath'), 'AC')
        self.stop_daemon()

    def test_props_offline_ac(self):
        '''properties with offline AC'''

        ac = self.testbed.add_device('power_supply', 'AC', None,
                                     ['type', 'Mains', 'online', '0'], [])
        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        # we don't have any known online power device now, but still no battery
        self.assertEqual(self.get_dbus_property('OnBattery'), False)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_NONE)
        self.assertEqual(self.get_dbus_dev_property(devs[0], 'Online'), False)
        self.stop_daemon()

    def test_macbook_capacity(self):
        '''MacBooks have incorrect sysfs capacity'''

        ac = self.testbed.add_device('power_supply', 'AC', None,
                                     ['type', 'Mains', 'online', '0'], [])
        bat0 = self.testbed.add_device('power_supply', 'BAT0', None,
                                       ['type', 'Battery',
                                        'present', '1',
                                        'status', 'Discharging',
                                        'capacity', '60',
                                        'energy_full', '60000000',
                                        'energy_full_design', '80000000',
                                        'energy_now', '48000000',
                                        'voltage_now', '12000000'], [])
        self.testbed.add_device('virtual', 'virtual/dmi', None,
                                ['id/product_name', 'MacBookAir7,2'], [])
        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 2)
        if 'BAT' in devs[0] == ac_up:
            (bat0_up, ac_up) = devs
        else:
            (ac_up, bat0_up) = devs

        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Percentage'), 80)

    def test_macbook_uevent(self):
        '''MacBooks sent uevent 5 seconds before battery updates'''

        ac = self.testbed.add_device('power_supply', 'AC', None,
                                     ['type', 'Mains', 'online', '0'], [])
        bat0 = self.testbed.add_device('power_supply', 'BAT0', None,
                                       ['type', 'Battery',
                                        'present', '1',
                                        'status', 'Discharging',
                                        'energy_full', '60000000',
                                        'energy_full_design', '80000000',
                                        'energy_now', '48000000',
                                        'voltage_now', '12000000'], [])
        self.testbed.add_device('virtual', 'virtual/dmi', None,
                                ['id/product_name', 'MacBookAir7,2'], [])
        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 2)
        if 'BAT' in devs[0] == ac_up:
            (bat0_up, ac_up) = devs
        else:
            (ac_up, bat0_up) = devs

        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'State'), UP_DEVICE_STATE_DISCHARGING)

        self.testbed.set_attribute(ac, 'online', '1')
        self.testbed.uevent(ac, 'change')
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'State'), UP_DEVICE_STATE_DISCHARGING)
        time.sleep(3)
        self.testbed.set_attribute(bat0, 'status', 'Charging')
        time.sleep(1)

        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'State'), UP_DEVICE_STATE_CHARGING)

    def test_battery_ac(self):
        '''properties with dynamic battery/AC'''

        # offline AC + discharging battery
        ac = self.testbed.add_device('power_supply', 'AC', None,
                                     ['type', 'Mains', 'online', '0'], [])
        bat0 = self.testbed.add_device('power_supply', 'BAT0', None,
                                       ['type', 'Battery',
                                        'present', '1',
                                        'status', 'Discharging',
                                        'energy_full', '60000000',
                                        'energy_full_design', '80000000',
                                        'energy_now', '48000000',
                                        'voltage_now', '12000000'], [])

        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 2)
        if 'BAT' in devs[0] == ac_up:
            (bat0_up, ac_up) = devs
        else:
            (ac_up, bat0_up) = devs

        # we don't have any known online power device now, but still no battery
        self.assertEqual(self.get_dbus_property('OnBattery'), True)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_NONE)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'IsPresent'), True)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'State'), UP_DEVICE_STATE_DISCHARGING)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Percentage'), 80.0)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Energy'), 48.0)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'EnergyFull'), 60.0)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'EnergyFullDesign'), 80.0)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Voltage'), 12.0)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'NativePath'), 'BAT0')
        self.stop_daemon()

        # offline AC + discharging low battery
        self.testbed.set_attribute(bat0, 'energy_now', '1500000')
        self.start_daemon()
        self.assertEqual(self.get_dbus_property('OnBattery'), True)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_CRITICAL)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'IsPresent'), True)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'State'), UP_DEVICE_STATE_DISCHARGING)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Percentage'), 2.5)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'PowerSupply'), True)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Type'), UP_DEVICE_KIND_BATTERY)
        self.stop_daemon()

        # now connect AC again
        self.testbed.set_attribute(ac, 'online', '1')
        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 2)
        # we don't have any known online power device now, but still no battery
        self.assertEqual(self.get_dbus_property('OnBattery'), False)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_NONE)
        self.assertEqual(self.get_dbus_dev_property(ac_up, 'Online'), True)
        self.stop_daemon()

    def test_multiple_batteries(self):
        '''Multiple batteries'''

        # one well charged, one low
        bat0 = self.testbed.add_device('power_supply', 'BAT0', None,
                                       ['type', 'Battery',
                                        'present', '1',
                                        'status', 'Discharging',
                                        'energy_full', '60000000',
                                        'energy_full_design', '80000000',
                                        'energy_now', '48000000',
                                        'voltage_now', '12000000'], [])

        self.testbed.add_device('power_supply', 'BAT1', None,
                                ['type', 'Battery',
                                 'present', '1',
                                 'status', 'Discharging',
                                 'energy_full', '60000000',
                                 'energy_full_design', '80000000',
                                 'energy_now', '1500000',
                                 'voltage_now', '12000000'], [])

        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 2)

        # as we have one which is well-charged, the summary state is "not low
        # battery"
        self.assertEqual(self.get_dbus_property('OnBattery'), True)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_NONE)
        self.stop_daemon()

        # now set both to low
        self.testbed.set_attribute(bat0, 'energy_now', '1500000')
        self.start_daemon()
        self.assertEqual(self.get_dbus_property('OnBattery'), True)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_CRITICAL)
        self.stop_daemon()

    def test_unknown_battery_status_no_ac(self):
        '''Unknown battery charge status, no AC'''

        self.testbed.add_device('power_supply', 'BAT0', None,
                                ['type', 'Battery',
                                 'present', '1',
                                 'status', 'unknown',
                                 'energy_full', '60000000',
                                 'energy_full_design', '80000000',
                                 'energy_now', '48000000',
                                 'voltage_now', '12000000'], [])

        # with no other power sources, the OnBattery value here is really
        # arbitrary, so don't test it. The only thing we know for sure is that
        # we aren't on low battery
        self.start_daemon()
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_NONE)
        self.stop_daemon()

    def test_unknown_battery_status_with_ac(self):
        '''Unknown battery charge status, with AC'''

        self.testbed.add_device('power_supply', 'BAT0', None,
                                ['type', 'Battery',
                                 'present', '1',
                                 'status', 'unknown',
                                 'energy_full', '60000000',
                                 'energy_full_design', '80000000',
                                 'energy_now', '48000000',
                                 'voltage_now', '12000000'], [])
        ac = self.testbed.add_device('power_supply', 'AC', None,
                                     ['type', 'Mains', 'online', '0'], [])
        self.start_daemon()
        self.assertEqual(self.get_dbus_property('OnBattery'), True)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_NONE)
        self.stop_daemon()

        self.testbed.set_attribute(ac, 'online', '1')
        self.start_daemon()
        self.assertEqual(self.get_dbus_property('OnBattery'), False)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_NONE)
        self.stop_daemon()

    def test_display_pending_charge_one_battery(self):
        '''One battery pending-charge'''

        self.testbed.add_device('power_supply', 'BAT0', None,
                                ['type', 'Battery',
                                 'present', '1',
                                 'status', 'Not charging',
                                 'charge_full', '10500000',
                                 'charge_full_design', '11000000',
                                 'capacity', '40',
                                 'voltage_now', '12000000'], [])

        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        self.assertEqual(self.get_dbus_display_property('State'), UP_DEVICE_STATE_PENDING_CHARGE)
        self.stop_daemon()

    def test_display_pending_charge_other_battery_discharging(self):
        '''One battery pending-charge and another one discharging'''

        self.testbed.add_device('power_supply', 'BAT0', None,
                                ['type', 'Battery',
                                 'present', '1',
                                 'status', 'Not charging',
                                 'charge_full', '10500000',
                                 'charge_full_design', '11000000',
                                 'capacity', '40',
                                 'voltage_now', '12000000'], [])
        self.testbed.add_device('power_supply', 'BAT1', None,
                                ['type', 'Battery',
                                 'present', '1',
                                 'status', 'Discharging',
                                 'charge_full', '10500000',
                                 'charge_full_design', '11000000',
                                 'capacity', '40',
                                 'voltage_now', '12000000'], [])


        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 2)
        self.assertEqual(self.get_dbus_display_property('State'), UP_DEVICE_STATE_DISCHARGING)
        self.stop_daemon()

    def test_display_pending_charge_other_battery_charging(self):
        '''One battery pending-charge and another one charging'''

        self.testbed.add_device('power_supply', 'BAT0', None,
                                ['type', 'Battery',
                                 'present', '1',
                                 'status', 'Not charging',
                                 'charge_full', '10500000',
                                 'charge_full_design', '11000000',
                                 'capacity', '40',
                                 'voltage_now', '12000000'], [])
        self.testbed.add_device('power_supply', 'BAT1', None,
                                ['type', 'Battery',
                                 'present', '1',
                                 'status', 'Charging',
                                 'charge_full', '10500000',
                                 'charge_full_design', '11000000',
                                 'capacity', '40',
                                 'voltage_now', '12000000'], [])


        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 2)
        self.assertEqual(self.get_dbus_display_property('State'), UP_DEVICE_STATE_CHARGING)
        self.stop_daemon()

    def test_map_pending_charge_to_fully_charged(self):
        '''Map pending-charge to fully-charged'''

        bat0 = self.testbed.add_device('power_supply', 'BAT0', None,
                                       ['type', 'Battery',
                                        'present', '1',
                                        'status', 'Not charging',
                                        'charge_full', '10500000',
                                        'charge_full_design', '11000000',
                                        'capacity', '100',
                                        'voltage_now', '12000000'], [])

        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        bat0_up = devs[0]
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'State'), UP_DEVICE_STATE_FULLY_CHARGED)
        self.stop_daemon()

        # and make sure we still return pending-charge below 100%
        self.testbed.set_attribute(bat0, 'capacity', '99')
        self.start_daemon()
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'State'), UP_DEVICE_STATE_PENDING_CHARGE)
        self.stop_daemon()

    def test_battery_charge(self):
        '''battery which reports charge instead of energy

        energy_* is in uWh, while charge_* is in uAh.
        '''
        self.testbed.add_device('power_supply', 'BAT0', None,
                                ['type', 'Battery',
                                 'present', '1',
                                 'status', 'Discharging',
                                 'charge_full', '10500000',
                                 'charge_full_design', '11000000',
                                 'charge_now', '7875000',
                                 'current_now', '787000',
                                 'voltage_now', '12000000'], [])

        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        bat0_up = devs[0]

        self.assertEqual(self.get_dbus_property('OnBattery'), True)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_NONE)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'IsPresent'), True)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'State'), UP_DEVICE_STATE_DISCHARGING)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Percentage'), 75.0)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Energy'), 94.5)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'EnergyFull'), 126.0)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'EnergyFullDesign'), 132.0)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Voltage'), 12.0)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Temperature'), 0.0)
        self.stop_daemon()

    def test_battery_energy_charge_mixed(self):
        '''battery which reports current energy, but full charge'''

        self.testbed.add_device('power_supply', 'BAT0', None,
                                ['type', 'Battery',
                                 'present', '1',
                                 'status', 'Discharging',
                                 'charge_full', '10500000',
                                 'charge_full_design', '11000000',
                                 'energy_now', '50400000',
                                 'voltage_now', '12000000'], [])

        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        bat0_up = devs[0]

        self.assertEqual(self.get_dbus_property('OnBattery'), True)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_NONE)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'IsPresent'), True)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'State'), UP_DEVICE_STATE_DISCHARGING)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Energy'), 50.4)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'EnergyFull'), 126.0)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'EnergyFullDesign'), 132.0)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Voltage'), 12.0)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Percentage'), 40.0)
        self.stop_daemon()

    def test_battery_capacity_and_charge(self):
        '''battery which reports capacity and charge_full'''

        self.testbed.add_device('power_supply', 'BAT0', None,
                                ['type', 'Battery',
                                 'present', '1',
                                 'status', 'Discharging',
                                 'charge_full', '10500000',
                                 'charge_full_design', '11000000',
                                 'capacity', '40',
                                 'voltage_now', '12000000'], [])

        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        bat0_up = devs[0]

        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Percentage'), 40.0)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'IsPresent'), True)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'State'), UP_DEVICE_STATE_DISCHARGING)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Energy'), 50.4)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'EnergyFull'), 126.0)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'EnergyFullDesign'), 132.0)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Voltage'), 12.0)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'PowerSupply'), True)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Type'), UP_DEVICE_KIND_BATTERY)

        self.assertEqual(self.get_dbus_property('OnBattery'), True)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_NONE)
        self.stop_daemon()

    def test_battery_overfull(self):
        '''battery which reports a > 100% percentage for a full battery'''

        self.testbed.add_device('power_supply', 'BAT0', None,
                                ['type', 'Battery',
                                 'present', '1',
                                 'status', 'Full',
                                 'capacity_level', 'Normal\n',
                                 'current_now', '1000',
                                 'charge_now', '11000000',
                                 'charge_full', '10000000',
                                 'charge_full_design', '11000000',
                                 'capacity', '110',
                                 'voltage_now', '12000000'], [])

        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        bat0_up = devs[0]

        # should clamp percentage
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Percentage'), 100.0)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'IsPresent'), True)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'State'),
                         UP_DEVICE_STATE_FULLY_CHARGED)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Energy'), 132.0)
        # should adjust EnergyFull to reality, not what the battery claims
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'EnergyFull'), 132.0)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'EnergyFullDesign'), 132.0)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Voltage'), 12.0)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'PowerSupply'), True)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Type'), UP_DEVICE_KIND_BATTERY)
        # capacity_level is unused because a 'capacity' attribute is present and used instead
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'BatteryLevel'), UP_DEVICE_LEVEL_NONE)
        self.stop_daemon()

    def test_battery_temperature(self):
        '''battery which reports temperature'''

        self.testbed.add_device('power_supply', 'BAT0', None,
                                ['type', 'Battery',
                                 'present', '1',
                                 'status', 'Discharging',
                                 'temp', '254',
                                 'energy_full', '60000000',
                                 'energy_full_design', '80000000',
                                 'energy_now', '1500000',
                                 'voltage_now', '12000000'], [])

        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        bat0_up = devs[0]

        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Temperature'), 25.4)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Percentage'), 2.5)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Energy'), 1.5)
        self.assertEqual(self.get_dbus_property('OnBattery'), True)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_CRITICAL)
        self.stop_daemon()

    def test_battery_broken_name(self):
        '''Battery with funky kernel name'''

        self.testbed.add_device('power_supply', 'bq24735@5-0009', None,
                                ['type', 'Battery',
                                 'present', '1',
                                 'status', 'unknown',
                                 'energy_full', '60000000',
                                 'energy_full_design', '80000000',
                                 'energy_now', '48000000',
                                 'voltage_now', '12000000'], [])

        self.start_daemon()
        self.assertEqual(self.get_dbus_display_property('IsPresent'), True)
        self.stop_daemon()

    def test_battery_zero_power_draw(self):
        '''Battery with zero power draw, e.g. in a dual-battery system'''

        self.testbed.add_device('power_supply', 'BAT0', None,
                                ['type', 'Battery',
                                 'present', '1',
                                 'status', 'Full',
                                 'energy_full', '60000000',
                                 'energy_full_design', '80000000',
                                 'energy_now', '60000000',
                                 'voltage_now', '12000000',
                                 'power_now', '0',
                                 'current_now', '787000'], [])

        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        bat0_up = devs[0]

        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'EnergyRate'), 0.0)
        self.stop_daemon()

    def test_ups_no_ac(self):
        '''UPS properties without AC'''

        # add a charging UPS
        ups0 = self.testbed.add_device('usb', 'hiddev0', None, [],
                                       ['DEVNAME', 'null', 'UPOWER_VENDOR', 'APC',
                                        'UPOWER_BATTERY_TYPE', 'ups',
                                        'UPOWER_FAKE_DEVICE', '1',
                                        'UPOWER_FAKE_HID_CHARGING', '1',
                                        'UPOWER_FAKE_HID_PERCENTAGE', '70'])

        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        ups0_up = devs[0]

        self.assertEqual(self.get_dbus_dev_property(ups0_up, 'Vendor'), 'APC')
        self.assertEqual(self.get_dbus_dev_property(ups0_up, 'IsPresent'), True)
        self.assertEqual(self.get_dbus_dev_property(ups0_up, 'Percentage'), 70.0)
        self.assertEqual(self.get_dbus_dev_property(ups0_up, 'State'), UP_DEVICE_STATE_CHARGING)
        self.assertEqual(self.get_dbus_dev_property(ups0_up, 'PowerSupply'), True)
        self.assertEqual(self.get_dbus_dev_property(ups0_up, 'Type'), UP_DEVICE_KIND_UPS)

        self.assertEqual(self.get_dbus_property('OnBattery'), False)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_NONE)
        self.stop_daemon()

        # now switch to discharging UPS
        self.testbed.set_property(ups0, 'UPOWER_FAKE_HID_CHARGING', '0')

        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        self.assertEqual(devs[0], ups0_up)

        self.assertEqual(self.get_dbus_dev_property(ups0_up, 'IsPresent'), True)
        self.assertEqual(self.get_dbus_dev_property(ups0_up, 'Percentage'), 70.0)
        self.assertEqual(self.get_dbus_dev_property(ups0_up, 'State'), UP_DEVICE_STATE_DISCHARGING)
        self.assertEqual(self.get_dbus_property('OnBattery'), True)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_DISCHARGING)
        self.stop_daemon()

        # low UPS charge
        self.testbed.set_property(ups0, 'UPOWER_FAKE_HID_PERCENTAGE', '2')
        self.start_daemon()
        self.assertEqual(self.get_dbus_dev_property(ups0_up, 'Percentage'), 2.0)
        self.assertEqual(self.get_dbus_dev_property(ups0_up, 'State'), UP_DEVICE_STATE_DISCHARGING)
        self.assertEqual(self.get_dbus_property('OnBattery'), True)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_ACTION)
        self.stop_daemon()

    def test_ups_offline_ac(self):
        '''UPS properties with offline AC'''

        # add low charge UPS
        ups0 = self.testbed.add_device('usb', 'hiddev0', None, [],
                                       ['DEVNAME', 'null', 'UPOWER_VENDOR', 'APC',
                                        'UPOWER_BATTERY_TYPE', 'ups',
                                        'UPOWER_FAKE_DEVICE', '1',
                                        'UPOWER_FAKE_HID_CHARGING', '0',
                                        'UPOWER_FAKE_HID_PERCENTAGE', '2'])
        # add an offline AC, should still be on battery
        ac = self.testbed.add_device('power_supply', 'AC', None,
                                     ['type', 'Mains', 'online', '0'], [])
        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        if 'AC' in devs[0]:
            ups0_up = devs[1]
        else:
            ups0_up = devs[0]

        self.assertEqual(len(devs), 2)

        self.assertEqual(self.get_dbus_dev_property(ups0_up, 'Percentage'), 2.0)
        self.assertEqual(self.get_dbus_dev_property(ups0_up, 'State'), UP_DEVICE_STATE_DISCHARGING)
        self.assertEqual(self.get_dbus_property('OnBattery'), True)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_ACTION)
        self.stop_daemon()

        # now plug in the AC, should switch to OnBattery=False
        self.testbed.set_attribute(ac, 'online', '1')
        self.start_daemon()
        self.assertEqual(self.get_dbus_property('OnBattery'), False)
        # FIXME this is completely wrong
        # The AC status doesn't change anything, the AC is what powers the UPS
        # and the UPS powers the desktop
        #
        # A plugged in UPS is always the one supplying the computer
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_ACTION)
        self.stop_daemon()

    def test_refresh_after_sleep(self):
        '''sleep/wake cycle to check we properly refresh the batteries'''

        bat0 = self.testbed.add_device('power_supply', 'BAT0', None,
                                       ['type', 'Battery',
                                        'present', '1',
                                        'status', 'Discharging',
                                        'energy_full', '60000000',
                                        'energy_full_design', '80000000',
                                        'energy_now', '48000000',
                                        'voltage_now', '12000000'], [])

        self.start_logind()
        self.start_daemon()

        self.logind_obj.EmitSignal('', 'PrepareForSleep', 'b', [True])
        self.assertEventually(lambda: self.have_text_in_log("Poll paused"), timeout=10)

        # simulate some battery drain during sleep for which we then
        # can check after we 'woke up'
        self.testbed.set_attribute(bat0, 'energy_now', '40000000')

        self.logind_obj.EmitSignal('', 'PrepareForSleep', 'b', [False])
        self.assertEventually(lambda: self.have_text_in_log("Poll resumed"), timeout=10)

        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)

        bat0_up = devs[0]
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Energy'), 40.0)

        self.stop_daemon()

    @unittest.skipIf(parse_version(dbusmock.__version__) <= parse_version('0.23.1'), 'Not supported in dbusmock version')
    def test_prevent_sleep_until_critical_action_is_executed(self):
        '''check that critical action is executed when trying to suspend'''

        bat0 = self.testbed.add_device('power_supply', 'BAT0', None,
                                       ['type', 'Battery',
                                        'present', '1',
                                        'status', 'Discharging',
                                        'energy_full', '60000000',
                                        'energy_full_design', '80000000',
                                        'energy_now', '50000000',
                                        'voltage_now', '12000000'], [])

        config = tempfile.NamedTemporaryFile(delete=False, mode='w')
        config.write("[UPower]\n")
        config.write("UsePercentageForPolicy=true\n")
        config.write("PercentageAction=5\n")
        config.write("CriticalPowerAction=Hibernate\n")
        config.close()

        self.start_logind()
        self.start_daemon(cfgfile=config.name)

        # delay inhibitor taken
        self.assertEqual(len(self.logind_obj.ListInhibitors()), 1)

        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        bat0_up = devs[0]

        # simulate that battery has 1% (less than PercentageAction)
        self.testbed.set_attribute(bat0, 'energy_now', '600000')
        self.testbed.uevent(bat0, 'change')

        # critical action is scheduled, a block inhibitor lock is taken besides a delay inhibitor lock
        time.sleep(0.5)
        self.assertEventually(lambda: self.get_dbus_display_property('WarningLevel'), value=UP_DEVICE_LEVEL_ACTION)
        self.assertEqual(len(self.logind_obj.ListInhibitors()), 2)

        time.sleep(UP_DAEMON_ACTION_DELAY + 0.5) # wait for UP_DAEMON_ACTION_DELAY
        self.assertEqual(self.count_text_in_log("About to call logind method Hibernate"), 1)

        # block inhibitor lock is released
        self.assertEqual(len(self.logind_obj.ListInhibitors()), 1)

    def test_critical_action_is_taken_repeatedly(self):
        '''check that critical action works repeatedly (eg. after resume)'''

        bat0 = self.testbed.add_device('power_supply', 'BAT0', None,
                                       ['type', 'Battery',
                                        'present', '1',
                                        'status', 'Discharging',
                                        'energy_full', '60000000',
                                        'energy_full_design', '80000000',
                                        'energy_now', '50000000',
                                        'voltage_now', '12000000'], [])

        config = tempfile.NamedTemporaryFile(delete=False, mode='w')
        config.write("[UPower]\n")
        config.write("UsePercentageForPolicy=true\n")
        config.write("PercentageAction=5\n")
        config.write("CriticalPowerAction=Hibernate\n")
        config.close()

        self.start_logind()
        self.start_daemon(cfgfile=config.name)

        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        bat0_up = devs[0]

        # simulate that battery has 1% (less than PercentageAction)
        self.testbed.set_attribute(bat0, 'energy_now', '600000')
        self.testbed.uevent(bat0, 'change')

        time.sleep(0.5)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_ACTION)

        time.sleep(UP_DAEMON_ACTION_DELAY + 0.5) # wait for UP_DAEMON_ACTION_DELAY
        self.assertEqual(self.count_text_in_log("About to call logind method Hibernate"), 1)

        # simulate that battery was charged to 100% during sleep
        self.testbed.set_attribute(bat0, 'energy_now', '60000000')
        self.testbed.uevent(bat0, 'change')

        time.sleep(0.5)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_NONE)

        # simulate that battery was drained to 1% again
        self.testbed.set_attribute(bat0, 'energy_now', '600000')
        self.testbed.uevent(bat0, 'change')

        time.sleep(0.5)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_ACTION)

        time.sleep(UP_DAEMON_ACTION_DELAY + 0.5) # wait for UP_DAEMON_ACTION_DELAY
        self.assertEqual(self.count_text_in_log("About to call logind method Hibernate"), 2)

        self.stop_daemon()

        os.unlink(config.name)

    def test_no_poll_batteries(self):
        ''' setting NoPollBatteries option should disable polling'''

        self.testbed.add_device('power_supply', 'BAT0', None,
                                ['type', 'Battery',
                                 'present', '1',
                                 'status', 'Discharging',
                                 'energy_full', '60000000',
                                 'energy_full_design', '80000000',
                                 'energy_now', '48000000',
                                 'voltage_now', '12000000'], [])

        config = tempfile.NamedTemporaryFile(delete=False, mode='w')
        config.write("[UPower]\n")
        config.write("NoPollBatteries=true\n")
        config.close()

        self.start_logind()
        self.start_daemon(cfgfile=config.name)

        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)

        self.logind_obj.EmitSignal('', 'PrepareForSleep', 'b', [True])
        self.assertEventually(lambda: self.have_text_in_log("Polling will be paused"), timeout=10)

        self.logind_obj.EmitSignal('', 'PrepareForSleep', 'b', [False])
        self.assertEventually(lambda: self.have_text_in_log("Polling will be resumed"), timeout=10)

        self.stop_daemon()

        # Now make sure we don't have any actual polling setup for the battery
        self.assertFalse(self.have_text_in_log("Setup poll for"))
        self.assertFalse(self.have_text_in_log("Poll paused for"))
        self.assertFalse(self.have_text_in_log("Poll resumed for"))

        os.unlink(config.name)

    def test_percentage_low_icon_set(self):
        '''Without battery level, PercentageLow is limit for icon change'''

        bat0 = self.testbed.add_device('power_supply', 'BAT0', None,
                                       ['type', 'Battery',
                                        'present', '1',
                                        'status', 'Discharging',
                                        'energy_full',        '100000000',
                                        'energy_full_design', '100000000',
                                        'energy_now',          '15000000',
                                        'capacity', '15',
                                        'voltage_now', '12000000'], [])

        config = tempfile.NamedTemporaryFile(delete=False, mode='w')
        # Low, Critical and Action are all needed to avoid fallback to defaults
        config.write("[UPower]\n")
        config.write("PercentageLow=20\n")
        config.write("PercentageCritical=3\n")
        config.write("PercentageAction=2\n")
        config.close()

        self.start_daemon(cfgfile=config.name)
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        bat0_up = devs[0]

        # capacity_level is unused because a 'capacity' attribute is present and used instead
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'BatteryLevel'), UP_DEVICE_LEVEL_NONE)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Percentage'), 15.0)
        # Battery below 20% from config, should set 'caution' icon even if over default (10%)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'IconName'), 'battery-caution-symbolic')

        self.stop_daemon()

        os.unlink(config.name)

    def test_vendor_strings(self):
        '''manufacturer/model_name/serial_number with valid and invalid strings'''

        bat0 = self.testbed.add_device('power_supply', 'BAT0', None,
                                       ['type', 'Battery',
                                        'present', '1',
                                        'status', 'Discharging',
                                        'energy_full', '60000000',
                                        'energy_full_design', '80000000',
                                        'energy_now', '1500000',
                                        'voltage_now', '12000000',
                                        # valid ASCII string
                                        'serial_number', '123ABC',
                                        # valid UTF-8 string
                                        'manufacturer', '⍾ Batt Inc. ☢'],
                                       [])

        # string with invalid chars
        self.testbed.set_attribute_binary(bat0, 'model_name', b'AB\xFFC12\x013')

        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        bat0_up = devs[0]

        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Serial'), '123ABC')
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Vendor'), '⍾ Batt Inc. ☢')
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Model'), 'ABC123')
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Energy'), 1.5)
        self.stop_daemon()

    def _add_bt_mouse(self):
        '''Add a bluetooth mouse to testbed'''

        self.testbed.add_device('bluetooth',
                                'usb1/bluetooth/hci0/hci0:01',
                                None,
                                [], [])

        self.testbed.add_device(
            'input',
            'usb1/bluetooth/hci0/hci0:01/input2/mouse3',
            None,
            [], ['DEVNAME', 'input/mouse3', 'ID_INPUT_MOUSE', '1'])

        mousebat0 = self.testbed.add_device(
            'power_supply',
            'usb1/bluetooth/hci0/hci0:01/1/power_supply/hid-00:11:22:33:44:55-battery',
            None,
            ['type', 'Battery',
             'scope', 'Device',
             'present', '1',
             'online', '1',
             'status', 'Discharging',
             'capacity', '30',
             'model_name', 'Fancy BT mouse'],
            [])

        return mousebat0

    def test_bluetooth_mouse(self):
        '''bluetooth mouse battery'''

        self._add_bt_mouse()

        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        mousebat0_up = devs[0]

        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Model'), 'Fancy BT mouse')
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Percentage'), 30)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'PowerSupply'), False)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Type'), UP_DEVICE_KIND_MOUSE)
        self.assertEqual(self.get_dbus_property('OnBattery'), False)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_NONE)
        self.stop_daemon()

    def test_bluetooth_mouse_reconnect(self):
        '''bluetooth mouse powerdown/reconnect'''

        mb = self._add_bt_mouse()

        self.start_daemon()
        devs_before = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs_before), 1)

        self.testbed.uevent(mb, 'remove')
        time.sleep(1)
        self.assertEqual(self.proxy.EnumerateDevices(), [])
        self.testbed.uevent(mb, 'add')
        time.sleep(0.5)

        devs_after = self.proxy.EnumerateDevices()
        self.assertEqual(devs_before, devs_after)

        # second add, which should be treated as change
        self.testbed.uevent(mb, 'add')
        time.sleep(0.5)

        devs_after = self.proxy.EnumerateDevices()
        self.assertEqual(devs_before, devs_after)

        # with BT devices, original devices don't get removed on powerdown, but
        # on wakeup we'll get a new one which ought to replace the previous;
        # emulate that kernel bug
        os.unlink(os.path.join(self.testbed.get_sys_dir(), 'class',
                               'power_supply', 'hid-00:11:22:33:44:55-battery'))
        mb1 = self.testbed.add_device(
            'power_supply',
            'usb1/bluetooth/hci0/hci0:01/2/power_supply/hid-00:11:22:33:44:55-battery',
            None,
            ['type', 'Battery',
             'scope', 'Device',
             'present', '1',
             'online', '1',
             'status', 'Discharging',
             'capacity', '30',
             'model_name', 'Fancy BT mouse'],
            [])

        self.testbed.uevent(mb1, 'add')
        time.sleep(0.5)

        devs_after = self.proxy.EnumerateDevices()
        self.assertEqual(devs_before, devs_after)

        mb1_up = devs_after[0]
        self.assertEqual(self.get_dbus_dev_property(mb1_up, 'Model'), 'Fancy BT mouse')
        self.assertEqual(self.get_dbus_dev_property(mb1_up, 'Percentage'), 30)
        self.assertEqual(self.get_dbus_dev_property(mb1_up, 'PowerSupply'), False)
        self.stop_daemon()

    def test_hidpp_mouse(self):
        '''HID++ mouse battery'''

        dev = self.testbed.add_device('hid',
                                      '/devices/pci0000:00/0000:00:14.0/usb3/3-10/3-10:1.2/0003:046D:C52B.0009/0003:046D:4101.000A',
                                      None,
                                      [], [])

        parent = dev
        self.testbed.add_device(
            'input',
            '/devices/pci0000:00/0000:00:14.0/usb3/3-10/3-10:1.2/0003:046D:C52B.0009/0003:046D:4101.000A/input/input22',
            parent,
            [], ['DEVNAME', 'input/mouse3', 'ID_INPUT_MOUSE', '1'])

        self.testbed.add_device(
            'power_supply',
            '/devices/pci0000:00/0000:00:14.0/usb3/3-10/3-10:1.2/0003:046D:C52B.0009/0003:046D:4101.000A/power_supply/hidpp_battery_3',
            parent,
            ['type', 'Battery',
             'scope', 'Device',
             'present', '1',
             'online', '1',
             'status', 'Discharging',
             'capacity', '30',
             'serial_number', '123456',
             'model_name', 'Fancy Logitech mouse'],
            [])

        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        mousebat0_up = devs[0]

        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Model'), 'Fancy Logitech mouse')
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Percentage'), 30)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'PowerSupply'), False)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Type'), UP_DEVICE_KIND_MOUSE)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Serial'), '123456')
        self.assertEqual(self.get_dbus_property('OnBattery'), False)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_NONE)
        self.stop_daemon()

    def test_usb_joypad(self):
        '''DualShock 4 joypad connected via USB'''

        dev = self.testbed.add_device('usb',
                                      '/devices/pci0000:00/0000:00:14.0/usb3/3-9',
                                      None,
                                      [], [])

        parent = dev
        self.testbed.add_device(
            'input',
            '/devices/pci0000:00/0000:00:14.0/usb3/3-9/3-9:1.3/0003:054C:09CC.0007/input/input51',
            parent,
            ['name', 'Sony Interactive Entertainment Wireless Controller',
             'uniq', 'ff:ff:ff:ff:ff:ff'],
            ['ID_INPUT', '1',
             'ID_INPUT_JOYSTICK', '1'])

        dev = self.testbed.add_device(
            'power_supply',
            '/devices/pci0000:00/0000:00:14.0/usb3/3-9/3-9:1.3/0003:054C:09CC.0007/power_supply/sony_controller_battery_ff:ff:ff:ff:ff:ff',
            parent,
            ['type', 'Battery',
             'scope', 'Device',
             'present', '1',
             'status', 'Charging',
             'capacity', '20',],
            [])

        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        joypadbat0_up = devs[0]

        self.assertEqual(self.get_dbus_dev_property(joypadbat0_up, 'Model'), 'Sony Interactive Entertainment Wireless Controller')
        self.assertEqual(self.get_dbus_dev_property(joypadbat0_up, 'Serial'), 'ff:ff:ff:ff:ff:ff')
        self.assertEqual(self.get_dbus_dev_property(joypadbat0_up, 'PowerSupply'), False)
        self.assertEqual(self.get_dbus_dev_property(joypadbat0_up, 'Type'), UP_DEVICE_KIND_GAMING_INPUT)
        self.assertEqual(self.get_dbus_property('OnBattery'), False)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_NONE)

    def test_hidpp_touchpad_race(self):
        '''HID++ touchpad with input node that appears later'''

        dev = self.testbed.add_device('hid',
                                      '/devices/pci0000:00/0000:00:14.0/usb3/3-10/3-10:1.2/0003:046D:C52B.0009/0003:046D:4101.000A',
                                      None,
                                      [], [])

        parent = dev
        batt_dev = self.testbed.add_device(
            'power_supply',
            '/devices/pci0000:00/0000:00:14.0/usb3/3-10/3-10:1.2/0003:046D:C52B.0009/0003:046D:4101.000A/power_supply/hidpp_battery_3',
            parent,
            ['type', 'Battery',
             'scope', 'Device',
             'present', '1',
             'online', '1',
             'status', 'Discharging',
             'capacity_level', 'Full\n',
             'serial_number', '123456',
             'model_name', 'Logitech T650'],
            [])

        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        mousebat0_up = devs[0]

        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Model'), 'Logitech T650')
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'PowerSupply'), False)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Type'), UP_DEVICE_KIND_BATTERY)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Serial'), '123456')
        self.assertEqual(self.get_dbus_property('OnBattery'), False)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_NONE)

        # Now test all the levels
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Percentage'), 100)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'BatteryLevel'), UP_DEVICE_LEVEL_FULL)

        self.testbed.add_device(
            'input',
            '/devices/pci0000:00/0000:00:14.0/usb3/3-10/3-10:1.2/0003:046D:C52B.0009/0003:046D:4101.000A/input/input22',
            parent,
            [], ['DEVNAME', 'input/mouse3', 'ID_INPUT_TOUCHPAD', '1', 'ID_INPUT_MOUSE', '1'])
        self.testbed.uevent(batt_dev, 'change')

        time.sleep(0.5)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Type'), UP_DEVICE_KIND_TOUCHPAD)

    def test_hidpp_touchpad(self):
        '''HID++ touchpad battery with 5 capacity levels'''

        dev = self.testbed.add_device('hid',
                                      '/devices/pci0000:00/0000:00:14.0/usb3/3-10/3-10:1.2/0003:046D:C52B.0009/0003:046D:4101.000A',
                                      None,
                                      [], [])

        parent = dev
        self.testbed.add_device(
            'input',
            '/devices/pci0000:00/0000:00:14.0/usb3/3-10/3-10:1.2/0003:046D:C52B.0009/0003:046D:4101.000A/input/input22',
            parent,
            [], ['DEVNAME', 'input/mouse3', 'ID_INPUT_TOUCHPAD', '1', 'ID_INPUT_MOUSE', '1'])

        dev = self.testbed.add_device(
            'power_supply',
            '/devices/pci0000:00/0000:00:14.0/usb3/3-10/3-10:1.2/0003:046D:C52B.0009/0003:046D:4101.000A/power_supply/hidpp_battery_3',
            parent,
            ['type', 'Battery',
             'scope', 'Device',
             'present', '1',
             'online', '1',
             'status', 'Discharging',
             'capacity_level', 'Full\n',
             'serial_number', '123456',
             'model_name', 'Logitech T650'],
            [])

        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        mousebat0_up = devs[0]

        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Model'), 'Logitech T650')
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'PowerSupply'), False)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Type'), UP_DEVICE_KIND_TOUCHPAD)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Serial'), '123456')
        self.assertEqual(self.get_dbus_property('OnBattery'), False)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_NONE)

        # Now test all the levels
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Percentage'), 100)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'BatteryLevel'), UP_DEVICE_LEVEL_FULL)

        self.testbed.set_attribute(dev, 'capacity_level', 'Critical\n')
        self.testbed.uevent(dev, 'change')
        time.sleep(0.5)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Percentage'), 5)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'BatteryLevel'), UP_DEVICE_LEVEL_CRITICAL)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'WarningLevel'), UP_DEVICE_LEVEL_CRITICAL)

        self.testbed.set_attribute(dev, 'capacity_level', 'Low\n')
        self.testbed.uevent(dev, 'change')
        time.sleep(0.5)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Percentage'), 10)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'BatteryLevel'), UP_DEVICE_LEVEL_LOW)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'WarningLevel'), UP_DEVICE_LEVEL_LOW)

        self.testbed.set_attribute(dev, 'capacity_level', 'High\n')
        self.testbed.uevent(dev, 'change')
        time.sleep(0.5)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Percentage'), 70)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'BatteryLevel'), UP_DEVICE_LEVEL_HIGH)

        self.testbed.set_attribute(dev, 'capacity_level', 'Normal\n')
        self.testbed.uevent(dev, 'change')
        time.sleep(0.5)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Percentage'), 55)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'BatteryLevel'), UP_DEVICE_LEVEL_NORMAL)

        self.testbed.set_attribute(dev, 'capacity_level', 'Unknown\n')
        self.testbed.set_attribute(dev, 'status', 'Charging\n')
        self.testbed.uevent(dev, 'change')
        time.sleep(0.5)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Percentage'), 50.0)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'BatteryLevel'), UP_DEVICE_LEVEL_UNKNOWN)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'State'), UP_DEVICE_STATE_CHARGING)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'IconName'), 'battery-good-charging-symbolic')

        self.testbed.set_attribute(dev, 'capacity_level', 'Full\n')
        self.testbed.set_attribute(dev, 'status', 'Full\n')
        self.testbed.uevent(dev, 'change')
        time.sleep(0.5)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Percentage'), 100)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'BatteryLevel'), UP_DEVICE_LEVEL_FULL)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'State'), UP_DEVICE_STATE_FULLY_CHARGED)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'IconName'), 'battery-full-charged-symbolic')

        self.stop_daemon()

    def test_bluetooth_hid_mouse(self):
        '''bluetooth HID mouse battery'''

        dev = self.testbed.add_device(
            'bluetooth',
            '/devices/pci0000:00/0000:00:14.0/usb2/2-7/2-7:1.0/bluetooth/hci0',
            None,
            [], [])

        parent = dev
        dev = self.testbed.add_device(
            'bluetooth',
            'hci0:256',
            parent,
            [], ['DEVTYPE', 'link'])

        parent = dev
        dev = self.testbed.add_device(
            'hid',
            '0005:046D:B00D.0002',
            parent,
            [], ['HID_NAME', 'Fancy BT Mouse'])

        parent = dev
        self.testbed.add_device(
            'power_supply',
            'power_supply/hid-00:1f:20:96:33:47-battery',
            parent,
            ['type', 'Battery',
             'scope', 'Device',
             'present', '1',
             'online', '1',
             'status', 'Discharging',
             'capacity', '30',
             'model_name', 'Fancy BT mouse'],
            [])

        dev = self.testbed.add_device(
            'input',
            'input/input22',
            parent,
            [], ['ID_INPUT_MOUSE', '1'])

        parent = dev
        self.testbed.add_device(
            'input',
            'mouse1',
            parent,
            [], ['ID_INPUT_MOUSE', '1'])

        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        mousebat0_up = devs[0]

        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Model'), 'Fancy BT mouse')
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Percentage'), 30)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'PowerSupply'), False)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Type'), UP_DEVICE_KIND_MOUSE)
        self.assertEqual(self.get_dbus_property('OnBattery'), False)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_NONE)
        self.stop_daemon()

    def test_virtual_unparented_device(self):
        '''Unparented virtual input device'''

        dev = self.testbed.add_device(
            'input',
            'virtual/input/input18',
            None,
            [], [])

        acpi = self.testbed.add_device('acpi', 'PNP0C0A:00', None, [], [])
        bat0 = self.testbed.add_device('power_supply', 'BAT0', acpi,
                                       ['type', 'Battery',
                                        'present', '1',
                                        'status', 'Discharging',
                                        'energy_full', '60000000',
                                        'energy_full_design', '80000000',
                                        'energy_now', '48000000',
                                        'voltage_now', '12000000'], [])

        # Generated a critical in older versions of upower
        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.stop_daemon()

    def test_bluetooth_hid_mouse_no_legacy_subdevice(self):
        '''bluetooth HID mouse battery'''

        dev = self.testbed.add_device(
            'bluetooth',
            '/devices/pci0000:00/0000:00:14.0/usb2/2-7/2-7:1.0/bluetooth/hci0',
            None,
            [], [])

        parent = dev
        dev = self.testbed.add_device(
            'bluetooth',
            'hci0:256',
            parent,
            [], ['DEVTYPE', 'link'])

        parent = dev
        dev = self.testbed.add_device(
            'hid',
            '0005:046D:B00D.0002',
            parent,
            [], ['HID_NAME', 'Fancy BT Mouse'])

        parent = dev
        self.testbed.add_device(
            'power_supply',
            'power_supply/hid-00:1f:20:96:33:47-battery',
            parent,
            ['type', 'Battery',
             'scope', 'Device',
             'present', '1',
             'online', '1',
             'status', 'Discharging',
             'capacity', '30',
             'model_name', 'Fancy BT mouse'],
            [])

        self.testbed.add_device(
            'input',
            'input/input22',
            parent,
            [], ['ID_INPUT_MOUSE', '1'])

        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        mousebat0_up = devs[0]

        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Model'), 'Fancy BT mouse')
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Percentage'), 30)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'PowerSupply'), False)
        self.assertEqual(self.get_dbus_dev_property(mousebat0_up, 'Type'), UP_DEVICE_KIND_MOUSE)
        self.assertEqual(self.get_dbus_property('OnBattery'), False)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_NONE)
        self.stop_daemon()

    def test_bluetooth_keyboard(self):
        '''bluetooth keyboard battery'''

        dev = self.testbed.add_device('bluetooth',
                                      'usb2/bluetooth/hci0/hci0:1',
                                      None,
                                      [], [])

        parent = dev
        self.testbed.add_device(
            'input',
            'input3/event4',
            parent,
            [], ['DEVNAME', 'input/event4', 'ID_INPUT_KEYBOARD', '1'])

        self.testbed.add_device(
            'power_supply',
            'power_supply/hid-00:22:33:44:55:66-battery',
            parent,
            ['type', 'Battery',
             'scope', 'Device',
             'present', '1',
             'online', '1',
             'status', 'Discharging',
             'capacity', '40',
             'model_name', 'Monster Typist'],
            [])

        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        kbdbat0_up = devs[0]

        self.assertEqual(self.get_dbus_dev_property(kbdbat0_up, 'Model'), 'Monster Typist')
        self.assertEqual(self.get_dbus_dev_property(kbdbat0_up, 'Percentage'), 40)
        self.assertEqual(self.get_dbus_dev_property(kbdbat0_up, 'PowerSupply'), False)
        self.assertEqual(self.get_dbus_dev_property(kbdbat0_up, 'Type'), UP_DEVICE_KIND_KEYBOARD)
        self.assertEqual(self.get_dbus_property('OnBattery'), False)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_NONE)
        self.stop_daemon()

    def test_bluetooth_mouse_and_keyboard(self):
        '''keyboard/mouse combo battery'''

        dev = self.testbed.add_device('bluetooth',
                                      'usb2/bluetooth/hci0/hci0:1',
                                      None,
                                      [], [])

        parent = dev
        self.testbed.add_device(
            'input',
            'input3/event3',
            parent,
            [], ['DEVNAME', 'input/event3', 'ID_INPUT_MOUSE', '1'])

        self.testbed.add_device(
            'input',
            'input3/event4',
            parent,
            [], ['DEVNAME', 'input/event4', 'ID_INPUT_KEYBOARD', '1'])

        self.testbed.add_device(
            'power_supply',
            'power_supply/hid-00:22:33:44:55:66-battery',
            parent,
            ['type', 'Battery',
             'scope', 'Device',
             'present', '1',
             'online', '1',
             'status', 'Discharging',
             'capacity', '40',
             'model_name', 'Monster Typist Mouse/Keyboard Combo'],
            [])

        self.start_daemon()
        devs = self.proxy.EnumerateDevices()
        self.assertEqual(len(devs), 1)
        kbdbat0_up = devs[0]

        self.assertEqual(self.get_dbus_dev_property(kbdbat0_up, 'Model'), 'Monster Typist Mouse/Keyboard Combo')
        self.assertEqual(self.get_dbus_dev_property(kbdbat0_up, 'Percentage'), 40)
        self.assertEqual(self.get_dbus_dev_property(kbdbat0_up, 'PowerSupply'), False)
        self.assertEqual(self.get_dbus_dev_property(kbdbat0_up, 'Type'), UP_DEVICE_KIND_KEYBOARD)
        self.assertEqual(self.get_dbus_property('OnBattery'), False)
        self.assertEqual(self.get_dbus_display_property('WarningLevel'), UP_DEVICE_LEVEL_NONE)
        self.stop_daemon()

    def _add_bluez_battery_device(self, alias, device_properties, battery_level):
        self.start_bluez()

        # Add an adapter to both bluez and udev
        adapter_name = 'hci0'
        path = self.bluez_obj.AddAdapter(adapter_name, 'my-computer')
        self.assertEqual(path, '/org/bluez/' + adapter_name)

        dev = self.testbed.add_device('bluetooth',
                                      'usb2/bluetooth/hci0/hci0:1',
                                      None,
                                      [], [])

        # Add a device to bluez
        address = '11:22:33:44:55:66'

        path = self.bluez_obj.AddDevice(adapter_name, address, alias)

        device = self.dbus_con.get_object('org.bluez', path)

        if device_properties:
            device.AddProperties(DEVICE_IFACE, device_properties)

        battery_properties = {
            'Percentage': dbus.Byte(battery_level, variant_level=1),
        }

        device.AddProperties(BATTERY_IFACE, battery_properties)

        self.start_daemon()

        # process = subprocess.Popen(['gdbus', 'introspect', '--system', '--dest', 'org.bluez', '--object-path', '/org/bluez/hci0/dev_11_22_33_44_55_66'])

        # Wait for UPower to process the new device
        time.sleep(0.5)
        return self.proxy.EnumerateDevices()

    def test_bluetooth_le_mouse(self):
        '''Bluetooth LE mouse'''

        alias = 'Arc Touch Mouse SE'
        battery_level = 99
        device_properties = {
            'Appearance': dbus.UInt16(0x03c2, variant_level=1)
        }

        devs = self._add_bluez_battery_device(alias, device_properties, battery_level)
        self.assertEqual(len(devs), 1)
        mouse_bat0_up = devs[0]

        self.assertEqual(self.get_dbus_dev_property(mouse_bat0_up, 'Model'), alias)
        self.assertEqual(self.get_dbus_dev_property(mouse_bat0_up, 'Percentage'), battery_level)
        self.assertEqual(self.get_dbus_dev_property(mouse_bat0_up, 'PowerSupply'), False)
        self.assertEqual(self.get_dbus_dev_property(mouse_bat0_up, 'Type'), UP_DEVICE_KIND_MOUSE)
        self.assertEqual(self.get_dbus_dev_property(mouse_bat0_up, 'UpdateTime') != 0, True)
        self.stop_daemon()

    def test_bluetooth_le_device(self):
        '''Bluetooth LE Device'''
        '''See https://gitlab.freedesktop.org/upower/upower/issues/100'''

        alias = 'Satechi M1 Mouse'
        battery_level = 99
        device_properties = None

        devs = self._add_bluez_battery_device(alias, device_properties, battery_level)
        self.assertEqual(len(devs), 1)
        mouse_bat0_up = devs[0]

        self.assertEqual(self.get_dbus_dev_property(mouse_bat0_up, 'Model'), alias)
        self.assertEqual(self.get_dbus_dev_property(mouse_bat0_up, 'Percentage'), battery_level)
        self.assertEqual(self.get_dbus_dev_property(mouse_bat0_up, 'PowerSupply'), False)
        self.assertEqual(self.get_dbus_dev_property(mouse_bat0_up, 'Type'), UP_DEVICE_KIND_BLUETOOTH_GENERIC)
        self.stop_daemon()

    def test_bluetooth_headphones(self):
        '''Bluetooth Headphones'''

        alias = 'WH-1000XM3'
        battery_level = 99
        device_properties = {
            'Class': dbus.UInt32(0x240404, variant_level=1)
        }

        devs = self._add_bluez_battery_device(alias, device_properties, battery_level)
        self.assertEqual(len(devs), 1)
        bat0_up = devs[0]

        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Model'), alias)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Percentage'), battery_level)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'PowerSupply'), False)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Type'), UP_DEVICE_KIND_HEADSET)
        self.stop_daemon()

    def test_bluetooth_wireless_earbuds(self):
        '''Bluetooth Wireless Earbuds'''

        alias = 'QCY-qs2_R'
        battery_level = 99
        device_properties = {
            'Class': dbus.UInt32(0x240418, variant_level=1)
        }

        devs = self._add_bluez_battery_device(alias, device_properties, battery_level)
        self.assertEqual(len(devs), 1)
        bat0_up = devs[0]

        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Model'), alias)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Percentage'), battery_level)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'PowerSupply'), False)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Type'), UP_DEVICE_KIND_HEADPHONES)
        self.stop_daemon()

    def test_bluetooth_phone(self):
        '''Bluetooth Phone'''

        alias = 'Phone'
        battery_level = 99
        device_properties = {
            'Class': dbus.UInt32(0x5a020c, variant_level=1)
        }

        devs = self._add_bluez_battery_device(alias, device_properties, battery_level)
        self.assertEqual(len(devs), 1)
        bat0_up = devs[0]

        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Model'), alias)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Percentage'), battery_level)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'PowerSupply'), False)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Type'), UP_DEVICE_KIND_PHONE)
        self.stop_daemon()

    def test_bluetooth_computer(self):
        '''Bluetooth Computer'''

        alias = 'Computer'
        battery_level = 99
        device_properties = {
            'Class': dbus.UInt32(0x6c010c, variant_level=1)
        }

        devs = self._add_bluez_battery_device(alias, device_properties, battery_level)
        self.assertEqual(len(devs), 1)
        bat0_up = devs[0]

        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Model'), alias)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Percentage'), battery_level)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'PowerSupply'), False)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Type'), UP_DEVICE_KIND_COMPUTER)
        self.stop_daemon()

    def test_bluetooth_heart_rate_monitor(self):
        '''Bluetooth Heart Rate Monitor'''

        alias = 'Polar H7'
        battery_level = 99
        device_properties = {
            'Appearance': dbus.UInt16(0x0341, variant_level=1)
        }

        devs = self._add_bluez_battery_device(alias, device_properties, battery_level)
        self.assertEqual(len(devs), 1)
        bat0_up = devs[0]

        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Model'), alias)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Percentage'), battery_level)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'PowerSupply'), False)
        self.assertEqual(self.get_dbus_dev_property(bat0_up, 'Type'), UP_DEVICE_KIND_BLUETOOTH_GENERIC)
        self.stop_daemon()

    #
    # libupower-glib tests (through introspection)
    #

    def test_lib_daemon_properties(self):
        '''library GI: daemon properties'''

        self.start_logind(parameters={'CanHybridSleep': 'yes'})
        self.start_daemon()
        client = UPowerGlib.Client.new()
        self.assertRegex(client.get_daemon_version(), '^[0-9.]+$')
        self.assertIn(client.get_lid_is_present(), [False, True])
        self.assertIn(client.get_lid_is_closed(), [False, True])
        self.assertEqual(client.get_on_battery(), False)
        self.assertEqual(client.get_critical_action(), 'HybridSleep')
        self.stop_daemon()

    #
    # Helper methods
    #

    @classmethod
    def _props_to_str(cls, properties):
        '''Convert a properties dictionary to uevent text representation.'''

        prop_str = ''
        if properties:
            for k, v in properties.items():
                prop_str += '%s=%s\n' % (k, v)
        return prop_str

if __name__ == '__main__':
    try:
        import gi
        from gi.repository import GLib
        from gi.repository import Gio
        gi.require_version('UPowerGlib', '1.0')
        from gi.repository import UPowerGlib
    except ImportError as e:
        sys.stderr.write('Skipping tests, PyGobject not available for Python 3, or missing GI typelibs: %s\n' % str(e))
        sys.exit(77)

    try:
        gi.require_version('UMockdev', '1.0')
        from gi.repository import UMockdev
    except ImportError:
        sys.stderr.write('Skipping tests, umockdev not available (https://github.com/martinpitt/umockdev)\n')
        sys.exit(77)

    # run ourselves under umockdev
    if 'umockdev' not in os.environ.get('LD_PRELOAD', ''):
        os.execvp('umockdev-wrapper', ['umockdev-wrapper'] + sys.argv)

    unittest.main()