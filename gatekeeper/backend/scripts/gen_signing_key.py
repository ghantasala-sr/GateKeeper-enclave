"""Print a new RSA private key for signing Gatekeeper access tokens, as a .env line.

Usage: python -m scripts.gen_signing_key >> ../.env
"""

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
pem = key.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
)
# Base64 so the multi-line PEM fits on one .env line.
print(f"TOKEN_SIGNING_KEY_B64={base64.b64encode(pem).decode()}")
