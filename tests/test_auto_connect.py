"""The first-open workflow uses isolated controllers, never user credentials."""

import os
import unittest
from unittest.mock import patch

import optimizer
from tests import test_client_connection as fixtures


class AutoConnectTests(unittest.TestCase):
    setUp = fixtures.ClientConnectionTests.setUp
    controller = fixtures.ClientConnectionTests.controller
    fields = fixtures.ClientConnectionTests.fields
    assert_config_unchanged = fixtures.ClientConnectionTests.assert_config_unchanged

    def discover_only(self, *endpoints):
        return patch.object(optimizer.client_detection, "controller_addresses", return_value=list(endpoints))

    def test_first_open_saves_unique_verified_connection_and_starts_enabled(self):
        endpoint, calls = self.controller()
        with self.discover_only(endpoint):
            result = optimizer.auto_connect_client()
        self.assertTrue(result["connected"])
        saved = optimizer.load_config()
        self.assertEqual(saved["controller"], endpoint)
        self.assertEqual(saved["controller_secret_env"], "")
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["custom_note"], self.initial["custom_note"])
        self.assertEqual(saved["scan_nodes"], self.initial["scan_nodes"])
        self.assertTrue(all(method == "GET" for method, _, _ in calls))

    def test_automatic_connection_preserves_a_user_pause(self):
        optimizer.atomic_json(self.config, self.initial | {"enabled": False})
        endpoint, _calls = self.controller()
        with self.discover_only(endpoint):
            result = optimizer.auto_connect_client()
        self.assertTrue(result["connected"])
        self.assertFalse(result["config"]["enabled"])

    def test_multiple_clients_require_one_choice_without_changing_configuration(self):
        first, _calls = self.controller()
        second, _calls = self.controller()
        with self.discover_only(first, second):
            result = optimizer.auto_connect_client()
        self.assertFalse(result["connected"])
        self.assertEqual(len(result["candidates"]), 2)
        self.assert_config_unchanged()

    def test_explicit_client_choice_does_not_contact_other_running_clients(self):
        first, calls = self.controller()
        _second, other_calls = self.controller()
        result = optimizer.auto_connect_client(self.fields(first))
        self.assertTrue(result["connected"])
        self.assertTrue(calls)
        self.assertEqual(other_calls, [])

    def test_ambiguous_groups_need_only_a_group_choice(self):
        endpoint, calls = self.controller(extra_group=True)
        result = optimizer.auto_connect_client(self.fields(endpoint, group="Missing"))
        self.assertFalse(result["connected"])
        self.assertIn("Main", result["groups"])
        self.assertIn("Other", result["groups"])
        self.assert_config_unchanged()
        self.assertTrue(all(method == "GET" for method, _, _ in calls))

    def test_selecting_a_client_without_a_group_yet_can_read_its_group_choices(self):
        endpoint, _calls = self.controller(extra_group=True)
        optimizer.atomic_json(self.config, self.initial | {"group": "Previous client group"})
        result = optimizer.auto_connect_client(self.fields(endpoint, group=""))
        self.assertFalse(result["connected"])
        self.assertIn("Main", result["groups"])
        self.assertIn("Other", result["groups"])

    def test_authentication_refresh_uses_private_store_without_environment_variables(self):
        endpoint, calls = self.controller(secret="a" * 48)
        available = {"secret": ""}
        environment = dict(os.environ)

        def refresh(address, *, elevate=False):
            self.assertEqual(address, endpoint)
            self.assertFalse(elevate)
            available["secret"] = "a" * 48
            return True

        with patch.object(optimizer.windows_app, "IS_WINDOWS", True), \
             patch.object(optimizer.windows_app, "read_controller_credential", side_effect=lambda _: available["secret"]), \
             patch.object(optimizer.windows_app, "refresh_controller_credential", side_effect=refresh) as imported, \
             patch.object(optimizer.windows_app, "controller_credential_needs_permission", return_value=False):
            result = optimizer.auto_connect_client(self.fields(endpoint), allow_elevation=True)
        self.assertTrue(result["connected"])
        imported.assert_called_once_with(endpoint)
        self.assertEqual(result["config"]["controller_secret_env"], "")
        self.assertEqual(dict(os.environ), environment)
        self.assertTrue(all(method == "GET" for method, _, _ in calls))

    def test_unidentified_auth_failure_never_launches_elevation(self):
        endpoint, _calls = self.controller(secret="a" * 48)
        with patch.object(optimizer.windows_app, "IS_WINDOWS", True), \
             patch.object(optimizer.windows_app, "refresh_controller_credential", return_value=False) as imported, \
             patch.object(optimizer.windows_app, "controller_credential_needs_permission", return_value=False):
            result = optimizer.auto_connect_client(self.fields(endpoint), allow_elevation=True)
        self.assertFalse(result["connected"])
        imported.assert_called_once_with(endpoint)
        self.assert_config_unchanged()

    def test_multiple_protected_clients_require_choice_before_any_windows_prompt(self):
        first, _calls = self.controller(secret="a" * 48)
        second, _calls = self.controller(secret="b" * 48)
        with self.discover_only(first, second), \
             patch.object(optimizer.windows_app, "IS_WINDOWS", True), \
             patch.object(optimizer.windows_app, "refresh_controller_credential", return_value=False) as imported, \
             patch.object(optimizer.windows_app, "controller_credential_needs_permission", return_value=True):
            result = optimizer.auto_connect_client(allow_elevation=True)
        self.assertFalse(result["connected"])
        self.assertEqual(imported.call_count, 2)
        self.assertTrue(all(not call.kwargs for call in imported.call_args_list))
        self.assert_config_unchanged()

    def test_private_credential_repairs_a_stale_managed_variable_from_another_port(self):
        endpoint, _calls = self.controller(secret="a" * 48)
        with patch.object(optimizer.windows_app, "read_controller_credential", return_value="a" * 48):
            result = optimizer.auto_connect_client(self.fields(endpoint, controller_secret_env="NEARROUTE_MAOMAO_SECRET_1"))
        self.assertTrue(result["connected"])
        self.assertEqual(result["config"]["controller_secret_env"], "")

    def test_missing_managed_credential_recovers_even_with_stale_inherited_environment(self):
        endpoint, _calls = self.controller(secret="a" * 48)
        name = "NEARROUTE_MAOMAO_SECRET_" + endpoint.rsplit(":", 1)[1]
        store = {"secret": ""}

        def refresh(_endpoint):
            store["secret"] = "a" * 48
            return True

        with patch.dict(os.environ, {name: "b" * 48}), \
             patch.object(optimizer.windows_app, "IS_WINDOWS", True), \
             patch.object(optimizer.windows_app, "_read_user_environment", side_effect=OSError("removed")), \
             patch.object(optimizer.windows_app, "read_controller_credential", side_effect=lambda _: store["secret"]), \
             patch.object(optimizer.windows_app, "refresh_controller_credential", side_effect=refresh), \
             patch.object(optimizer.windows_app, "controller_credential_needs_permission", return_value=False):
            result = optimizer.auto_connect_client(self.fields(endpoint, controller_secret_env=name))
        self.assertTrue(result["connected"])
        self.assertEqual(result["config"]["controller_secret_env"], "")

    def test_runtime_client_can_reuse_port_after_old_private_credential_is_rejected(self):
        endpoint, _calls = self.controller(secret="runtime-secret")
        with patch.object(optimizer.windows_app, "read_controller_credential", return_value="a" * 48), \
             patch.object(optimizer.client_detection, "local_client_credentials", return_value="runtime-secret"):
            result = optimizer.auto_connect_client(self.fields(endpoint))
        self.assertTrue(result["connected"])
        self.assertEqual(self.auth_headers[endpoint][:2], ["Bearer " + "a" * 48, "Bearer runtime-secret"])

    def test_supported_runtime_credential_replaces_a_missing_legacy_environment_variable(self):
        endpoint, _calls = self.controller(secret="runtime-secret")
        name = "NEARROUTE_MAOMAO_SECRET_" + endpoint.rsplit(":", 1)[1]
        with patch.object(optimizer.windows_app, "IS_WINDOWS", True), \
             patch.object(optimizer.windows_app, "read_controller_credential", return_value=""), \
             patch.object(optimizer.windows_app, "_read_user_environment", side_effect=OSError("removed")), \
             patch.object(optimizer.client_detection, "local_client_credentials", return_value="runtime-secret"):
            result = optimizer.auto_connect_client(self.fields(endpoint, controller_secret_env=name))
        self.assertTrue(result["connected"])
        self.assertEqual(result["config"]["controller_secret_env"], "")

    def test_localhost_credentials_use_the_same_literal_ipv4_endpoint_as_requests(self):
        endpoint, _calls = self.controller(secret="runtime-secret")
        with patch.object(optimizer.windows_app, "read_controller_credential", return_value=""), \
             patch.object(optimizer.client_detection, "local_client_credentials", return_value="runtime-secret"):
            result = optimizer.auto_connect_client(self.fields(endpoint.replace("127.0.0.1", "localhost")))
        self.assertTrue(result["connected"])
        self.assertTrue(all(value == "Bearer runtime-secret" for value in self.auth_headers[endpoint]))

    def test_cancelled_windows_authorization_leaves_configuration_untouched(self):
        endpoint, _calls = self.controller(secret="a" * 48)
        with patch.object(optimizer.windows_app, "IS_WINDOWS", True), \
             patch.object(optimizer.windows_app, "refresh_controller_credential", return_value=False) as imported, \
             patch.object(optimizer.windows_app, "controller_credential_needs_permission", return_value=True):
            result = optimizer.auto_connect_client(self.fields(endpoint), allow_elevation=True)
        self.assertFalse(result["connected"])
        self.assertTrue(result["needs_permission"])
        self.assertEqual(imported.call_args_list[-1].kwargs, {"elevate": True})
        self.assertEqual(imported.call_count, 2)
        self.assert_config_unchanged()

    def test_existing_custom_authentication_is_not_replaced(self):
        endpoint, _calls = self.controller(secret="different-secret")
        with patch.object(optimizer.windows_app, "IS_WINDOWS", True), \
             patch.object(optimizer.windows_app, "refresh_controller_credential") as imported:
            result = optimizer.auto_connect_client(self.fields(endpoint, controller_secret_env=self.secret_env),
                                                   allow_elevation=True)
        self.assertFalse(result["connected"])
        imported.assert_not_called()
        self.assert_config_unchanged()

    def test_pending_rollback_blocks_changing_client_after_read_only_detection(self):
        endpoint, calls = self.controller()
        pending = {"preserve": "unfinished rollback"}
        optimizer.atomic_json(self.state / "pending-selection.json", pending)
        with self.assertRaises(RuntimeError):
            optimizer.auto_connect_client(self.fields(endpoint))
        self.assert_config_unchanged()
        self.assertEqual(optimizer.read_json(self.state / "pending-selection.json", {}), pending)
        self.assertTrue(all(method == "GET" for method, _, _ in calls))


if __name__ == "__main__":
    unittest.main()
