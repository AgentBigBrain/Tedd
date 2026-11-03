"""Synthetic ISO9660 tests for the stopped Mac reader entitlement."""

from __future__ import annotations

import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

from platform_support import set_private_path_mode
from tedd_knowledge_graph.source_contracts import SourceArchiveError
from tedd_knowledge_graph.source_reader_cidata import ColdIsoBaseline, validate_cidata

BLOCK = 2048
META = b"instance-id: iid-1\nlocal-hostname: synthetic\n"
ENV = b"LIMA_CIDATA_IID=iid-1\nLIMA_CIDATA_UDP_DNS_LOCAL_PORT=0\nLIMA_CIDATA_TCP_DNS_LOCAL_PORT=0\n"
AGENT = b"trusted guest agent"
USER_DATA = b"#cloud-config\nusers: [synthetic]\n"
NETWORK_CONFIG = b"version: 2\nethernets: {}\n"


def _both32(number: int) -> bytes:
    return number.to_bytes(4, "little") + number.to_bytes(4, "big")


def _both16(number: int) -> bytes:
    return number.to_bytes(2, "little") + number.to_bytes(2, "big")


def _record(
    name: bytes, sector: int, length: int, *, directory: bool = False, susp: bytes = b""
) -> bytes:
    size = 33 + len(name) + (1 if len(name) % 2 == 0 else 0) + len(susp)
    data = bytearray(size)
    data[0] = size
    data[2:10] = _both32(sector)
    data[10:18] = _both32(length)
    data[25] = 2 if directory else 0
    data[28:32] = _both16(1)
    data[32] = len(name)
    data[33 : 33 + len(name)] = name
    if susp:
        data[-len(susp) :] = susp
    return bytes(data)


