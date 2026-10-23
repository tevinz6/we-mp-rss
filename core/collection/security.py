import hashlib
import hmac
import json
import os
import secrets
import time
from cryptography.fernet import Fernet
import portalocker


class Vault:
    def __init__(self, settings):
        self.directory = settings.data_dir / "auth"
        with portalocker.Lock(str(self.directory / "vault.lock"), timeout=10):
            key_path = self.directory / "key"
            if not key_path.exists():
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as file:
                    file.write(Fernet.generate_key())
            self.key = key_path.read_bytes()
        self.cipher = Fernet(self.key)

    def save(self, source, data):
        path = self.directory / f"{source}.enc"
        tmp = path.with_suffix(f".{secrets.token_hex(6)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(self.cipher.encrypt(json.dumps(data).encode()))
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)

    def load(self, source):
        path = self.directory / f"{source}.enc"
        return json.loads(self.cipher.decrypt(path.read_bytes())) if path.exists() else {}

    def session_token(self, context=""):
        data = f"{int(time.time())}.{secrets.token_hex(16)}"
        return data + "." + hmac.new(self.key, (data + context).encode(), hashlib.sha256).hexdigest()

    def verify_session(self, value, context=""):
        try:
            timestamp, nonce, signature = value.split(".")
            age = time.time() - int(timestamp)
            digest = hmac.new(self.key, (f"{timestamp}.{nonce}" + context).encode(), hashlib.sha256).hexdigest()
            return 0 <= age <= 43200 and hmac.compare_digest(signature, digest)
        except (ValueError, AttributeError):
            return False


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600000).hex()
    return salt + ":" + digest


def check_password(password, value):
    salt, digest = value.split(":")
    return hmac.compare_digest(digest, hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600000).hex())
