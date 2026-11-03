"""Security helpers: password hashing, opaque tokens, rate limiting, secret encryption and SSRF checks.
Nothing here invents cryptography: Argon2id (argon2-cffi), SHA-256 / HMAC and Fernet (cryptography)."""
