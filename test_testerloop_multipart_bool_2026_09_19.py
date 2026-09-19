"""Tester-loop 2026-09-19: multipart transport breaks musefm-v1 signatures on bool fields.

identity.signed_body() signs the canonical form of ai_generated=True as the
string "true" (render_value). But a multipart client (e.g. requests data={...})
encodes the field value with str(True) == "True". The server rebuilds the
canonical message from request.form.to_dict() (all strings), so the signature
fails with 401 "signature does not verify" and the signed-provenance feature
(ai_generated) is unusable for any client that follows the shipped helper
naively with multipart transport.

This file is tests ONLY (tester-loop fix policy: no app source changes).
It currently FAILS; it should PASS once bool fields survive multipart
transport (e.g. the helper documents string coercion, or the verifier
canonicalizes bools before checking).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from identity import b64u_encode, signed_body, verify_signed_body, IdentityError


class StubDB:
    """Minimal db surface verify_signed_body needs."""
    def __init__(self, ident):
        self.ident = ident
        self.nonces = set()

    def get_identity(self, fm_id):
        return self.ident if fm_id == self.ident["fm_id"] else None

    def note_nonce(self, nonce, ttl):
        if nonce in self.nonces:
            return False
        self.nonces.add(nonce)
        return True


def _fresh_identity():
    priv = Ed25519PrivateKey.generate()
    priv_b64 = b64u_encode(priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption()))
    pub_b64 = b64u_encode(priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    ident = {"fm_id": "fm_testbool1", "handle": "boolprobe", "public_key": pub_b64}
    return priv_b64, ident


def _multipart_transport(body):
    """Simulate what requests does with data={...}: every value str()-ified."""
    return {k: (v if isinstance(v, str) else str(v)) for k, v in body.items()}


def test_multipart_bool_field_verifies():
    """The signed_body helper's bool fields must survive multipart transport."""
    priv_b64, ident = _fresh_identity()
    db = StubDB(ident)
    # Client builds the body with the shipped helper, exactly as documented.
    body = signed_body(priv_b64, "upload", ident["fm_id"],
                       file_sha256="abc123", ai_generated=True)
    # Multipart transport stringifies the bool: True -> "True".
    transported = _multipart_transport(body)
    assert transported["ai_generated"] == "True", "sanity: transport must str() the bool"
    try:
        verify_signed_body(transported, db, expected_action="upload")
    except IdentityError as e:
        raise AssertionError(
            "multipart transport broke the signature on a bool field: %s "
            "(client signed canonical 'true', server rebuilt from 'True')" % e)


def test_json_bool_field_still_verifies():
    """Positive control: JSON transport keeps types, must keep verifying."""
    priv_b64, ident = _fresh_identity()
    db = StubDB(ident)
    body = signed_body(priv_b64, "upload", ident["fm_id"],
                       file_sha256="abc123", ai_generated=True)
    assert body["ai_generated"] is True
    verify_signed_body(body, db, expected_action="upload")  # must not raise


if __name__ == "__main__":
    fails = []
    for name, fn in [("multipart bool verifies", test_multipart_bool_field_verifies),
                     ("json bool control", test_json_bool_field_still_verifies)]:
        try:
            fn()
            print("PASS", name)
        except Exception as e:
            fails.append(name)
            print("FAIL", name, "->", e)
    print()
    print(len(fails), "failed" if fails else "all passed")
    print("FAILURES:", fails)
    sys.exit(1 if fails else 0)
