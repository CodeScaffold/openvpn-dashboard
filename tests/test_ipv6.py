import ipaddress
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import endpoints, ipv6, netlimit


class IPv6Tests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"IPV6_EGRESS": "", "BIND_IP": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_ipv4_only_endpoints_capture_and_block_ipv6(self):
        config = endpoints._config_text("test", 1194, "udp", "AES-128-GCM", "tun0", "10.9.0.0/24", "", "", 7505, "/tmp/ccd")
        self.assertIn('push "redirect-gateway def1 bypass-dhcp ipv6"', config)
        self.assertIn("server-ipv6 ", config)
        self.assertIn("\nblock-ipv6\n", config)

    def test_public_egress_enables_ipv6_without_blocking(self):
        os.environ["IPV6_EGRESS"] = "2001:4860:4860::8888"
        self.assertNotIn("block-ipv6", ipv6.config_lines("10.9.0.0/24"))

    def test_invalid_egress_is_rejected(self):
        for value in ("127.0.0.1", "::1", "fd00::1", "fe80::1", "::ffff:8.8.8.8", "garbage"):
            with self.subTest(value=value):
                os.environ["IPV6_EGRESS"] = value
                with self.assertRaises(ValueError):
                    ipv6.egress()

    def test_endpoint_and_shared_host_agent_networks_are_distinct(self):
        first = ipv6.network("10.9.0.0/24")
        self.assertEqual(first, ipv6.network("10.9.0.0/24"))
        self.assertNotEqual(first, ipv6.network("10.9.1.0/24"))
        os.environ["BIND_IP"] = "203.0.113.5"
        self.assertNotEqual(first, ipv6.network("10.9.0.0/24"))

    def test_upgrade_preserves_custom_settings_and_is_idempotent(self):
        config = 'dev tun\nlocal 203.0.113.5\npush "redirect-gateway def1 bypass-dhcp"\nstatus /custom/status.log\n'
        updated = ipv6.upgrade_config(config, "10.9.0.0/24")
        self.assertIn("local 203.0.113.5", updated)
        self.assertIn("status /custom/status.log", updated)
        self.assertEqual(updated, ipv6.upgrade_config(updated, "10.9.0.0/24"))
        os.environ["IPV6_EGRESS"] = "2001:4860:4860::8888"
        self.assertNotIn("block-ipv6", ipv6.upgrade_config(updated, "10.9.0.0/24"))

    def test_upgrade_refuses_unmanaged_ipv6_and_custom_routes(self):
        for config in ('server-ipv6 fd00::/64\npush "redirect-gateway def1"\n', 'push "redirect-gateway ipv6 !ipv4"\n', "dev tun\n"):
            with self.assertRaises(ValueError):
                ipv6.upgrade_config(config, "10.9.0.0/24")

    def test_static_ipv4_gets_reserved_ipv6_ccd_assignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subnet = ipv6.network("10.9.0.0/24")
            (root / "server.conf").write_text(f"server-ipv6 {subnet}\n")
            with patch.object(endpoints, "SERVER_DIR", root):
                ccd = root / "ccd" / "server"
                netlimit.write_ccd(str(ccd), "alice", "10.9.0.50", False)
                content = (ccd / "alice").read_text()
            address = ipv6.client_address(subnet, "10.9.0.50")
            self.assertIn(f"ifconfig-ipv6-push {address}/64 {subnet.network_address + 1}", content)
            self.assertLess(int(ipaddress.IPv6Address(address)) - int(subnet.network_address), 0x1000)

    def test_firewall_failure_does_not_silently_enable_ipv6(self):
        os.environ["IPV6_EGRESS"] = "2001:4860:4860::8888"
        with patch.object(ipv6.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "failed")):
            with self.assertRaises(RuntimeError):
                ipv6.configure_runtime("add", "tun0", ipv6.network("10.9.0.0/24"))

    def test_ipv6_shaping_shares_ipv4_class_with_distinct_filter_priorities(self):
        with patch.object(netlimit, "_tc") as tc, patch.object(netlimit, "_ipv6_address", return_value="fd00::132"):
            netlimit.apply_bandwidth("tun0", "10.9.0.50", 1000)
            calls = [call.args for call in tc.call_args_list]
            ipv6_calls = [args for args in calls if "ipv6" in args]
            self.assertEqual(len(ipv6_calls), 2)
            self.assertTrue(all("306" in args for args in ipv6_calls))
            self.assertIn("1:50", ipv6_calls[0])
            self.assertTrue(all("50" in args for args in calls if "ip" in args))
            tc.reset_mock()
            netlimit.remove_bandwidth("tun0", "10.9.0.50")
            self.assertEqual(sum("306" in call.args for call in tc.call_args_list), 2)


if __name__ == "__main__":
    unittest.main()
