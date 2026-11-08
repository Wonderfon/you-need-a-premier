"""Import the user-created, single-use local token file without printing its contents."""
from __future__ import annotations

import hmac
from pathlib import Path

from gateway import token_from_dpapi
from token_setup import store_encrypted_token

INPUT_PATH = Path(__file__).resolve().parent / ".secrets" / "bot-token.input.txt"


def main() -> None:
    token = None
    restored = None
    try:
        token = INPUT_PATH.read_text(encoding="utf-8-sig").strip()
        if not token or len(token) > 4096 or any(character.isspace() for character in token):
            raise ValueError("Invalid token input")
        store_encrypted_token(token)
        restored = token_from_dpapi()
        if not hmac.compare_digest(token, restored):
            raise RuntimeError("Encrypted token verification failed")
        INPUT_PATH.unlink()
        print("Token encrypted and verified for this Windows user; temporary plaintext file removed.")
    except Exception:
        raise SystemExit("Token import failed. Input was retained for recovery; no credential content was printed.") from None
    finally:
        token = None
        restored = None


if __name__ == "__main__":
    main()
