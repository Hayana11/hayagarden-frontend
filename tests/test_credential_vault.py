import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

from cryptography.fernet import Fernet

MODULE_PATH = Path(__file__).resolve().parents[1] / "relay" / "credential_vault.py"
SPEC = importlib.util.spec_from_file_location("credential_vault_under_test", MODULE_PATH)
credential_vault = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(credential_vault)

CredentialVaultError = credential_vault.CredentialVaultError
decrypt_secret = credential_vault.decrypt_secret
encrypt_secret = credential_vault.encrypt_secret


class CredentialVaultTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.key_file = Path(self.tempdir.name) / "vault.key"
        self.key_file.write_bytes(Fernet.generate_key() + b"\n")
        if os.name == "posix":
            self.key_file.chmod(0o600)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_encrypts_and_decrypts_without_plaintext_in_ciphertext(self):
        secret = "console-access-token-value"
        ciphertext = encrypt_secret(secret, key_file=str(self.key_file))

        self.assertNotIn(secret, ciphertext)
        self.assertEqual(decrypt_secret(ciphertext, key_file=str(self.key_file)), secret)

    def test_wrong_key_never_exposes_secret_in_error(self):
        ciphertext = encrypt_secret("private-session", key_file=str(self.key_file))
        wrong_key = Path(self.tempdir.name) / "wrong.key"
        wrong_key.write_bytes(Fernet.generate_key())
        if os.name == "posix":
            wrong_key.chmod(0o600)

        with self.assertRaises(CredentialVaultError) as raised:
            decrypt_secret(ciphertext, key_file=str(wrong_key))
        self.assertNotIn("private-session", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
