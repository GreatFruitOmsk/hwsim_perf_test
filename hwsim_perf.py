#!/usr/bin/env python3

import argparse
import collections
import contextlib
import locale
import pathlib
import subprocess
import tempfile


console_encoding = locale.getpreferredencoding()


def command(*args, **kwargs):
    kwargs.setdefault('check', True)
    return subprocess.run(args, **kwargs)


class Daemon(subprocess.Popen):
    def __init__(self, *args, **kwargs):
        super().__init__(args, **kwargs)

    def __exit__(self, *args):
        try:
            super().terminate()
        except ProcessLookupError:
            pass
        finally:
            return super().__exit__(*args)


class NetNS:
    def __init__(self, name):
        self.name = name

    def __enter__(self):
        print("Creating ns {}".format(self.name))
        command('ip', 'netns', 'add', self.name)
        return self

    def __exit__(self, *_):
        print("Deleting ns {}".format(self.name))
        command('ip', 'netns', 'delete', self.name)

    def daemon(self, *args, **kwargs):
        return Daemon('ip', 'netns', 'exec', self.name, *args, **kwargs)

    def command(self, *args, **kwargs):
        return command('ip', 'netns', 'exec', self.name, *args, **kwargs)

    def popen(self, *args, **kwargs):
        return subprocess.Popen(('ip', 'netns', 'exec', self.name) + args, **kwargs)

    def move_phy(self, wdev):
        print("Moving {} to ns {}".format(wdev, self.name))
        return command('iw', 'phy', wdev.phy, 'set', 'netns', 'name', self.name)


def test(num_clients, tcp_window_size, time, bandwidth=None):
    iperf_config = ['-N', '-w', str(tcp_window_size), '-l', str(tcp_window_size)]

    if bandwidth:
        iperf_config += ['-b', str(bandwidth)]

    data_dir = pathlib.Path(__file__).resolve().parent

    if pathlib.Path('/sys/module/mac80211_hwsim').exists():
        command('rmmod', 'mac80211_hwsim')

    command('modprobe', 'mac80211_hwsim', 'radios={}'.format(num_clients + 1))

    sim_devs = []
    WDev = collections.namedtuple('WDev', ['phy', 'dev'])

    for dev in pathlib.Path('/sys/class/mac80211_hwsim').iterdir():
        dev_name = next((dev / 'net').iterdir()).name
        phy_name = next((dev / 'ieee80211').iterdir()).name
        sim_devs.append(WDev(phy_name, dev_name))

    ap_dev = sim_devs[0]
    sim_devs = sim_devs[1:]

    with contextlib.ExitStack() as stack:
        ap_ns = stack.enter_context(NetNS('access_point'))
        ap_ns.move_phy(ap_dev)
        ap_ns.command('ip', 'link', 'set', 'dev', ap_dev.dev, 'name', 'wlan_ap')
        ap_dev = WDev(ap_dev.phy, 'wlan_ap')
        ap_ns.command('ip', 'addr', 'add', '192.168.200.1/24', 'broadcast', '192.168.200.255', 'dev', ap_dev.dev)

        stack.enter_context(ap_ns.daemon('hostapd', str(data_dir / 'hostapd.conf')))

        stack.enter_context(ap_ns.daemon('iperf', '-s', *iperf_config))

        client_namespaces = []
        wpa_clis = []

        for i, wdev in enumerate(sim_devs):
            client_temp = stack.enter_context(tempfile.TemporaryDirectory())
            client_ctrl = pathlib.Path(client_temp) / 'wpa_ctrl'

            client_ns = stack.enter_context(NetNS('client{}'.format(i)))
            client_ns.move_phy(wdev)
            client_ns.command('ip', 'addr', 'add', '192.168.200.{}/24'.format(i + 2), 'broadcast', '192.168.200.255', 'dev', wdev.dev)

            wpa_supplicant = stack.enter_context(client_ns.daemon('wpa_supplicant', '-i', wdev.dev, '-c', str(data_dir / 'wpa_supplicant.conf'), '-C', str(client_ctrl), '-W'))

            wpa_cli = stack.enter_context(client_ns.daemon('wpa_cli', '-i', wdev.dev, '-p', str(client_ctrl), stdin=subprocess.PIPE, stdout=subprocess.PIPE))
            wpa_clis.append(wpa_cli)

            client_namespaces.append(client_ns)

        for wpa_cli in wpa_clis:
            for line in wpa_cli.stdout:
                if b'CTRL-EVENT-CONNECTED' in line:
                    wpa_cli.terminate()
                    break

        for client_ns in client_namespaces:
            stack.enter_context(client_ns.popen('iperf', '-c', '192.168.200.1', '-t', str(time), *iperf_config))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--num-clients', type=int, default=1)
    parser.add_argument('--time', type=int, default=10, help="in seconds")
    parser.add_argument('--tcp-window-size', default='416K', help="in bytes (K/M/G suffixes allowed)")
    parser.add_argument('--bandwidth', help="in bits per second (K/M/G suffixes allowed)")
    test(**vars(parser.parse_args()))
