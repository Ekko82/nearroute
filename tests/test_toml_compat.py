"""Bundled upstream parser handles the Codex config without extra dependencies."""
import io
import unittest
from decimal import Decimal

import toml_compat


class BundledTomlTests(unittest.TestCase):
    def test_realistic_configuration_preserves_nested_and_quoted_values(self):
        config = toml_compat.loads('''model = "gpt-6.1-sol"
model_reasoning_effort = "ultra"
notes = """line one
line two"""
[model_providers."nearroute-http"]
name = "OpenAI"
supports_websockets = false
[projects.'D:\\work space']
trust_level = "trusted"
[[tools]]
name = "one"
options = { enabled = true, regions = ["JP", "TW"] }
''')
        self.assertEqual(config["model_reasoning_effort"], "ultra")
        self.assertIs(config["model_providers"]["nearroute-http"]["supports_websockets"], False)
        self.assertEqual(config["projects"]["D:\\work space"]["trust_level"], "trusted")
        self.assertEqual(config["tools"][0]["options"]["regions"], ["JP", "TW"])

    def test_binary_load_custom_float_and_invalid_duplicate(self):
        self.assertEqual(toml_compat.load(io.BytesIO(b"value=1.25\n"), parse_float=Decimal), {"value": Decimal("1.25")})
        with self.assertRaises(toml_compat.TOMLDecodeError):
            toml_compat.loads('model="one"\nmodel="two"')