def synthetic_iso(
    *,
    meta: bytes = META,
    env: bytes = ENV,
    agent: bytes = AGENT,
    user_data: bytes | None = USER_DATA,
    network_config: bytes | None = NETWORK_CONFIG,
    extra: bool = False,
    duplicate: bool = False,
    link: bool = False,
    overlap: bool = False,
) -> bytes:
    image = bytearray(40 * BLOCK)
    root_records = [
        _record(b"\0", 20, BLOCK, directory=True),
        _record(b"\1", 20, BLOCK, directory=True),
        _record(b"META-DATA;1", 21, len(meta), susp=b"SL\x04\x01" if link else b""),
        _record(b"LIMA.ENV;1", 21 if overlap else 22, len(env)),
        _record(b"ETC", 23, BLOCK, directory=True),
    ]
    for name, sector, content in (
        (b"USER-DATA;1", 25, user_data),
        (b"NETWORK-CONFIG;1", 26, network_config),
    ):
        if content is not None:
            root_records.append(_record(name, sector, len(content)))
            image[sector * BLOCK : sector * BLOCK + len(content)] = content
    if duplicate:
        root_records.append(_record(b"LIMA.ENV;1", 22, len(env)))
    if extra:
        root_records.append(_record(b"UNTRUSTED;1", 27, 1))
        image[27 * BLOCK] = ord("x")
    image[20 * BLOCK : 20 * BLOCK + sum(map(len, root_records))] = b"".join(root_records)
    nested = b"".join(
        (
            _record(b"\0", 23, BLOCK, directory=True),
            _record(b"\1", 20, BLOCK, directory=True),
            _record(b"GUESTAGENT;1", 24, len(agent)),
        )
    )
    image[23 * BLOCK : 23 * BLOCK + len(nested)] = nested
    for sector, content in ((21, meta), (22, env), (24, agent)):
        image[sector * BLOCK : sector * BLOCK + len(content)] = content
    descriptor = 16 * BLOCK
    image[descriptor] = 1
    image[descriptor + 1 : descriptor + 6] = b"CD001"
    image[descriptor + 6] = 1
    image[descriptor + 40 : descriptor + 72] = b"cidata".ljust(32, b" ")
    image[descriptor + 80 : descriptor + 88] = _both32(40)
    image[descriptor + 120 : descriptor + 124] = _both16(1)
    image[descriptor + 124 : descriptor + 128] = _both16(1)
    image[descriptor + 128 : descriptor + 132] = _both16(BLOCK)
    image[descriptor + 156 : descriptor + 190] = _record(b"\0", 20, BLOCK, directory=True)
    terminator = 17 * BLOCK
    image[terminator] = 255
    image[terminator + 1 : terminator + 6] = b"CD001"
    image[terminator + 6] = 1
    return bytes(image)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ColdIsoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix=".tedd-cidata-", dir=Path.home())
        self.addCleanup(self.temporary.cleanup)
        set_private_path_mode(Path(self.temporary.name), 0o700)
        self.path = Path(self.temporary.name) / "cidata.iso"
        self.path.write_bytes(synthetic_iso())
        set_private_path_mode(self.path, 0o600)
        self.baseline = ColdIsoBaseline(
            {
                "etc/guestagent": _sha(AGENT),
                "user-data": _sha(USER_DATA),
                "network-config": _sha(NETWORK_CONFIG),
            },
            _sha(b"instance-id: <iid>\nlocal-hostname: synthetic\n"),
            _sha(
                b"LIMA_CIDATA_IID=<iid>\nLIMA_CIDATA_UDP_DNS_LOCAL_PORT=0\nLIMA_CIDATA_TCP_DNS_LOCAL_PORT=0\n"
            ),
        )

    def validate(self, iid: str = "iid-1") -> None:
        snapshot = validate_cidata(self.path, self.baseline, time.monotonic() + 5, None)
        self.assertEqual(snapshot.iid, iid)
        self.assertEqual(snapshot.sha256, _sha(self.path.read_bytes()))

    def test_exact_iso_passes_and_one_new_iid_is_allowed(self) -> None:
        self.validate()
        self.path.write_bytes(
            synthetic_iso(
                meta=META.replace(b"iid-1", b"iid-987"),
                env=ENV.replace(b"iid-1", b"iid-987"),
            )
        )
        self.validate("iid-987")

    def test_binary_control_bytes_are_preserved_in_the_iso_snapshot(self) -> None:
        agent = b"trusted\r\nguest\x1aagent\x00"
        image = synthetic_iso(agent=agent)
        self.path.write_bytes(image)
        baseline = ColdIsoBaseline(
            {**self.baseline.entries, "etc/guestagent": _sha(agent)},
            self.baseline.meta_data_normalized_sha256,
            self.baseline.lima_env_normalized_sha256,
        )
        snapshot = validate_cidata(self.path, baseline, time.monotonic() + 5, None)
        self.assertEqual(snapshot.sha256, _sha(image))

    def test_descriptor_and_path_timestamp_baselines_are_independent(self) -> None:
        info = self.path.lstat()
        fields = {
            name: getattr(info, name)
            for name in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        }
        # Windows Python versions can report different ctime clocks for these
        # two APIs even when the private file is unchanged.
        opened = SimpleNamespace(**{**fields, "st_ctime_ns": fields["st_ctime_ns"] + 1})
        with mock.patch("tedd_knowledge_graph.source_reader_cidata.os.fstat", return_value=opened):
            self.validate()

    def test_timestamp_changes_on_either_file_view_refuse(self) -> None:
        before = self.path.lstat()
        fields = {
            name: getattr(before, name)
            for name in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        }
        parent = self.path.parent.lstat()
        for view in ("descriptor", "path"):
            for field in ("st_mtime_ns", "st_ctime_ns"):
                changed = SimpleNamespace(**{**fields, field: fields[field] + 1})
                with (
                    self.subTest(view=view, field=field),
                    mock.patch(
                        "tedd_knowledge_graph.source_reader_cidata.source_files.check_entry",
                        side_effect=(parent, before),
                    ),
                    mock.patch(
                        "tedd_knowledge_graph.source_reader_cidata.os.fstat",
                        side_effect=(before, changed if view == "descriptor" else before),
                    ),
                    mock.patch.object(
                        Path, "lstat", return_value=changed if view == "path" else before
                    ),
                    self.assertRaisesRegex(SourceArchiveError, "source_unavailable"),
                ):
                    self.validate()

    def test_changed_hostname_or_extra_file_refuses(self) -> None:
        for image in (
            synthetic_iso(meta=META.replace(b"synthetic", b"alternate")),
            synthetic_iso(extra=True),
            synthetic_iso(agent=b"altered guest agent"),
        ):
            with self.subTest(image=_sha(image)):
                self.path.write_bytes(image)
                with self.assertRaises(SourceArchiveError):
                    self.validate()

    def test_nul_padded_cidata_volume_label_passes(self) -> None:
        image = bytearray(synthetic_iso())
        image[16 * BLOCK + 40 : 16 * BLOCK + 72] = b"cidata".ljust(32, b"\0")
        self.path.write_bytes(image)
        self.validate()

    def test_logical_directory_length_can_span_partial_blocks(self) -> None:
        image = bytearray(synthetic_iso())
        # Relocate the directory so its second allocated block is not a file.
        image[30 * BLOCK : 31 * BLOCK] = image[20 * BLOCK : 21 * BLOCK]
        record = _record(b"\0", 30, 2420, directory=True)
        image[16 * BLOCK + 156 : 16 * BLOCK + 190] = record
        image[30 * BLOCK : 30 * BLOCK + 34] = record
        image[30 * BLOCK + 34 : 30 * BLOCK + 68] = _record(b"\1", 30, 2420, directory=True)
        # Put an existing file record in the partial final block.
        start = image.find(b"LIMA.ENV;1", 30 * BLOCK) - 33
        length = image[start]
        image[31 * BLOCK : 31 * BLOCK + length] = image[start : start + length]
        image[start : 31 * BLOCK] = image[start + length : 31 * BLOCK] + bytes(length)
        self.path.write_bytes(image)
        self.validate()

    def test_rock_ridge_name_allows_one_alignment_nul(self) -> None:
        image = bytearray(synthetic_iso())
        start = 23 * BLOCK + 68
        record = _record(b"GUESTAGENT;1", 24, len(AGENT), susp=b"NM\x0f\x01\0guestagent\0")
        image[start : start + len(record)] = record
        self.path.write_bytes(image)
        self.validate()

    def test_partial_directory_cannot_consume_padding_or_overlap(self) -> None:
        for label, sector, size in (
            ("overlap", 20, BLOCK + 1),
            ("record past logical end", 23, 69),
            ("past volume", 39, BLOCK + 1),
        ):
            with self.subTest(label=label):
                image = bytearray(synthetic_iso())
                start = image.find(b"ETC") - 33
                image[start : start + image[start]] = _record(b"ETC", sector, size, directory=True)
                self.path.write_bytes(image)
                with self.assertRaises(SourceArchiveError):
                    self.validate()

    def test_nonpadding_label_and_rock_ridge_trailer_refuse(self) -> None:
        for padding in (b"x", b"\0\0", b"\0x"):
            image = bytearray(synthetic_iso())
            start = 23 * BLOCK + 68
            record = _record(
                b"GUESTAGENT;1", 24, len(AGENT), susp=b"NM\x0f\x01\0guestagent" + padding
            )
            image[start : start + len(record)] = record
            self.path.write_bytes(image)
            with self.assertRaises(SourceArchiveError):
                self.validate()
        for label in (b"cidata\0hidden", b"cidata2", b"CIDATA"):
            image = bytearray(synthetic_iso())
            image[16 * BLOCK + 40 : 16 * BLOCK + 72] = label.ljust(32, b"\0")
            self.path.write_bytes(image)
            with self.assertRaises(SourceArchiveError):
                self.validate()

    def test_nonzero_dns_port_or_iid_mismatch_refuses(self) -> None:
        for image in (
            synthetic_iso(env=ENV.replace(b"UDP_DNS_LOCAL_PORT=0", b"UDP_DNS_LOCAL_PORT=53")),
            synthetic_iso(env=ENV.replace(b"TCP_DNS_LOCAL_PORT=0", b"TCP_DNS_LOCAL_PORT=53")),
            synthetic_iso(env=ENV.replace(b"iid-1", b"iid-2")),
        ):
            with self.subTest(image=_sha(image)):
                self.path.write_bytes(image)
                with self.assertRaises(SourceArchiveError):
                    self.validate()

    def test_lima_generated_dns_keys_require_exact_zero_even_if_misenrolled(self) -> None:
        env = (
            b"LIMA_CIDATA_IID=iid-1\nLIMA_CIDATA_UDP_DNS_LOCAL_PORT=0\n"
            b"LIMA_CIDATA_TCP_DNS_LOCAL_PORT=0\n"
        )
        self.path.write_bytes(synthetic_iso(env=env))
        baseline = ColdIsoBaseline(
            dict(self.baseline.entries),
            self.baseline.meta_data_normalized_sha256,
            _sha(env.replace(b"iid-1", b"<iid>")),
        )
        validate_cidata(self.path, baseline, time.monotonic() + 5, None)
        for bad in (
            env.replace(b"UDP_DNS_LOCAL_PORT=0", b"UDP_DNS_LOCAL_PORT=53"),
            env.replace(b"TCP_DNS_LOCAL_PORT=0", b"TCP_DNS_LOCAL_PORT=53"),
            env + b"LIMA_CIDATA_UDP_DNS_LOCAL_PORT=0\n",
            env.replace(b"LIMA_CIDATA_UDP_DNS_LOCAL_PORT=0\n", b""),
            env.replace(b"UDP_DNS_LOCAL_PORT=0", b"UDP_DNS_LOCAL_PORT=53")
            + b"UDP_DNS_LOCAL_PORT=0\nTCP_DNS_LOCAL_PORT=0\n",
        ):
            with self.subTest(env=_sha(bad)):
                self.path.write_bytes(synthetic_iso(env=bad))
                unsafe_baseline = ColdIsoBaseline(
                    dict(baseline.entries),
                    baseline.meta_data_normalized_sha256,
                    _sha(bad.replace(b"iid-1", b"<iid>")),
                )
                with self.assertRaises(SourceArchiveError):
                    validate_cidata(self.path, unsafe_baseline, time.monotonic() + 5, None)

    def test_nonzero_dns_port_refuses_even_if_baseline_was_misenrolled(self) -> None:
        env = ENV.replace(b"UDP_DNS_LOCAL_PORT=0", b"UDP_DNS_LOCAL_PORT=53")
        self.path.write_bytes(synthetic_iso(env=env))
        baseline = ColdIsoBaseline(
            dict(self.baseline.entries),
            self.baseline.meta_data_normalized_sha256,
            _sha(env.replace(b"iid-1", b"<iid>")),
        )
        with self.assertRaises(SourceArchiveError):
            validate_cidata(self.path, baseline, time.monotonic() + 5, None)

    def test_link_duplicate_overlap_and_bad_descriptor_refuse(self) -> None:
        image = bytearray(synthetic_iso())
        image[16 * BLOCK] = 0  # boot descriptor, not the reviewed primary volume
        alternate = bytearray(synthetic_iso())
        alternate[18 * BLOCK : 18 * BLOCK + 7] = b"\x00CD001\x01"
        oversized = bytearray(synthetic_iso())
        length_field = oversized.find(b"GUESTAGENT;1") - 33 + 10
        oversized[length_field : length_field + 8] = _both32(65 * 1024 * 1024)
        for changed in (
            bytes(image),
            bytes(alternate),
            bytes(oversized),
            synthetic_iso(link=True),
            synthetic_iso(duplicate=True),
            synthetic_iso(overlap=True),
            synthetic_iso()[: 24 * BLOCK],
        ):
            self.path.write_bytes(changed)
            with self.assertRaises(SourceArchiveError):
                self.validate()

    def test_expired_deadline_refuses(self) -> None:
        self.path.write_bytes(synthetic_iso())
        with self.assertRaises(SourceArchiveError):
            validate_cidata(self.path, self.baseline, time.monotonic() - 1, None)

    def test_malformed_baseline_is_a_typed_refusal(self) -> None:
        with self.assertRaises(SourceArchiveError):
            ColdIsoBaseline(
                cast(dict[str, str], {"bad": 7}),
                self.baseline.meta_data_normalized_sha256,
                self.baseline.lima_env_normalized_sha256,
            )


if __name__ == "__main__":
    unittest.main()
