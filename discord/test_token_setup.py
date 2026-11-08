"""Dependency-free tests for the local token setup persistence boundary."""
import tempfile
import subprocess
import shutil
import unittest
from pathlib import Path
from urllib.parse import urlencode

from token_setup import encrypt_for_current_user, parse_form_token, store_encrypted_token


class TokenSetupCheck(unittest.TestCase):
    def test_form_parse_and_dpapi_dummy_roundtrip_in_temp_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / ".secrets" / "bot-token.dpapi"
            form = bytearray(urlencode({"csrf": "test-csrf", "token": "harmless_dummy_token"}).encode("ascii"))
            token = parse_form_token(form, "test-csrf")
            store_encrypted_token(token, destination, encrypt=encrypt_for_current_user)
            token = None
            form[:] = b"\0" * len(form)
            stored = destination.read_text(encoding="ascii")
            self.assertTrue(stored.startswith("01000000"))

            decrypt_script = r"""
$Ciphertext = [Console]::In.ReadToEnd()
$Secure = ConvertTo-SecureString -String $Ciphertext
$Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
try { [Console]::Out.Write([Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)) }
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
    $Secure.Dispose()
            $Ciphertext = $null
}
"""
            powershell = shutil.which("pwsh.exe")
            self.assertIsNotNone(powershell)
            result = subprocess.run(
                [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", decrypt_script],
                input=stored, capture_output=True, text=True, encoding="utf-8", check=True,
            )
            self.assertEqual(result.stdout, "harmless_dummy_token")

    def test_rejects_bad_form_and_csrf(self):
        with self.assertRaises(ValueError):
            parse_form_token(bytearray(b"csrf=wrong&token=harmless_dummy_token"), "expected")
        with self.assertRaises(ValueError):
            parse_form_token(bytearray(b"csrf=expected&token=has+whitespace"), "expected")

    def test_rejects_empty_or_whitespace_input_before_encryption(self):
        for value in ("", "has whitespace"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                called = []
                destination = Path(directory) / "token.dpapi"
                with self.assertRaises(ValueError):
                    store_encrypted_token(value, destination, encrypt=lambda token: called.append(token))
                self.assertEqual(called, [])
                self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
