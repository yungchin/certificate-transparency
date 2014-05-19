"""Verify CT log statements."""

import hashlib
import io
import struct

from ct.crypto import error
from ct.crypto import merkle
from ct.crypto import pem
from ct.proto import client_pb2
from ct.proto import ct_pb2
import ecdsa


class LogVerifier(object):
    """CT log verifier."""
    __ECDSA_READ_MARKERS = ("PUBLIC KEY", "ECDSA PUBLIC KEY")
    __ECDSA_WRITE_MARKER = "ECDSA PUBLIC KEY"

    def __init__(self, key_info, merkle_verifier=merkle.MerkleVerifier()):
        """Initialize from KeyInfo protocol buffer and a MerkleVerifier."""
        self.__merkle_verifier = merkle_verifier
        if key_info.type != client_pb2.KeyInfo.ECDSA:
            raise error.UnsupportedAlgorithmError("Key type %d not supported" %
                                                  key_info.type)

        # Will raise a PemError on invalid encoding
        self.__der, _ = pem.from_pem(key_info.pem_key,
                                     LogVerifier.__ECDSA_READ_MARKERS)
        try:
            self.__pubkey = ecdsa.VerifyingKey.from_der(self.__der)
        except ecdsa.der.UnexpectedDER as e:
            raise error.EncodingError(e)

    def __repr__(self):
        return "%r(public key: %r)" % (self.__class__.__name__,
                                       pem.to_pem(self.__der,
                                                  self.__ECDSA_WRITE_MARKER))

    def __str__(self):
        return "%s(public key: %s)" % (self.__class__.__name__,
                                       pem.to_pem(self.__der,
                                                  self.__ECDSA_WRITE_MARKER))

    def _encode_sth_input(self, sth_response):
        if len(sth_response.sha256_root_hash) != 32:
            raise error.EncodingError("Wrong hash length: expected 32, got %d" %
                                      len(sth_response.sha256_root_hash))
        return struct.pack(">BBQQ32s", ct_pb2.V1, ct_pb2.TREE_HEAD,
                           sth_response.timestamp, sth_response.tree_size,
                           sth_response.sha256_root_hash)

    def _decode_signature(self, signature):
        """Decode the TLS-encoded serialized signature.

        Args:
            signature: TLS-encoded signature.

        Returns:
            the inner, ASN.1 encoded contents.

        Raises:
            ct.crypto.error.EncodingError: invalid TLS encoding.
        """

        sig_stream = io.BytesIO(signature)

        sig_prefix = sig_stream.read(2)
        if len(sig_prefix) != 2:
            raise error.EncodingError("Invalid algorithm prefix %s" %
                                      sig_prefix.encode("hex"))
        hash_algo, sig_algo = struct.unpack(">BB", sig_prefix)
        if (hash_algo != ct_pb2.DigitallySigned.SHA256 or
            sig_algo != ct_pb2.DigitallySigned.ECDSA):
            raise error.EncodingError("Invalid algorithm(s) %d, %d" %
                                      (hash_algo, sig_algo))

        length_prefix = sig_stream.read(2)
        if len(length_prefix) != 2:
            raise error.EncodingError("Invalid signature length prefix %s" %
                                      length_prefix.encode("hex"))
        sig_length, = struct.unpack(">H", length_prefix)
        remaining = sig_stream.read()
        if len(remaining) != sig_length:
            raise error.EncodingError("Invalid signature length %d for "
                                      "signature %s with length %d" %
                                      (sig_length, remaining.encode("hex"),
                                       len(remaining)))
        return remaining

    def _verify(self, signature_input, signature):
        try:
            return self.__pubkey.verify(signature, signature_input,
                                        hashfunc=hashlib.sha256,
                                        sigdecode=ecdsa.util.sigdecode_der)
        except ecdsa.der.UnexpectedDER:
            raise error.EncodingError("Invalid DER encoding for signature %s",
                                      signature.encode("hex"))
        except ecdsa.keys.BadSignatureError:
            raise error.SignatureError("Signature did not verify: %s",
                                       signature.encode("hex"))

    @error.returns_true_or_raises
    def verify_sth(self, sth_response):
        """Verify the STH Response.

        Args:
            sth_response: client_pb2.SthResponse proto. The response must have
                all fields present.

        Returns:
            True. The return value is enforced by a decorator and need not be
                checked by the caller.

        Raises:
            ct.crypto.error.EncodingError: failed to encode signature input,
                or decode the signature.
            ct.crypto.error.SignatureError: invalid signature.
        """
        signature_input = self._encode_sth_input(sth_response)
        signature = self._decode_signature(sth_response.tree_head_signature)
        return self._verify(signature_input, signature)

    @staticmethod
    @error.returns_true_or_raises
    def verify_sth_temporal_consistency(old_sth, new_sth):
        """Verify the temporal consistency for two STH responses.

        For two STHs, verify that the newer STH has bigger tree size.
        Does not verify STH signatures or consistency of hashes.

        Args:
            old_sth: client_pb2.SthResponse proto. The STH with the older
                timestamp must be supplied first.
            new_sth: client_pb2.SthResponse proto.

        Returns:
            True. The return value is enforced by a decorator and need not be
                checked by the caller.

        Raises:
            ct.crypto.error.ConsistencyError: STHs are inconsistent
            ValueError: "Older" STH is not older.
        """
        if old_sth.timestamp > new_sth.timestamp:
            raise ValueError("Older STH has newer timestamp (%d vs %d), did "
                             "you supply inputs in the wrong order?" %
                             (old_sth.timestamp, new_sth.timestamp))

        if (old_sth.timestamp == new_sth.timestamp and
            old_sth.tree_size != new_sth.tree_size):
            # Issuing two different STHs for the same timestamp is illegal,
            # even if they are otherwise consistent.
            raise error.ConsistencyError("Inconsistency: different tree sizes "
                                         "for the same timestamp")
        if (old_sth.timestamp < new_sth.timestamp and
            old_sth.tree_size > new_sth.tree_size):
            raise error.ConsistencyError("Inconsistency: older tree has bigger "
                                         "size")
        return True

    @error.returns_true_or_raises
    def verify_sth_consistency(self, old_sth, new_sth, proof):
        """Verify consistency of two STHs.

        Verify the temporal consistency and consistency proof for two STH
        responses. Does not verify STH signatures.

        Args:
            old_sth: client_pb2.SthResponse() proto. The STH with the older
                timestamp must be supplied first.
            new_sth: client_pb2.SthResponse() proto.
            proof: a list of SHA256 audit nodes.

        Returns:
            True. The return value is enforced by a decorator and need not be
                checked by the caller.

        Raises:
            ConsistencyError: STHs are inconsistent
            ProofError: proof is invalid
            ValueError: "Older" STH is not older.
        """
        self.verify_sth_temporal_consistency(old_sth, new_sth)
        self.__merkle_verifier.verify_tree_consistency(
            old_sth.tree_size, new_sth.tree_size, old_sth.sha256_root_hash,
            new_sth.sha256_root_hash, proof)
        return True
