"""Mirror-node replay comparison (audit 2026-11-12).

The HCS topic holds the anchored messages, but nothing in the tree
could read them back and check them against the local log. The
comparison is pure (no network in these tests): mirror payloads are
decoded and matched to local messages by message_hash.
"""

from __future__ import annotations

import base64
import hashlib
import json

from joulex.attestation import canonical_bytes
from scripts.hcs_mirror_replay import compare_messages, decode_mirror_message


def _local_msg(seq, payload):
    body = {
        "topic": "joulex-actions",
        "sequence": seq,
        "consensus_timestamp": "2026-11-10T12:00:00.000000000Z",
        "sender": "agent-a",
        "action": "place_order",
        "payload_hash": hashlib.sha256(canonical_bytes(payload)).hexdigest(),
        "payload": payload,
        "attestation": None,
        "provenance": {"told_by": "exchange", "known_facts": []},
        "prev_hash": "0" * 64,
    }
    body["message_hash"] = hashlib.sha256(canonical_bytes(body)).hexdigest()
    return body


def _mirror_raw(msg, seq):
    raw = base64.b64encode(canonical_bytes(msg)).decode("ascii")
    return {
        "consensus_timestamp": "1794335407.470921104",
        "message": raw,
        "sequence_number": seq,
        "topic_id": "0.0.11399235",
        "running_hash": "abc",
    }


def test_decode_mirror_message():
    msg = _local_msg(1, {"x": 1})
    decoded, error = decode_mirror_message(_mirror_raw(msg, 1))
    assert error is None
    assert decoded == msg
    _, error = decode_mirror_message({"message": "!!!not-base64"})
    assert error is not None
    _, error = decode_mirror_message({"message": base64.b64encode(b"not json").decode()})
    assert error is not None


def test_compare_all_matched():
    local = [_local_msg(1, {"x": 1}), _local_msg(2, {"x": 2})]
    mirror = [_mirror_raw(m, i + 1) for i, m in enumerate(local)]
    report = compare_messages(local, mirror, receipted_seqs={1, 2})
    assert report["ok"] is True
    assert report["matched"] == 2
    assert report["mirror_only"] == []
    assert report["local_only"] == []
    assert report["local_only_with_receipt"] == []
    assert report["undecodable"] == []


def test_compare_local_only_without_receipt_is_not_a_failure():
    # A local-only message (never submitted) is expected, not a break.
    local = [_local_msg(1, {"x": 1}), _local_msg(2, {"x": 2})]
    mirror = [_mirror_raw(local[0], 1)]
    report = compare_messages(local, mirror, receipted_seqs={1})
    assert report["ok"] is True
    assert report["local_only"] == [2]
    assert report["local_only_with_receipt"] == []


def test_compare_receipted_but_missing_from_chain_is_a_failure():
    # A message WITH a success receipt that is absent from the topic
    # means the anchor is not where the receipt says it is.
    local = [_local_msg(1, {"x": 1}), _local_msg(2, {"x": 2})]
    mirror = [_mirror_raw(local[0], 1)]
    report = compare_messages(local, mirror, receipted_seqs={1, 2})
    assert report["ok"] is False
    assert report["local_only_with_receipt"] == [2]


def test_compare_mirror_only_message_is_a_failure():
    # Something on the topic that is not in the local log: an
    # unexpected poster (the testnet topic is open) or a lost local log.
    local = [_local_msg(1, {"x": 1})]
    stranger = _local_msg(9, {"x": 9})
    mirror = [_mirror_raw(local[0], 1), _mirror_raw(stranger, 2)]
    report = compare_messages(local, mirror, receipted_seqs={1})
    assert report["ok"] is False
    assert report["mirror_only"] == [2]


def test_compare_undecodable_mirror_message_is_a_failure():
    local = [_local_msg(1, {"x": 1})]
    mirror = [_mirror_raw(local[0], 1), {"message": "!!!", "sequence_number": 2}]
    report = compare_messages(local, mirror, receipted_seqs={1})
    assert report["ok"] is False
    assert report["undecodable"] == [2]
