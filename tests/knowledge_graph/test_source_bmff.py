"""Neutral BMFF structure tests, not image-decoder or corpus qualification."""

from __future__ import annotations

import io
import json
import os
import struct
import sys
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from importlib.util import find_spec
from pathlib import Path
from typing import cast
from unittest.mock import patch

from tedd_knowledge_graph.source_contracts import SourceArchiveError

# Frozen self-authored Pillow 12.3/libavif 1.4.2 sequence: 16x8 red/green/blue,
# save_all=True, durations 100/200/300 ms, quality=100, speed=8, 4:4:4,
# max_threads=1. Only sample 1 is a sync frame; later frames depend on it.
# Tests decode these fixed bytes, never invoke the optional encoder.
AVIF_SEQUENCE = bytes.fromhex(
    "0000002c66747970617669730000000061766966617669736d73663169736f386d6966316d696166"
    "4d413141000000eb6d657461000000000000002168646c7200000000000000007069637400000000"
    "0000000000000000000000000e7069746d0000000000010000001e696c6f63000000004400000100"
    "0100000001000003f9000000270000002869696e660000000000010000001a696e66650200000000"
    "01000061763031436f6c6f72000000006a697072700000004b6970636f0000001469737065000000"
    "000000001000000008000000107069786900000000030808080000000c6176314381200000000000"
    "13636f6c726e636c780001000d0006800000001769706d6100000000000000010001040102830400"
    "0002da6d6f6f76000000786d7668640100000000000000e6cce76200000000e6cce762000003e8ff"
    "ffffffffffffff000100000100000000000000000000000001000000000000000000000000000000"
    "01000000000000000000000000000040000000000000000000000000000000000000000000000000"
    "000000000000010000025a7472616b00000068746b68640100000100000000e6cce76200000000e6"
    "cce7620000000100000000ffffffffffffffff000000000000000000000000000000000001000000"
    "00000000000000000000000001000000000000000000000000000040000000001000000008000000"
    "00002c6564747300000024656c737401000001000000010000000000000258000000000000000000"
    "010000000001be6d6469610000002c6d6468640100000000000000e6cce76200000000e6cce76200"
    "0003e8000000000000025855c400000000002168646c720000000000000000706963740000000000"
    "0000000000000000000001696d696e6600000014766d686400000001000000000000000000000024"
    "64696e660000001c6472656600000000000000010000000c75726c2000000001000001297374626c"
    "00000095737473640000000000000001000000856176303100000000000000010000000000000000"
    "00000000000000000010000800480000004800000000000000010a414f4d20436f64696e67000000"
    "0000000000000000000000000000000000000018ffff0000000c617631438120000000000013636f"
    "6c726e636c780001000d0006800000001063637374000000007c0000000000002873747473000000"
    "0000000003000000010000006400000001000000c8000000010000012c0000001c73747363000000"
    "0000000001000000010000000300000001000000207374737a000000000000000000000003000000"
    "270000001e0000001d000000147374636f0000000000000001000003f90000001473747373000000"
    "0000000001000000010000006a6d64617412000a0c2000000197f36be480868348321510008000b6"
    "fc328a7a00378a3dbc7c37b947781d701200321a30038080fdf87a000400b7b9e70415ca54071691"
    "7f69047aa0ca12003219300401057df87a000400b93ab1503f27c73fd329bfc6f08620"
)

# Frozen once from self-authored red+alpha and blue 8x8 images using the qualified
# pillow-heif 1.7.0/libheif 1.23.3 encoder (quality=-1, 4:4:4, lossless=True).
# Tests only decode these bytes. This is not an independently authored codec
# corpus, depth/gain-map fixture, or encoder-availability requirement.
HEVC_COLLECTION = bytes.fromhex(
    "0000001c6674797068656978000000006d696631686569786d6961660000029c6d657461"
    "000000000000002168646c72000000000000000070696374000000000000000000000000"
    "0000000046696c6f63000000004440000300010000000002c00001000000000000003400"
    "020000000002f40001000000000000001d00030000000003110001000000000000003300"
    "00004d69696e6600000000000300000015696e6665020000000001000068766331000000"
    "0015696e66650200000000020000687663310000000015696e6665020000000003000068"
    "766331000000000e7069746d000000000001000001b469707270000001846970636f0000"
    "0077687663430104080000000000000000001ef000fcfff8f800000f0360000100174001"
    "0c01ffff0408000003009e3800000300001eba0240610001002a4201010408000003009e"
    "3800000300001e90041020b2dd55d35cdc04341810000003019000000300108062000100"
    "084401c1731830189000000013636f6c726e636c780001000d0006800000001469737065"
    "00000000000000400000004000000028636c6170000000080000000100000008000000"
    "01ffffffc800000002ffffffc800000002000000107069786900000000030808080000"
    "0071687663430104080000000000000000001ef000fcfcf8f800000f0360000100174001"
    "0c01ffff0408000003009ff800000300001eba024061000100264201010408000003009f"
    "f800000300001ec08204165baaba6b9b02000003003200000300021062000100064401c1"
    "73c1890000000e7069786900000000010800000027617578430000000075726e3a6d7065"
    "673a686576633a323031353a61757869643a31000000002869706d610000000000000003"
    "0001058102030584000205860307888400030581020305840000001a6972656600000000"
    "0000000e6175786c0002000100010000008c6d646174000000302801af1321666340f810"
    "f767ffebbc15ff956b3ff337b1e9cec84740c0d220809b4048935d500b1610808776a556"
    "dcf8000000192801ae26472380ef367de228731beb37806230da4d63c740ec0000002f28"
    "01af1321646340f52722ffffb79d220fb40e47f65d9616772ba0c15bf2fac0f6649bc0f3"
    "aa919e808769a7301578"
)

# Exact libavif de87135613171d96b17107de370e9e1c8b337774 white_1x1.avif.
# Per-file provenance/license: tests/data/README.md at that immutable commit.
# See the adjacent libavif-fixture-LICENSE.txt. No test-time encoder or network.
AVIF_WHITE = bytes.fromhex(
    "00000020667479706176696600000000617669666d6966316d6961664d413141000000f2"
    "6d657461000000000000002868646c720000000000000000706963740000000000000000"
    "000000006c696261766966000000000e7069746d0000000000010000001e696c6f630000"
    "0000440000010001000000010000011a000000170000002869696e660000000000010000"
    "001a696e6665020000000001000061763031436f6c6f72000000006a697072700000004b"
    "6970636f0000001469737065000000000000000100000001000000107069786900000000"
    "030808080000000c617631438120000000000013636f6c726e636c780001000d00068000"
    "00001769706d610000000000000001000104010283040000001f6d64617412000a073800"
    "061010d069320a1ff03fffffc40000af70"
)

# Self-authored RGB256x256 red/green/blue/white quadrants, once encoded with
# approved Pillow12.3.0/libaom3.14.1: quality90, speed8, 4:4:4, max_threads1,
# autotiling=False, tile_rows=tile_cols=0 or1. The public dav1d probe confirms
# one or four coded tiles in ONE av01 item, not a grid. These are frozen
# synthetic controls, not an independent codec corpus; tests require no encoder.
AVIF_SINGLE_TILE_256 = bytes.fromhex(
    "00000020667479706176696600000000617669666d6966316d6961664d413141000000eb"
    "6d657461000000000000002168646c720000000000000000706963740000000000000000"
    "00000000000000000e7069746d0000000000010000001e696c6f63000000004400000100"
    "010000000100000113000000af0000002869696e660000000000010000001a696e666502"
    "0000000001000061763031436f6c6f72000000006a697072700000004b6970636f000000"
    "146973706500000000000001000000010000000010706978690000000003080808000000"
    "0c617631438120000000000013636f6c726e636c780001000d0006800000001769706d61"
    "000000000000000100010401028304000000b76d64617412000a09381dffffda404341a4"
    "329f01108090d2a0104107c80000cfcc0bd86bd3c785e6b144734d2799425b0b29b6ead8"
    "81eaf52a9fec5b3fa17a5bcbfcfa6ab61fb5bf6be67cc8e75c6f6b71f0f1c5217c344923"
    "229fa69bf1e8a1b93d32efb5f5be8798ed8465670b07671becc11bbd25e8bdd4bf0e4a28"
    "db4e7bf4df034fe19ae592ab7c2b671a70ab3f5e6631267f0b310b9b02c67efdd058b546"
    "27ca4e7b2f966b7f137826d0f272c895c340"
)

AVIF_FOUR_TILES_256 = bytes.fromhex(
    "00000020667479706176696600000000617669666d6966316d6961664d413141000000eb"
    "6d657461000000000000002168646c720000000000000000706963740000000000000000"
    "00000000000000000e7069746d0000000000010000001e696c6f63000000004400000100"
    "0100000001000001130000007c0000002869696e660000000000010000001a696e666502"
    "0000000001000061763031436f6c6f72000000006a697072700000004b6970636f000000"
    "146973706500000000000001000000010000000010706978690000000003080808000000"
    "0c617631438120000000000013636f6c726e636c780001000d0006800000001769706d61"
    "000000000000000100010401028304000000846d64617412000a09381dffffda404341a4"
    "326d1a4202434a8041041f2000000017cfcc0bd86bd3c785e6b144734d2799425b0b29b7"
    "056a446219cfcc1b1dcf76929b256d0b591a665fa3ec4cd7bb9cf6b24f3ee418cfcc1998"
    "b0c61f568d322a69ceb764ac28caa735bfa3e70ca5cfcc1b1dcf6ca7145f73738e5abe3b"
    "38b528"
)

# Same four-tile source with public advanced={'num-tile-groups': '2'}:
# OBU types [2,1,3,4,4], a separate frame header and two tile groups.
AVIF_TWO_TILE_GROUPS_256 = bytes.fromhex(
    "00000020667479706176696600000000617669666d6966316d6961664d413141000000eb"
    "6d657461000000000000002168646c720000000000000000706963740000000000000000"
    "00000000000000000e7069746d0000000000010000001e696c6f63000000004400000100"
    "010000000100000113000000860000002869696e660000000000010000001a696e666502"
    "0000000001000061763031436f6c6f72000000006a697072700000004b6970636f000000"
    "146973706500000000000001000000010000000010706978690000000003080808000000"
    "0c617631438120000000000013636f6c726e636c780001000d0006800000001769706d61"
    "0000000000000001000104010283040000008e6d64617412000a09381dffffda404341a4"
    "1a0c1a7202434a8041041f20001022378817000000cfcc0bd86bd3c785e6b144734d2799"
    "425b0b29b7056a4462cfcc1b1dcf76929b256d0b591a665fa3ec4cd7bb9cf6b24f3ee422"
    "30d818000000cfcc1998b0c61f568d322a69ceb764ac28caa735bfa3e70ca5cfcc1b1dcf"
    "6ca7145f73738e5abe3b38b528"
)

# Self-authored once-encoded 16x8 red-left/blue-right source, frozen by the
# independent reviewer. The clean-aperture mutation below changes no codec bytes.
HEVC_TWO_COLORS = bytes.fromhex(
    "0000001c6674797068656978000000006d696631686569786d6961660000017e6d657461"
    "000000000000002168646c72000000000000000070696374000000000000000000000000"
    "0000000022696c6f63000000004440000100010000000001a20001000000000000003f00"
    "00002369696e6600000000000100000015696e6665020000000001000068766331000000"
    "000e7069746d000000000001000000fe69707270000000de6970636f0000007768766343"
    "0104080000000000000000001ef000fcfff8f800000f03600001001740010c01ffff0408"
    "000003009e3800000300001eba0240610001002a4201010408000003009e380000030000"
    "1e90041020b2dd55d35cdc04341810000003019000000300108062000100084401c17318"
    "30189000000013636f6c726e636c780001000d0006800000001469737065000000000000"
    "00400000004000000028636c617000000010000000010000000800000001ffffffd000"
    "000002ffffffc800000002000000107069786900000000030808080000001869706d61"
    "00000000000000010001058102030584000000476d6461740000003b2801af13217c72c0"
    "e53303bfda74f844f930d5fcd3a2f31fa77d2fc9f320539fb6d860fa2258e8f4f81e8d"
    "ad460da0e5f10df92f9ca892f26844fb"
)


# Exact libavif fixture at the same pin/license as AVIF_WHITE. Two color tiles
# each have their own real alpha item; the grid clips the second tile.
AVIF_GRID = bytes.fromhex(
    "00000020667479706176696600000000617669666d6966316d6961664d4131410000024b6d657461"
    "000000000000002868646c720000000000000000706963740000000000000000000000006c696261"
    "766966000000000e7069746d00000000000100000056696c6f630000000044000005000100000001"
    "0000027300000008000200000001000005bb0000030d000300000001000008c80000007d00050000"
    "00010000027b0000024d000600000001000004c8000000f30000009069696e660000000000050000"
    "001a696e6665020000000001000067726964436f6c6f72000000001a696e66650200000100020000"
    "61763031436f6c6f72000000001a696e6665020000010003000061763031436f6c6f72000000001a"
    "696e6665020000010005000061763031416c706861000000001a696e666502000001000600006176"
    "3031416c706861000000003869726566000000000000001064696d6700010002000200030000000e"
    "6175786c0005000100020000000e6175786c000600010003000000eb69707270000000b16970636f"
    "00000014697370650000000000000050000000500000001070697869000000000308080800000013"
    "636f6c726e636c780001000d00068000000014697370650000000000000050000000400000000c61"
    "763143812000000000000e7069786900000000010800000038617578430000000075726e3a6d7065"
    "673a6d706567423a636963703a73797374656d733a617578696c696172793a616c70686100000000"
    "0c6176314381001c000000003269706d610000000000000005000103010203000204040285030003"
    "04040285030005040406880700060404068807000006da6d646174000001000050005012000a0618"
    "1967fec2a032c004100000dd2b2a9ed438363648225c81442b6d5f10085188f8d0148796efcf65d1"
    "64078cde1ece7ad12a9154de0331d0e2e825bc738593f9789b48feb7574e591384aa817309b478ce"
    "6ffffff8526074d4e3da62eebffc29d740bce735e52be7dcd0387360cd7fa66c555ffdb0d8a8dbda"
    "f0a17744e04e0e4a984fe24d07dbfbb28ac714a84e1384bfff2e72666570cc132947075f4d6a9e0d"
    "082613991389fed354be6127e16ee3912bcd51cc0579aa3fa1eb27bb48a1e8258d34e6b505b044f8"
    "deb753e56e01332450dcae6b7a0754f76528f45bb73c92b1448e332099cb54616c5993e09b94ed47"
    "27cc03c75366b5ce5a462a81d092284b5cdf9fb42116b37806d45d87713cb9352be3848e49d9133e"
    "da682f237a47044d452f0fc97fc0f8ec32a21d756cb3023cd7dd424caed62f31d7549d7d21fca1e0"
    "948d7a5f155971c8f40b1111f5a2debe79edf84a07e8274dcb4def62dae880bb3fce557c0e2eb48f"
    "a406151570cf4491f68c6739b5a163ce923548b822397cad0ab1960cbaf8515fa9b7c7083d34e6b6"
    "5763bc4fc512ef6b69b252478d91c08fccdd81359eabf06190e3adf91acf42b584881eb5e3485505"
    "83a2f27cd0625cacf2c9e2f681c2cfba4546dfd79aca8c1ff56ec850e75a39db7c6c69ec5c26a75c"
    "dbe5940bffc79cd270fa0bc114419618460a4425435f6edbd3f12d8b9376b627da17def3f5a1d21f"
    "89fc76ec5ab71ee3ada396035b1796d404424b8ea24be8dc0494b9620deed7add3646817c2787349"
    "4fe180a2a2f20c0132e07e5a5ccd0ab69a16c2722debb6b012000a06181967fec2a032e601100000"
    "dd558aac66a59b93b770cb7f82881de11452a53002fbf68957b32695901c61de2aa6e3d8afd0b44d"
    "33ccbb53b2136a0ee373fbea80187c1099167a634bcb42e5a83f0560e5dc85cacc96e31b0e27c06d"
    "2e266a5c1c8d43cf62cb955d3f70ab35ddc826f0d9992873e752bd772eb0e9e1b422f4fbc75e5431"
    "fa2c3884a581b69b31a43b53c4ee991e56364fa67f541b96e70ab91e2caa68202614a7b5ac6f5a7e"
    "57da4b44c76d4d1cdee40b8e888a4878d99e0b2da6cc96d73d3e37258d12300f8f1ffda693f94225"
    "b8200f9f77e9f3c1b1fb11ad1c7a82b0f5add769067eaeadfe364012000a09381967fec2021a0d20"
    "32fd051320030c30c280dd2b40afa742dae10a5853fa41b299fc0e2695a47195f3b7a5071c66b3f3"
    "32dded3153294e5d666afcc46964efb7ee825f5f70c6486ab334c3cd938d8b2cf68f836911dc7382"
    "bd66c3de1aff481223acf5b309b96f5ebcab61eede3b58b0fb0e68bd5074fb854110d31afc024836"
    "f56f9e25b42bc8baf963a0c0cdb42a55de610ded0f1d1a2acd2676bd41d647e5dde41e8891e9a4bc"
    "a0b648d71d5c3e66fb033b48e0f6c18ef83afbee33f22f5ab6af94a316593c8742b7a68010053b0b"
    "7072502597e846e70e82ceb852268a5e7477620fe685fcefddca5f9b6f5333e634a5b509236bfa20"
    "4cfe5875b0b7de71fbf30e628a7eeea8981c4c3486c23e56647851f7be02b72d013250b112bb9d9a"
    "b21b8757eaed37fd2836302d63feb24f5b1e846d8f54ede220e2d0ac4d99cf0f1ac892bf3865a9e2"
    "2d6ad2fb760d58bf5ee200d0c42ff5ce101b877ec319cfa5956fff9c595e9848f398243a02e8b2fa"
    "434cefdf2051b90e11d6b57a4b45c357382e48d8081fb876618a86e1ef5fd86b81c15f5216887352"
    "7ee29ba8a73b155e3deaf2995afde7a829b17f95e92c633f363815908af3fa1e8015e0d9f20b0fbe"
    "539e35d4eef31ea7abf88b608dabf88b7d3748af2704e39bee57a0cbe02dff0f0fffffed1da50fae"
    "7696c1c6ab4d1375dd4f7b122589819213dd10f463058192bb27b37470f96c315b8e2d19bbd5eabb"
    "8c9d40b7fddff704349ec6b7ee677e021301b05fb801e87915c2ef80f858210517108c5db7fffc39"
    "e42cbe556a2c5fa7a64d360e4d34bf183bb7b4a1321292a325eb45bd56987342da1f110a80ef5b8c"
    "95d7d3e5a3080c5307856d1c9b365e60e7c1459be3e2069349048276ac6c448b3f364693c677bee6"
    "7b1ac7057258df4774b809da0952e1df41b0ec5cd5fc10033ba601222fb4a469333630ceb303a316"
    "a987c1efca9fceec26b51970bcad371988a02eef2b1423b63ac21c4202987e0f630b124492f17c1c"
    "5a9f41cfac3ab94dd51f9d202c508879fca8e29862af21afc34d305594418e3eed640ef47441198b"
    "22c6f4bcbfbc336012000a09381967fec2021a0d20326e1320030c30c280dd46e6fea72717fc7c8e"
    "2d20f4b99a80a963bc22c9acc8a5d654de6657259c175d5bba0e76ab43e5625f7629397f3741f52a"
    "171959fe05c6673d1365d64925432723feb8ee3d9d6577f130d458e93c4375de3fea23375a15cc05"
    "2556940874b7a31180c6578d18"
)

# Self-authored six-column/four-row color fixture, frozen by the independent
# reviewer. Decode-only tests must not require its original runtime encoder.
AVIF_SIX_COLORS = bytes.fromhex(
    "00000020667479706176696600000000617669666d6966316d6961664d413141000000eb"
    "6d657461000000000000002168646c720000000000000000706963740000000000000000"
    "00000000000000000e7069746d0000000000010000001e696c6f63000000004400000100"
    "010000000100000113000000510000002869696e660000000000010000001a696e666502"
    "0000000001000061763031436f6c6f72000000006a697072700000004b6970636f000000"
    "1469737065000000000000000600000004000000107069786900000000030808080000"
    "000c617631438120000000000013636f6c726e636c780001000d0006800000001769706d"
    "61000000000000000100010401028304000000596d64617412000a0838086ec2021a0d20"
    "3243100000f6c6241b36acb23194dc9053ca3f17bbff413fbac91a8763e5259e7ffad48"
    "6e64abc075a742fa9ccf43e2be2bd8eb77329e164fdbbdfd8448e08b322ccd75fb8"
)


# Separate self-authored model-workflow control, frozen once using the existing
# avif_item_bytes builder around AVIF_SIX_COLORS' exact coded item. The original
# named fixture remains unchanged and requires owner-only metadata permission.
# This neutral test input is not a runtime sanitization/remediation route.
AVIF_SIX_COLORS_UNNAMED = bytes.fromhex(
    "0000001c667479706176696600000000617669666d6966316d696166000000f86d657461"
    "000000000000002168646c720000000000000000706963740000000000000000000000"
    "0000000000107069746d01000000000000010000002769696e66010000000000000100"
    "000017696e666503000000000000010000617630310000000024696c6f630200000044"
    "0000000001000000010000000000010000011c0000005100000070697072700000004b"
    "6970636f00000014697370650000000000000006000000040000001070697869000000"
    "00030808080000000c617631438120000000000013636f6c726e636c780001000d0006"
    "800000001d69706d61010000010000000100000001048001800280038004000000596d"
    "64617412000a0838086ec2021a0d203243100000f6c6241b36acb23194dc9053ca3f17"
    "bbff413fbac91a8763e5259e7ffad486e64abc075a742fa9ccf43e2be2bd8eb77329e"
    "164fdbbdfd8448e08b322ccd75fb8"
)


def hevc_ignored_tail() -> bytes:
    """Preserved independent suffix probe from the prior admission contract."""
    location = bytes.fromhex("000300000000031100010000000000000033")
    altered = HEVC_COLLECTION.replace(location, location[:-1] + b"\x43")
    altered = altered.replace(b"\0\0\0\x8cmdat", b"\0\0\0\x9cmdat")
    return altered[:-51] + b"\0\0\0\x3f" + altered[-47:] + b"synthetic-hidden"


def box(kind: bytes, value: bytes) -> bytes:
    return struct.pack("!I", len(value) + 8) + kind + value


def obu_suffix(data: bytes, index: int, suffix: bytes) -> bytes:
    """Change one frozen OBU's envelope length, never parse/rewrite its entropy."""
    parts: list[tuple[int, bytes]] = []
    cursor = 0
    while cursor < len(data):
        header = data[cursor]
        cursor += 1
        assert header & 2 and not header & 4  # These frozen fixtures have no extension.
        size, shift = 0, 0
        while True:
            value = data[cursor]
            cursor += 1
            size |= (value & 127) << shift
            if not value & 128:
                break
            shift += 7
        parts.append((header, data[cursor : cursor + size]))
        cursor += size
    assert cursor == len(data)
    header, body = parts[index]
    parts[index] = header, body + suffix
    result = bytearray()
    for header, body in parts:
        result.append(header)
        size = len(body)
        while size >= 128:
            result.append((size & 127) | 128)
            size >>= 7
        result.append(size)
        result.extend(body)
    return bytes(result)


def changed_avif_item(payload: bytes, item_id: int, data: bytes) -> bytes:
    from tedd_knowledge_graph.source_image_bmff import avif_item_bytes, inspect_bmff

    original = inspect_bmff(payload)
    changed = replace(
        original,
        items=tuple(
            replace(item, data=data) if item.item_id == item_id else item for item in original.items
        ),
    )
    return avif_item_bytes(changed, changed.primary_id)


def avif_movie_only() -> bytes:
    """Separate neutral original: same frozen samples, no poster/name/timestamps."""
    from tedd_knowledge_graph.source_contracts import digest

    assert AVIF_SEQUENCE[48:52] == b"meta" and AVIF_SEQUENCE[283:287] == b"moov"
    removed = 235
    raw = bytearray(AVIF_SEQUENCE[:44] + AVIF_SEQUENCE[279:])
    assert raw[973 - removed : 977 - removed] == b"stco"
    struct.pack_into("!I", raw, 985 - removed, 1017 - removed)
    for offset in (299, 427, 583):  # Version-1 mvhd/tkhd/mdhd creation + modification.
        raw[offset - removed : offset + 16 - removed] = bytes(16)
    raw[786 - removed : 818 - removed] = bytes(32)  # Empty compressor name.
    # Movie-only fixtures must not claim the still-item brands whose required
    # meta box was removed. The native pixel decoder enforces this distinction.
    raw[16:20], raw[32:36] = b"avis", b"msf1"
    result = bytes(raw)
    assert digest(result) == "0e2d3f1a18c414d113d8829ea144570a24f24414517d1bfd41655e2357982adf"
    return result


def avif_structure(*, property_bytes: bytes = b"", unused: bytes = b"") -> bytes:
    # Deliberately tiny placeholder OBUs: legal envelope, not valid AV1 samples.
    # A real decoder is still required after the container guard passes.
    pixels = b"\x0a\1\0\x32\1\0"
    ftyp = box(b"ftyp", b"avif\0\0\0\0mif1miafMA1B")
    hdlr = box(b"hdlr", bytes(8) + b"pict" + bytes(13))
    pitm = box(b"pitm", bytes(4) + b"\0\1")
    iinf = box(b"iinf", bytes(4) + b"\0\1" + box(b"infe", b"\2\0\0\0\0\1\0\0av01\0"))
    ipco = box(
        b"ipco",
        box(b"ispe", bytes(4) + struct.pack("!2I", 2, 1))
        + box(b"av1C", b"\x81\0\x0c\0")
        + box(b"pixi", b"\0\0\0\0\3\10\10\10")
        + property_bytes,
    )
    ipma = box(b"ipma", bytes(4) + struct.pack("!I", 1) + b"\0\1\3\x81\x82\3")

    def meta(offset: int) -> bytes:
        iloc = box(
            b"iloc", bytes(4) + b"\x44\0\0\1\0\1\0\0\0\1" + struct.pack("!2I", offset, len(pixels))
        )
        return box(b"meta", bytes(4) + hdlr + pitm + iinf + iloc + box(b"iprp", ipco + ipma))

    return ftyp + meta(len(ftyp) + len(meta(0)) + 8) + box(b"mdat", pixels + unused)


@unittest.skipUnless(
    os.environ.get("TEDD_TEST_AV1_VALIDATOR"), "optional native AV1 qualification not configured"
)
class Av1HostQualificationTests(unittest.TestCase):
    """The dedicated run sets the path; missing or wrong native artifacts fail."""

    def test_sequence_worker_reviews_poster_and_each_dependent_frame(self) -> None:
        from PIL import Image
        from tedd_knowledge_graph.source_contracts import digest
        from tedd_knowledge_graph.source_images import ImageReading

        from tests.knowledge_graph.test_source_office import office_worker

        arguments = ("--av1-validator", os.environ["TEDD_TEST_AV1_VALIDATOR"])
        for position, expected in enumerate(
            ((255, 0, 0), (255, 0, 0), (0, 255, 0), (0, 0, 255)), 1
        ):
            with self.subTest(position=position):
                result = office_worker(
                    AVIF_SEQUENCE, "image", "--render-unit", str(position), *arguments
                )
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(result.stderr, b"")
                reading = ImageReading.decode(AVIF_SEQUENCE, json.loads(result.stdout))
                self.assertEqual(reading.inventory.original_sha256, digest(AVIF_SEQUENCE))
                self.assertEqual(
                    tuple(unit.selector for unit in reading.inventory.units),
                    ("item:root:0", "frame:root:1", "frame:root:2", "frame:root:3"),
                )
                self.assertIn("animation_timing_not_reproduced", reading.document.gaps)
                self.assertEqual(reading.inventory.metadata_categories, ("description",))
                assert reading.png is not None
                with io.BytesIO(reading.png) as stream, Image.open(stream) as image:
                    self.assertEqual(image.size, (16, 8))
                    self.assertEqual(image.info, {})
                    # Self-authored solid primaries; small lossy YUV conversion
                    # error is expected, but a missing/wrong frame cannot pass.
                    pixels = image.tobytes()
                    for channel, value in enumerate(expected):
                        self.assertTrue(
                            all(abs(sample - value) <= 2 for sample in pixels[channel::4])
                        )
                    self.assertEqual(pixels[3::4], bytes([255]) * 128)

    def test_bad_last_sequence_sample_prevents_poster_rendering(self) -> None:
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff

        from tests.knowledge_graph.test_source_office import office_worker

        last = AVIF_SEQUENCE[1086:]
        changed = obu_suffix(last, -1, b"\x01")
        altered = bytearray(AVIF_SEQUENCE[:1086] + changed)
        struct.pack_into("!I", altered, 965, len(changed))  # stsz's third sample.
        struct.pack_into("!I", altered, 1009, len(altered) - 1009)  # mdat.
        payload = bytes(altered)
        self.assertEqual(len(inspect_bmff(payload).tracks[0].samples), 3)
        result = office_worker(
            payload,
            "image",
            "--render-unit",
            "1",
            "--av1-validator",
            os.environ["TEDD_TEST_AV1_VALIDATOR"],
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, b"")
        self.assertEqual(json.loads(result.stdout), {"status": "source_corrupt"})

    def test_still_or_late_auxiliary_configuration_mismatch_prevents_preview(self) -> None:
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff

        from tests.knowledge_graph.test_source_office import office_worker

        arguments = ("--av1-validator", os.environ["TEDD_TEST_AV1_VALIDATOR"])
        for label, source in (("still", AVIF_SIX_COLORS_UNNAMED), ("last-auxiliary", AVIF_GRID)):
            with self.subTest(case=label):
                control = office_worker(source, "image", "--render-unit", "1", *arguments)
                self.assertEqual(control.returncode, 0, control.stdout)
                self.assertEqual(control.stderr, b"")
                altered = bytearray(source)
                # Only the last property's declared level changes. The coded
                # pictures remain valid; the mismatch must prevent any preview.
                altered[source.rindex(b"av1C") + 5] ^= 1
                baseline = inspect_bmff(source)
                changed = inspect_bmff(bytes(altered))
                self.assertEqual(
                    tuple(item.data for item in baseline.items),
                    tuple(item.data for item in changed.items),
                )
                self.assertEqual(changed.metadata_categories, baseline.metadata_categories)
                rejected = office_worker(bytes(altered), "image", "--render-unit", "1", *arguments)
                self.assertEqual(rejected.returncode, 1)
                self.assertEqual(rejected.stderr, b"")
                self.assertEqual(json.loads(rejected.stdout), {"status": "source_corrupt"})

    def test_still_and_sequence_configuration_must_match_decoded_public_headers(self) -> None:
        from tedd_knowledge_graph.source_av1_host import Av1Validator

        validator = Av1Validator(Path(os.environ["TEDD_TEST_AV1_VALIDATOR"]))
        for source in (AVIF_SIX_COLORS_UNNAMED, AVIF_SEQUENCE):
            config = source.rindex(b"av1C") + 4
            self.assertEqual(source[config : config + 4], bytes.fromhex("81200000"))
            for label, offset, value in (
                ("profile", 1, 0),
                ("level", 1, 0x21),
                ("tier", 2, 0x80),
                ("high-bitdepth", 2, 0x40),
                ("twelve-bit", 2, 0x20),
                ("monochrome", 2, 0x10),
                ("horizontal-subsampling", 2, 8),
                ("vertical-subsampling", 2, 4),
                ("chroma-position", 2, 1),
                ("reserved-delay", 3, 1),
            ):
                with self.subTest(source=len(source), field=label):
                    altered = bytearray(source)
                    altered[config + offset] = value
                    with self.assertRaises(SourceArchiveError):
                        validator.inspect(bytes(altered))

    def test_sequence_protocol_keeps_references_and_rejects_bad_sync_or_late_data(self) -> None:
        import subprocess

        from tedd_knowledge_graph.source_av1_host import _VALIDATOR_SHA256
        from tedd_knowledge_graph.source_native_host import pinned_helper_files

        path = Path(os.environ["TEDD_TEST_AV1_VALIDATOR"])
        samples = (AVIF_SEQUENCE[1017:1056], AVIF_SEQUENCE[1056:1086], AVIF_SEQUENCE[1086:1115])

        def packet(parts: tuple[bytes, ...], sync: tuple[int, ...]) -> bytes:
            return (
                b"TEDDAV1\x02"
                + struct.pack("!2I", 1, len(parts))
                + bytes.fromhex("81200000")
                + b"".join(
                    struct.pack("!4I", len(part), 16, 8, key) + part
                    for part, key in zip(parts, sync, strict=True)
                )
            )

        valid = packet(samples, (1, 0, 0))
        malformed = []
        for label, offset, value in (
            ("zero-streams", 8, 0),
            ("stream-limit", 8, 257),
            ("zero-samples", 12, 0),
            ("sample-limit", 12, 257),
            ("zero-size", 20, 0),
            ("encoded-limit", 20, 64 * 1024**2 + 1),
            ("zero-width", 24, 0),
            ("axis-limit", 24, 131073),
            ("wrong-width", 24, 15),
            ("zero-height", 28, 0),
            ("wrong-sync-type", 32, 2),
        ):
            changed = bytearray(valid)
            struct.pack_into("!I", changed, offset, value)
            malformed.append((label, bytes(changed), False))
        malformed.extend(
            (f"truncated-{size}", valid[:size], False)
            for size in (0, 7, 8, 11, 12, 15, 16, 19, 20, 35, 74, len(valid) - 1)
        )
        with pinned_helper_files(((path, _VALIDATOR_SHA256),)):
            for label, raw, accepted in (
                ("dependent-frames", valid, True),
                ("first-not-sync", packet(samples, (0, 0, 0)), False),
                ("inter-frame-labelled-sync", packet(samples, (1, 1, 0)), False),
                ("missing-reference", packet(samples[1:], (1, 0)), False),
                (
                    "late-bad-data",
                    packet((*samples[:-1], obu_suffix(samples[-1], -1, b"\x01")), (1, 0, 0)),
                    False,
                ),
                ("extra-outer-bytes", valid + b"extra", False),
                *malformed,
            ):
                with self.subTest(case=label):
                    result = subprocess.run(
                        [str(path)],
                        input=raw,
                        capture_output=True,
                        timeout=10,
                        check=False,
                        cwd=path.parent,
                        env={
                            key: os.environ[key]
                            for key in ("SystemRoot", "WINDIR")
                            if key in os.environ
                        },
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    self.assertEqual(result.returncode, 0 if accepted else 1)
                    self.assertEqual(result.stderr, b"")
                    self.assertEqual(
                        result.stdout,
                        b"TEDDAV1\x02" + struct.pack("!2I", 3, 384) if accepted else b"",
                    )

    def test_declared_depth_and_gain_map_planes_remain_individually_reviewable(self) -> None:
        from contextlib import closing

        from PIL import Image
        from tedd_knowledge_graph.source_contracts import digest
        from tedd_knowledge_graph.source_image_bmff import avif_item_bytes, inspect_bmff
        from tedd_knowledge_graph.source_images import ImageReading

        from tests.knowledge_graph.test_source_office import office_worker

        original = inspect_bmff(AVIF_GRID)
        arguments = ("--av1-validator", os.environ["TEDD_TEST_AV1_VALIDATOR"])
        # Neutral envelope variants reuse the frozen grayscale coded planes.
        # They qualify retained auxiliary inventory/pixels, not camera depth
        # calibration, HDR tone mapping, or a vendor's complete gain-map profile.
        for urn, expected_digest in (
            (
                b"urn:mpeg:mpegB:cicp:systems:auxiliary:depth",
                "df502a0ebcca71cdd9ad660de4992284d57df43ce34d71b180754988f0549d7d",
            ),
            (
                b"urn:com:apple:photo:2020:aux:hdrgainmap",
                "3ab9fca61776915ad2ac013d0783e3c7e9907c7de5d8a4dcf61cc5a8326f70a3",
            ),
        ):
            items = tuple(
                replace(
                    item,
                    properties=tuple(
                        (kind, bytes(4) + urn + b"\0" if kind == b"auxC" else value)
                        for kind, value in item.properties
                    ),
                )
                for item in original.items
            )
            payload = avif_item_bytes(replace(original, items=items), original.primary_id)
            self.assertEqual(digest(payload), expected_digest)
            admitted = inspect_bmff(payload)
            self.assertEqual(
                tuple(item.data for item in admitted.items), tuple(i.data for i in items)
            )
            for position in range(1, 6):
                with self.subTest(auxiliary=urn, position=position):
                    result = office_worker(
                        payload, "image", "--render-unit", str(position), *arguments
                    )
                    self.assertEqual(result.returncode, 0, result.stdout)
                    self.assertEqual(result.stderr, b"")
                    reading = ImageReading.decode(payload, json.loads(result.stdout))
                    self.assertEqual(
                        tuple(unit.selector for unit in reading.inventory.units),
                        (
                            "item:root:0",
                            "item:root:1",
                            "item:root:2",
                            "auxiliary:1:5",
                            "auxiliary:2:6",
                        ),
                    )
                    self.assertIn(
                        "auxiliary_pixels_not_calibrated_measurements", reading.document.gaps
                    )
                    assert reading.png is not None
                    with (
                        io.BytesIO(reading.png) as stream,
                        Image.open(stream) as image,
                        closing(image),
                    ):
                        self.assertEqual(image.info, {})
                        self.assertEqual(image.size, (80, 80) if position == 1 else (80, 64))
                        if position >= 4:
                            pixels = image.tobytes()
                            self.assertEqual(pixels[0::4], pixels[1::4])
                            self.assertEqual(pixels[0::4], pixels[2::4])
                            self.assertEqual(pixels[3::4], bytes([255]) * (80 * 64))
                            # Same frozen grayscale picture, now separately
                            # declared as depth/gain data, not silently applied
                            # as primary-image transparency.
                            baseline = office_worker(
                                AVIF_GRID, "image", "--render-unit", str(position), *arguments
                            )
                            self.assertEqual(baseline.returncode, 0, baseline.stdout)
                            control = ImageReading.decode(AVIF_GRID, json.loads(baseline.stdout))
                            self.assertEqual(reading.png, control.png)
            last = next(item for item in admitted.items if item.item_id == 6)
            damaged = changed_avif_item(payload, 6, obu_suffix(last.data, -1, b"\x01"))
            result = office_worker(damaged, "image", "--render-unit", "1", *arguments)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout), {"status": "source_corrupt"})
            self.assertEqual(result.stderr, b"")

    def test_avif_frame_tail_bytes_must_not_be_admitted(self) -> None:
        from tedd_knowledge_graph.source_av1_host import Av1Validator
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff

        validator = Av1Validator(Path(os.environ["TEDD_TEST_AV1_VALIDATOR"]))
        self.assertEqual(validator.inspect(AVIF_WHITE), inspect_bmff(AVIF_WHITE))
        self.assertEqual(AVIF_WHITE[-12:-10], b"\x32\x0a")
        altered = AVIF_WHITE.replace(
            bytes.fromhex("0000011a00000017"), bytes.fromhex("0000011a00000027")
        )
        altered = altered.replace(b"\0\0\0\x1fmdat", b"\0\0\0\x2fmdat")
        altered = altered[:-12] + b"\x32\x1a" + altered[-10:] + b"synthetic-hidden"
        self.assertEqual(len(altered), 321)
        # Same original regression bytes. The pure envelope guard makes no
        # entropy claim; real pinned strict decoding must reject this suffix.
        self.assertEqual(inspect_bmff(altered).format, "avif")
        with self.assertRaises(SourceArchiveError):
            validator.inspect(altered)

    def test_real_image_worker_requires_host_binding_and_renders_native_detail(self) -> None:
        from PIL import Image
        from tedd_knowledge_graph.source_images import ImageReading

        from tests.knowledge_graph.test_source_office import office_worker

        absent = office_worker(AVIF_SIX_COLORS, "image", "--render-unit", "1")
        self.assertEqual(absent.returncode, 1)
        self.assertEqual(json.loads(absent.stdout), {"status": "capability_unavailable"})
        arguments = ("--av1-validator", os.environ["TEDD_TEST_AV1_VALIDATOR"])
        overview = office_worker(AVIF_SIX_COLORS, "image", "--render-unit", "1", *arguments)
        self.assertEqual(overview.returncode, 0, overview.stdout)
        self.assertEqual(overview.stderr, b"")
        result = ImageReading.decode(AVIF_SIX_COLORS, json.loads(overview.stdout))
        self.assertEqual(result.inventory.format, "avif")
        self.assertEqual(result.inventory.units[0].size, (6, 4))
        detail = office_worker(
            AVIF_SIX_COLORS, "image", "--render-unit", "1", "--rectangle", "1,0,3,2", *arguments
        )
        self.assertEqual(detail.returncode, 0, detail.stdout)
        cropped = ImageReading.decode(AVIF_SIX_COLORS, json.loads(detail.stdout))
        assert cropped.png is not None
        with Image.open(io.BytesIO(cropped.png), formats=["PNG"]) as pixels:
            self.assertEqual(pixels.size, (2, 2))
            self.assertEqual(pixels.tobytes(), bytes((0, 255, 0, 255, 0, 0, 253, 255)) * 2)

    def test_strict_admission_accepts_each_frozen_packaging_and_all_grid_items(self) -> None:
        from tedd_knowledge_graph.source_av1_host import Av1Validator
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff

        validator = Av1Validator(Path(os.environ["TEDD_TEST_AV1_VALIDATOR"]))
        for payload in (
            AVIF_WHITE,
            AVIF_SIX_COLORS,
            AVIF_GRID,
            AVIF_SINGLE_TILE_256,
            AVIF_FOUR_TILES_256,
            AVIF_TWO_TILE_GROUPS_256,
        ):
            with self.subTest(size=len(payload)):
                self.assertEqual(validator.inspect(payload), inspect_bmff(payload))

    def test_length_adjusted_nonzero_suffixes_refuse_but_tile_zero_padding_passes(self) -> None:
        from tedd_knowledge_graph.source_av1_host import Av1Validator
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff

        validator = Av1Validator(Path(os.environ["TEDD_TEST_AV1_VALIDATOR"]))
        for source in (AVIF_WHITE, AVIF_SINGLE_TILE_256, AVIF_FOUR_TILES_256):
            item = inspect_bmff(source).items[0]
            for tail in (b"\0" * 16, b"\x01", b"\x80", b"\xff", b"synthetic-hidden"):
                altered = changed_avif_item(source, item.item_id, obu_suffix(item.data, -1, tail))
                with self.subTest(source=len(source), tail=tail):
                    if tail == b"\0" * 16:
                        self.assertEqual(validator.inspect(altered).format, "avif")
                    else:
                        with self.assertRaises(SourceArchiveError):
                            validator.inspect(altered)
        item = inspect_bmff(AVIF_TWO_TILE_GROUPS_256).items[0]
        for index in (1, 2, 3, 4):  # Sequence, frame header, first/last tile group.
            tails = (b"\x01",) if index < 3 else (b"\0" * 16, b"\0" * 15 + b"\x01")
            for tail in tails:
                altered = changed_avif_item(
                    AVIF_TWO_TILE_GROUPS_256, item.item_id, obu_suffix(item.data, index, tail)
                )
                with self.subTest(obu=index, tail=tail):
                    if tail == b"\0" * 16:
                        self.assertEqual(validator.inspect(altered).format, "avif")
                    else:
                        with self.assertRaises(SourceArchiveError):
                            validator.inspect(altered)

    def test_one_bad_grid_or_alpha_item_prevents_whole_container_admission(self) -> None:
        from tedd_knowledge_graph.source_av1_host import Av1Validator
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff

        validator = Av1Validator(Path(os.environ["TEDD_TEST_AV1_VALIDATOR"]))
        original = inspect_bmff(AVIF_GRID)
        self.assertEqual(tuple(item.item_id for item in original.items), (1, 2, 3, 5, 6))
        for item in original.items:
            if item.kind != b"av01":
                continue
            with self.subTest(item=item.item_id):
                altered = changed_avif_item(
                    AVIF_GRID, item.item_id, obu_suffix(item.data, -1, b"\x01")
                )
                # The pure container guard has no codec-completion claim.
                self.assertEqual(len(inspect_bmff(altered).items), 5)
                with self.assertRaises(SourceArchiveError):
                    validator.inspect(altered)


class BmffAdmissionTests(unittest.TestCase):
    def test_sequence_chunk_layouts_preserve_sample_order_and_exact_coded_bytes(self) -> None:
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff

        baseline = inspect_bmff(AVIF_SEQUENCE)
        original_samples = tuple(sample.data for sample in baseline.tracks[0].samples)
        for name, tag, offsets, sample_count in (
            ("three-chunks", b"stco", struct.pack("!3I", 1025, 1064, 1094), 1),
            ("wide-offset", b"co64", struct.pack("!Q", 1021), 3),
        ):
            with self.subTest(layout=name):
                count = 3 if name == "three-chunks" else 1
                replacement = box(tag, bytes(4) + struct.pack("!I", count) + offsets)
                growth = len(replacement) - 20
                changed = bytearray(AVIF_SEQUENCE[:969] + replacement + AVIF_SEQUENCE[989:])
                for start in (279, 407, 563, 648, 712):
                    length = int.from_bytes(AVIF_SEQUENCE[start : start + 4], "big")
                    struct.pack_into("!I", changed, start, length + growth)
                struct.pack_into("!I", changed, 125, 1017 + growth)  # Poster extent.
                struct.pack_into("!I", changed, 929, sample_count)
                inventory = inspect_bmff(bytes(changed))
                track = inventory.tracks[0]
                self.assertEqual(tuple(sample.data for sample in track.samples), original_samples)
                self.assertEqual(track.durations, (100, 200, 300))
                self.assertEqual(track.sync, (True, False, False))
                self.assertEqual(track.samples[0].data, inventory.items[0].data)

    def test_sequence_only_source_cannot_claim_missing_still_item_structure(self) -> None:
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff

        original = avif_movie_only()
        self.assertEqual(inspect_bmff(original).items, ())
        for offset, brand in ((16, b"avif"), (32, b"mif1")):
            with self.subTest(brand=brand):
                altered = bytearray(original)
                altered[offset : offset + 4] = brand
                with self.assertRaises(SourceArchiveError):
                    inspect_bmff(bytes(altered))

    def test_sequence_nonzero_timestamps_require_description_classification(self) -> None:
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff

        original = avif_movie_only()
        baseline = inspect_bmff(original)
        self.assertEqual(baseline.metadata_categories, ())
        expected_samples = tuple(sample.data for sample in baseline.tracks[0].samples)
        for tag in (b"mvhd", b"tkhd", b"mdhd"):
            position = original.index(tag)
            self.assertEqual(original[position + 4], 1)
            for field, offset in (("creation", 8), ("modification", 16)):
                with self.subTest(box=tag, field=field):
                    changed = bytearray(original)
                    struct.pack_into("!Q", changed, position + offset, 0xDFFD0001)
                    inventory = inspect_bmff(bytes(changed))
                    self.assertEqual(inventory.metadata_categories, ("description",))
                    self.assertEqual(
                        tuple(sample.data for sample in inventory.tracks[0].samples),
                        expected_samples,
                    )

    def test_sequence_tables_refuse_ambiguous_or_unaccounted_samples(self) -> None:
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff

        # Offsets address independently frozen boxes; no production parser or
        # encoder constructs the malformed declarations used by this test.
        for label, offset, value in (
            ("movie-timescale-zero", 315, bytes(4)),
            ("track-id-zero", 443, bytes(4)),
            ("fractional-track-width", 511, struct.pack("!I", (16 << 16) + 1)),
            ("track-timeline-mismatch", 451, struct.pack("!Q", 1)),
            ("edit-offset", 551, struct.pack("!Q", 1)),
            ("media-timescale-zero", 599, bytes(4)),
            ("media-duration-mismatch", 603, struct.pack("!Q", 601)),
            ("external-data-reference", 708, bytes(4)),
            ("description-count", 732, struct.pack("!I", 2)),
            ("sample-dimensions", 768, struct.pack("!H", 15)),
            ("zero-duration", 889, bytes(4)),
            ("expanded-sample-limit", 885, struct.pack("!I", 257)),
            ("first-chunk-not-one", 925, struct.pack("!I", 2)),
            ("too-many-chunk-samples", 929, struct.pack("!I", 4)),
            ("wrong-description-reference", 933, struct.pack("!I", 2)),
            ("size-count-mismatch", 953, struct.pack("!I", 2)),
            ("empty-last-sample", 965, bytes(4)),
            ("missing-media-byte", 965, struct.pack("!I", 28)),
            ("offset-into-header", 985, struct.pack("!I", 1009)),
            ("offset-beyond-source", 985, struct.pack("!I", 2000)),
            ("first-sample-not-sync", 1005, struct.pack("!I", 2)),
            ("composition-reordering", 993, b"ctts"),
            ("unknown-track-metadata", 857, b"XMP_"),
        ):
            with self.subTest(case=label):
                changed = bytearray(AVIF_SEQUENCE)
                changed[offset : offset + len(value)] = value
                with self.assertRaises(SourceArchiveError):
                    inspect_bmff(bytes(changed))

    def test_sequence_names_are_screened_without_exposing_values(self) -> None:
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff

        self.assertEqual(AVIF_SEQUENCE[786:797], b"\x0aAOM Coding")
        for name in (
            b"password=synthetic-only",
            b"Bearer TOPSECRET_SYNTHETIC",
            b"invalid-utf8\xff",
        ):
            with self.subTest(name_length=len(name)):
                changed = bytearray(AVIF_SEQUENCE)
                changed[786:818] = bytes([len(name)]) + name + bytes(31 - len(name))
                with self.assertRaises(SourceArchiveError):
                    inspect_bmff(bytes(changed))

        # Descriptive paths require confidential owner review, not the credential
        # refusal. No name value is returned in the metadata categories.
        name = b"C:\\Synthetic\\private"
        changed = bytearray(AVIF_SEQUENCE)
        changed[786:818] = bytes([len(name)]) + name + bytes(31 - len(name))
        self.assertEqual(inspect_bmff(bytes(changed)).metadata_categories, ("description",))

    def test_sequence_inventory_accounts_for_poster_samples_sync_and_timing(self) -> None:
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff

        inventory = inspect_bmff(AVIF_SEQUENCE)
        self.assertEqual(inventory.format, "avif")
        self.assertEqual(len(inventory.items), 1)
        self.assertEqual(len(inventory.tracks), 1)
        track = inventory.tracks[0]
        self.assertEqual((track.track_id, track.timescale), (1, 1000))
        self.assertEqual(track.durations, (100, 200, 300))
        self.assertEqual(track.sync, (True, False, False))
        self.assertEqual(track.spans, ((1017, 1056), (1056, 1086), (1086, 1115)))
        self.assertEqual(tuple(sample.coded_size for sample in track.samples), ((16, 8),) * 3)
        self.assertEqual(track.samples[0].data, inventory.items[0].data)

    def test_frozen_native_tile_controls_are_single_items_not_grids(self) -> None:
        from tedd_knowledge_graph.source_contracts import digest
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff

        for payload, expected, item_digest in (
            (
                AVIF_SINGLE_TILE_256,
                "9caab636dbb39718cf4ad467dc474e884f19baad8cc503a68aa1579ee221ca4c",
                "9fa07a93f43637fd240d44b8bb35e88950d1018b50306e7ceda17b9bd8f74ae3",
            ),
            (
                AVIF_FOUR_TILES_256,
                "71f0c440ef037a4ef2fe44fe25bdff09edfa87aa72dc4dfb456c6ee2b9a82ea8",
                "4f4bc50e7b904a0b48446db025447af775a62b025defbb8799723e7fb2cbeaed",
            ),
            (
                AVIF_TWO_TILE_GROUPS_256,
                "82383f54b1bd5e7451f9ce37db610e0afec4050c98e247c53988ec841feeb9eb",
                "c70be8e6ff84471c13c9913168782ac1e220157fb75cfe73455147b7845883de",
            ),
        ):
            with self.subTest(source=expected):
                self.assertEqual(digest(payload), expected)
                inventory = inspect_bmff(payload)
                self.assertEqual(inventory.format, "avif")
                self.assertEqual(len(inventory.items), 1)
                self.assertEqual(inventory.items[0].kind, b"av01")
                self.assertEqual(inventory.items[0].coded_size, (256, 256))
                self.assertEqual(digest(inventory.items[0].data), item_digest)

    @unittest.skipUnless(find_spec("PIL"), "qualified Pillow helper")
    def test_avif_decoder_pixel_core_closes_on_success_failure_and_cancellation(self) -> None:
        from PIL import Image
        from tedd_knowledge_graph import source_image_host

        opener = Image.open
        captured: list[Image.Image] = []
        streams: list[io.BytesIO] = []
        fail_open = False

        def tracked_open(stream: io.BytesIO, *, formats: list[str]) -> Image.Image:
            streams.append(stream)
            if fail_open:
                raise OSError("synthetic opener failure")
            image = opener(stream, formats=formats)
            captured.append(image)
            return image

        for outcome in ("success", "open_error", "output_error", "load_error", "cancellation"):
            captured.clear()
            streams.clear()
            fail_open = outcome == "open_error"
            with self.subTest(outcome=outcome), patch.object(Image, "open", tracked_open):
                if outcome == "success":
                    source_image_host.render_avif_item(AVIF_WHITE, 1)
                elif fail_open:
                    with self.assertRaises(SourceArchiveError):
                        source_image_host.render_avif_item(AVIF_WHITE, 1)
                else:
                    target = (
                        "PIL.AvifImagePlugin.AvifImageFile.load"
                        if outcome == "load_error"
                        else "tedd_knowledge_graph.source_image_host._render_pixels"
                    )
                    with (
                        patch(
                            target,
                            side_effect=KeyboardInterrupt()
                            if outcome == "cancellation"
                            else OSError(),
                        ),
                        self.assertRaises(
                            KeyboardInterrupt if outcome == "cancellation" else SourceArchiveError
                        ),
                    ):
                        source_image_host.render_avif_item(AVIF_WHITE, 1)
                self.assertEqual(len(streams), 1)
                self.assertTrue(streams[0].closed)
                self.assertEqual(len(captured), int(not fail_open))
                # ImageFile.__exit__ closes the stream, not the pixel core.
                # Retaining a diagnostic reference must not keep pixels usable.
                for image in captured:
                    with self.assertRaises(ValueError):
                        image.tobytes()

    @unittest.skipUnless(
        sys.platform in {"win32", "linux"} and find_spec("pillow_heif"), "qualified HEIF helper"
    )
    def test_heif_view_uses_actual_native_planes_and_decodes_remaining_items(self) -> None:
        from PIL import Image
        from tedd_knowledge_graph import source_image_heif
        from tedd_knowledge_graph.source_image_host import render_heif_item

        native_decode = source_image_heif.decode_heif
        expected = {1: (254, 0, 0, 128), 2: (128, 128, 128, 255), 3: (0, 0, 254, 255)}
        with patch.object(source_image_heif, "decode_heif", wraps=native_decode) as decoder:
            for item_id, pixel in expected.items():
                with self.subTest(item=item_id):
                    png = render_heif_item(HEVC_COLLECTION, item_id)
                    with Image.open(io.BytesIO(png), formats=["PNG"]) as rendered:
                        self.assertEqual(rendered.size, (8, 8))
                        self.assertEqual(rendered.info, {})
                        self.assertEqual(rendered.tobytes(), bytes(pixel) * 64)
            self.assertEqual(decoder.call_count, 3)
            for call in decoder.call_args_list:
                self.assertEqual(tuple(item.item_id for item in call.args[1].items), (1, 2, 3))

        presentation = render_heif_item(HEVC_TWO_COLORS, 1)
        reconstruction = render_heif_item(HEVC_TWO_COLORS, 1, reconstruction=True)
        detail = render_heif_item(HEVC_TWO_COLORS, 1, rectangle=(8, 0, 16, 8))
        with (
            Image.open(io.BytesIO(presentation), formats=["PNG"]) as displayed,
            Image.open(io.BytesIO(reconstruction), formats=["PNG"]) as raw,
            Image.open(io.BytesIO(detail), formats=["PNG"]) as cropped,
        ):
            self.assertEqual(displayed.size, (16, 8))
            self.assertEqual(raw.size, (64, 64))
            self.assertEqual(cropped.size, (8, 8))
            with raw.crop((0, 0, 16, 8)) as expected_presentation:
                self.assertEqual(displayed.tobytes(), expected_presentation.tobytes())
            with displayed.crop((8, 0, 16, 8)) as expected_detail:
                self.assertEqual(cropped.tobytes(), expected_detail.tobytes())
        with patch.object(source_image_heif, "decode_heif") as unavailable:
            for item_id in (True, 0, 4):
                with self.subTest(invalid_item=item_id), self.assertRaises(SourceArchiveError):
                    render_heif_item(HEVC_COLLECTION, item_id)
            with self.assertRaises(SourceArchiveError):
                render_heif_item(AVIF_WHITE, 1)
            unavailable.assert_not_called()

    @unittest.skipUnless(find_spec("PIL"), "qualified Pillow helper")
    def test_avif_view_crops_before_orientation_and_keeps_reconstruction_untransformed(
        self,
    ) -> None:
        from PIL import Image
        from tedd_knowledge_graph.source_contracts import digest
        from tedd_knowledge_graph.source_image_bmff import avif_item_bytes, inspect_bmff
        from tedd_knowledge_graph.source_image_host import render_avif_item

        self.assertEqual(
            digest(AVIF_SIX_COLORS),
            "f2a6dbf39e1302d1f6dcb05d49b3b5e924314cb49cf93addd8c2603ebd21fdd9",
        )
        inventory = inspect_bmff(AVIF_SIX_COLORS)
        item = inventory.items[0]
        # Literal pinned decoder pixels: the encoded original is lossy.
        colors = (
            (255, 0, 1, 255),
            (0, 255, 0, 255),
            (0, 0, 253, 255),
            (254, 255, 0, 255),
            (255, 1, 254, 255),
            (0, 255, 252, 255),
        )
        original_pixels = b"".join(bytes(pixel) for pixel in colors) * 4
        center = colors[1:5]
        expected_pixels = {
            1: b"".join(bytes(pixel) for pixel in center) * 2,
            2: b"".join(bytes(pixel) for pixel in reversed(center)) * 2,
            3: b"".join(bytes(pixel) for pixel in reversed(center)) * 2,
            4: b"".join(bytes(pixel) for pixel in center) * 2,
            5: b"".join(bytes(pixel) * 2 for pixel in center),
            6: b"".join(bytes(pixel) * 2 for pixel in center),
            7: b"".join(bytes(pixel) * 2 for pixel in reversed(center)),
            8: b"".join(bytes(pixel) * 2 for pixel in reversed(center)),
        }
        clap = struct.pack("!4IiIiI", 4, 1, 2, 1, 0, 1, 0, 1)
        for axis, orientations in ((None, (1, 8, 3, 6)), (0, (4, 5, 2, 7)), (1, (2, 7, 4, 5))):
            for rotation, orientation in enumerate(orientations):
                with self.subTest(axis=axis, rotation=rotation):
                    properties = item.properties + ((b"clap", clap), (b"irot", bytes([rotation])))
                    if axis is not None:
                        properties += ((b"imir", bytes([axis])),)
                    changed = replace(inventory, items=(replace(item, properties=properties),))
                    source = avif_item_bytes(changed, 1)
                    with Image.open(io.BytesIO(source), formats=["AVIF"]) as native:
                        self.assertEqual(native.getexif().get(274, 1), orientation)
                        self.assertEqual(native.size, (6, 4))
                        with native.convert("RGBA") as rgba:
                            self.assertEqual(rgba.tobytes(), original_pixels)
                    for reconstruction in (False, True):
                        png = render_avif_item(source, 1, reconstruction=reconstruction)
                        with Image.open(io.BytesIO(png), formats=["PNG"]) as viewed:
                            self.assertEqual(viewed.info, {})
                            expected_size = (4, 2) if orientation < 5 else (2, 4)
                            self.assertEqual(
                                viewed.size, (6, 4) if reconstruction else expected_size
                            )
                            self.assertEqual(
                                viewed.tobytes(),
                                original_pixels if reconstruction else expected_pixels[orientation],
                            )
                    png = render_avif_item(source, 1, rectangle=(0, 0, 2, 2))
                    with Image.open(io.BytesIO(png), formats=["PNG"]) as detail:
                        stride = (4 if orientation < 5 else 2) * 4
                        expected = expected_pixels[orientation]
                        self.assertEqual(detail.size, (2, 2))
                        self.assertEqual(
                            detail.tobytes(), expected[:8] + expected[stride : stride + 8]
                        )

    def test_mirror_axes_follow_the_container_spec_after_rotation(self) -> None:
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff

        item = inspect_bmff(AVIF_WHITE).items[0]
        # HEIF/AVIF imir=0 swaps top/bottom; imir=1 swaps left/right.
        # Literal EXIF equivalents are independent of the implementation tables.
        for axis, expected in ((None, (1, 8, 3, 6)), (0, (4, 5, 2, 7)), (1, (2, 7, 4, 5))):
            for rotation, orientation in enumerate(expected):
                with self.subTest(axis=axis, rotation=rotation):
                    properties = item.properties + ((b"irot", bytes([rotation])),)
                    if axis is not None:
                        properties += ((b"imir", bytes([axis])),)
                    self.assertEqual(replace(item, properties=properties).orientation, orientation)

    @unittest.skipUnless(find_spec("PIL"), "qualified Pillow helper")
    def test_avif_grid_decodes_clipped_tiles_and_each_alpha_item(self) -> None:
        from PIL import Image
        from tedd_knowledge_graph.source_contracts import digest
        from tedd_knowledge_graph.source_image_bmff import avif_item_bytes, inspect_bmff

        self.assertEqual(
            digest(AVIF_GRID), "bae56368b348b1d847e2bfb662522599f0c63dfe62fb68826c9e42a300ff405d"
        )
        inventory = inspect_bmff(AVIF_GRID)
        self.assertEqual(inventory.top_ids, (1,))
        self.assertEqual(inventory.coded_pixels, 80 * 80 + 4 * 80 * 64)
        decoded: dict[int, bytes] = {}
        for item in inventory.items:
            with self.subTest(item=item.item_id):
                wrapper = avif_item_bytes(inventory, item.item_id)
                derivative = inspect_bmff(wrapper)
                self.assertEqual(derivative.primary_id, item.item_id)
                selected = next(part for part in derivative.items if part.item_id == item.item_id)
                self.assertEqual(selected.data, item.data)
                expected_ids = {1: (1, 2, 3, 5, 6), 2: (2, 5), 3: (3, 6), 5: (5,), 6: (6,)}
                self.assertEqual(
                    tuple(part.item_id for part in derivative.items), expected_ids[item.item_id]
                )
                with Image.open(io.BytesIO(wrapper), formats=["AVIF"]) as image:
                    self.assertEqual(image.n_frames, 1)
                    self.assertEqual(image.size, item.coded_size)
                    with image.convert("RGBA") as rgba:
                        decoded[item.item_id] = rgba.tobytes()

        # Check the original presentation independently of the item wrapper.
        with Image.open(io.BytesIO(AVIF_GRID), formats=["AVIF"]) as original:
            self.assertEqual(original.size, (80, 80))
            with original.convert("RGBA") as rgba:
                self.assertEqual(decoded[1], rgba.tobytes())
        row = 80 * 4
        self.assertEqual(decoded[1][: 64 * row], decoded[2])
        self.assertEqual(decoded[1][64 * row :], decoded[3][: 16 * row])
        # The clipped tile's remaining 48 rows are still decoded, not discarded
        # as if the grid were the whole retained source. Alpha items are also
        # independently viewable grayscale samples, not just applied silently.
        self.assertEqual(len(decoded[3][16 * row :]), 48 * row)
        self.assertEqual(decoded[3][16 * row :], bytes(48 * row))
        for color_id, alpha_id in ((2, 5), (3, 6)):
            self.assertEqual(decoded[color_id][3::4], decoded[alpha_id][0::4])
            self.assertEqual(decoded[alpha_id][0::4], decoded[alpha_id][1::4])
            self.assertEqual(decoded[alpha_id][0::4], decoded[alpha_id][2::4])
            self.assertEqual(decoded[alpha_id][3::4], bytes([255]) * (80 * 64))

    @unittest.skipUnless(find_spec("PIL"), "qualified Pillow helper")
    def test_avif_item_wrapper_is_only_a_decoder_derivative_of_exact_original(self) -> None:
        from PIL import Image
        from tedd_knowledge_graph.source_image_bmff import avif_item_bytes, inspect_bmff

        original = inspect_bmff(AVIF_WHITE)
        wrapper = avif_item_bytes(original, 1)
        derived = inspect_bmff(wrapper)
        self.assertNotEqual(wrapper, AVIF_WHITE)
        self.assertEqual(derived.primary_id, original.primary_id)
        self.assertEqual(derived.items[0].data, original.items[0].data)
        self.assertEqual(derived.items[0].properties, original.items[0].properties)
        self.assertEqual(derived.metadata_categories, ())
        with (
            Image.open(io.BytesIO(AVIF_WHITE), formats=["AVIF"]) as expected,
            Image.open(io.BytesIO(wrapper), formats=["AVIF"]) as actual,
        ):
            self.assertEqual(actual.n_frames, 1)
            self.assertEqual(actual.size, expected.size)
            self.assertEqual(actual.tobytes(), expected.tobytes())
        with self.assertRaises(SourceArchiveError):
            avif_item_bytes(original, 99)
        with self.assertRaises(SourceArchiveError):
            avif_item_bytes(inspect_bmff(HEVC_COLLECTION), 1)

    @unittest.skipUnless(
        sys.platform in {"win32", "linux"} and find_spec("pillow_heif"), "qualified HEIF helper"
    )
    def test_reconstruction_view_exposes_real_samples_hidden_by_clean_aperture(self) -> None:
        from tedd_knowledge_graph.source_contracts import digest
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff
        from tedd_knowledge_graph.source_image_heif import decode_heif

        self.assertEqual(
            digest(HEVC_TWO_COLORS),
            "6415a75d70dfaac246ca54d0e2a0b9caf96b36d2599dfb21240423aa7d480d2d",
        )
        crop = struct.pack("!4IiIiI", 8, 1, 8, 1, -56, 2, -56, 2)
        original = inspect_bmff(HEVC_TWO_COLORS)
        clap = dict(original.items[0].properties)[b"clap"]
        altered = HEVC_TWO_COLORS.replace(clap, crop)
        self.assertEqual(
            digest(altered), "e7272c57441cc8dc0ae701832c2613cead23d4415e8c5f5485dab87f9e2c2b4a"
        )
        inventory = inspect_bmff(altered)
        displays: list[bytes] = []

        def presentation(_item_id: int, size: tuple[int, int], rgba: bytes) -> None:
            self.assertEqual(size, (8, 8))
            displays.append(rgba)

        def reconstructed(_item_id: int, size: tuple[int, int], rgba: bytes) -> None:
            self.assertEqual(size, (64, 64))
            self.assertEqual(len(rgba), 64 * 64 * 4)
            red = rgba[(4 * 64 + 4) * 4 : (4 * 64 + 4) * 4 + 4]
            blue = rgba[(4 * 64 + 12) * 4 : (4 * 64 + 12) * 4 + 4]
            self.assertGreater(red[0], 240)
            self.assertLess(red[2], 16)
            self.assertGreater(blue[2], 240)
            self.assertLess(blue[0], 16)
            self.assertEqual(
                b"".join(rgba[row * 256 : row * 256 + 32] for row in range(8)), displays[0]
            )

        decode_heif(altered, inventory, presentation)
        decode_heif(altered, inventory, reconstructed, reconstruction=True)

    def test_hevc_item_cannot_hide_an_additional_coded_picture(self) -> None:
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff

        valid = inspect_bmff(HEVC_COLLECTION)
        location = bytes.fromhex("000300000000031100010000000000000033")
        extra_picture = HEVC_COLLECTION.replace(location, location[:-1] + b"\x67")
        extra_picture = extra_picture.replace(b"\0\0\0\x8cmdat", b"\0\0\0\xc0mdat")
        extra_picture += valid.items[0].data
        self.assertEqual(len(extra_picture), 888)
        with self.assertRaises(SourceArchiveError):
            inspect_bmff(extra_picture)

    def test_hevc_container_admission_does_not_claim_slice_byte_consumption(self) -> None:
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff

        baseline = inspect_bmff(HEVC_COLLECTION)
        altered = hevc_ignored_tail()
        self.assertEqual(len(altered), 852)
        # The approved boundary preserves the original, including ignored coded
        # bytes. This pure parser checks structure; it is not a decoder receipt.
        inventory = inspect_bmff(altered)
        self.assertEqual(inventory.top_ids, baseline.top_ids)
        self.assertEqual(inventory.coded_pixels, baseline.coded_pixels)
        self.assertEqual(inventory.metadata_categories, baseline.metadata_categories)
        self.assertTrue(any(item.data.endswith(b"synthetic-hidden") for item in inventory.items))

    @unittest.skipUnless(
        sys.platform in {"win32", "linux"} and find_spec("pillow_heif"), "qualified HEIF helper"
    )
    def test_heif_native_allocations_close_on_decode_and_consumer_failure(self) -> None:
        from tedd_knowledge_graph import source_image_heif as native
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff

        original_library = native._library
        inventory = inspect_bmff(HEVC_COLLECTION)
        for fail_after in (
            "heif_context_read_from_memory_without_copy",
            "heif_context_get_image_handle",
            "heif_decode_image",
            "consume",
        ):
            calls: list[str] = []

            class TrackedFunction:
                def __init__(
                    self, function: native._Function, name: str, recorded: list[str], failure: str
                ) -> None:
                    self.function, self.name = function, name
                    self.recorded, self.failure = recorded, failure

                @property
                def restype(self) -> object:
                    return self.function.restype

                @restype.setter
                def restype(self, value: object) -> None:
                    self.function.restype = value

                @property
                def argtypes(self) -> list[object]:
                    return self.function.argtypes

                @argtypes.setter
                def argtypes(self, value: list[object]) -> None:
                    self.function.argtypes = value

                def __call__(self, *args: object) -> object:
                    self.recorded.append(self.name)
                    result = self.function(*args)
                    if self.name == self.failure:
                        raise SourceArchiveError("source_unavailable")
                    return result

            @contextmanager
            def tracked_library(
                recorded: list[str] = calls, failure: str = fail_after
            ) -> Iterator[object]:
                with original_library() as library:

                    class TrackedLibrary:
                        def __getattr__(self, name: str) -> TrackedFunction:
                            return TrackedFunction(
                                cast(native._Function, getattr(library, name)),
                                name,
                                recorded,
                                failure,
                            )

                    yield TrackedLibrary()

            def consume(_item_id: int, _size: tuple[int, int], _rgba: bytes) -> None:
                raise SourceArchiveError("source_unavailable")

            with (
                self.subTest(failure=fail_after),
                patch.object(native, "_library", tracked_library),
                self.assertRaises(SourceArchiveError),
            ):
                native.decode_heif(HEVC_COLLECTION, inventory, consume)
            self.assertEqual(calls.count("heif_context_free"), 1)
            self.assertEqual(
                calls.count("heif_decoding_options_free"),
                int(fail_after != "heif_context_read_from_memory_without_copy"),
            )
            self.assertEqual(
                calls.count("heif_image_handle_release"),
                int(fail_after != "heif_context_read_from_memory_without_copy"),
            )
            self.assertEqual(
                calls.count("heif_image_release"),
                int(fail_after in ("heif_decode_image", "consume")),
            )
            self.assertNotIn("heif_init", calls)

    def test_independent_avif_envelope_has_exact_source_identity(self) -> None:
        from tedd_knowledge_graph.source_contracts import digest
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff

        self.assertEqual(len(AVIF_WHITE), 305)
        self.assertEqual(
            digest(AVIF_WHITE), "ea4e43d1f07e4c00de16c13afa32376111bb306e51f08212cf4c1b6064df3667"
        )
        inventory = inspect_bmff(AVIF_WHITE)
        self.assertEqual(inventory.format, "avif")
        self.assertEqual(inventory.top_ids, (1,))
        self.assertEqual(inventory.coded_pixels, 1)
        self.assertEqual(inventory.items[0].display_size, (1, 1))
        self.assertEqual(inventory.metadata_categories, ("description",))

    @unittest.skipUnless(find_spec("PIL"), "qualified Pillow helper")
    def test_independent_avif_decoder_control_matches_literal_white_pixel(self) -> None:
        from PIL import Image

        with Image.open(io.BytesIO(AVIF_WHITE), formats=["AVIF"]) as image:
            self.assertEqual(image.n_frames, 1)
            image.load()
            with image.convert("RGBA") as pixels:
                self.assertEqual(pixels.size, (1, 1))
                # Upstream calls this a default-quality (lossy) white pixel;
                # preserve a small explicit display-quantization tolerance.
                red, green, blue, alpha = pixels.tobytes()
                self.assertTrue(all(abs(value - 255) <= 2 for value in (red, green, blue)))
                self.assertEqual(alpha, 255)

    def test_hevc_metadata_nals_and_av1_metadata_obus_are_refused(self) -> None:
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff

        source = inspect_bmff(HEVC_COLLECTION)
        compressed = source.items[-1].data
        hidden_sei = compressed[:4] + bytes((39 << 1, 1)) + compressed[6:]
        invalid_header = compressed[:4] + bytes((compressed[4] | 0x80, 1)) + compressed[6:]
        for altered in (hidden_sei, invalid_header):
            with self.subTest(nal=altered[4]), self.assertRaises(SourceArchiveError):
                inspect_bmff(HEVC_COLLECTION.replace(compressed, altered))
        raw = avif_structure()
        for altered in (b"\x2a\1\0\x32\1\0", b"\x0a\1\0\x2a\1\0", b"\x0a\xff\0\x32\1\0"):
            with self.subTest(obus=altered.hex()), self.assertRaises(SourceArchiveError):
                inspect_bmff(raw.replace(b"\x0a\1\0\x32\1\0", altered))

    @unittest.skipUnless(
        sys.platform in {"win32", "linux"} and find_spec("pillow_heif"), "qualified HEIF helper"
    )
    def test_hevc_literal_decodes_every_item_without_encoder_or_plugin_initialization(self) -> None:
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff
        from tedd_knowledge_graph.source_image_heif import decode_heif

        inventory = inspect_bmff(HEVC_COLLECTION)
        received: list[int] = []

        def pixels(item_id: int, size: tuple[int, int], rgba: bytes) -> None:
            self.assertEqual(size, (8, 8))
            color = {1: (254, 0, 0, 128), 2: (128, 128, 128, 255), 3: (0, 0, 254, 255)}[item_id]
            self.assertEqual(rgba, bytes(color) * 64)
            received.append(item_id)

        # Supplying a fake Python module makes accidental use of its encoder or
        # Pillow registration fail. The public C API loads only qualified libraries.
        with patch.dict(sys.modules, {"pillow_heif": None}):
            decode_heif(HEVC_COLLECTION, inventory, pixels)
        self.assertEqual(received, [1, 2, 3])

    def test_frozen_hevc_inventory_separates_coded_aperture_and_auxiliary(self) -> None:
        from tedd_knowledge_graph.source_contracts import digest
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff

        self.assertEqual(len(HEVC_COLLECTION), 836)
        self.assertEqual(
            digest(HEVC_COLLECTION),
            "ad02df9c9250f8a007087487e58b2046b54c0eab9a33c55b6af717c2af010707",
        )
        inventory = inspect_bmff(HEVC_COLLECTION)
        self.assertEqual(inventory.top_ids, (1, 3))
        self.assertEqual(inventory.coded_pixels, 3 * 64 * 64)
        self.assertEqual(inventory.metadata_categories, ())
        for item in inventory.items:
            self.assertEqual(item.coded_size, (64, 64))
            self.assertEqual(item.display_size, (8, 8))
            self.assertEqual(item.aperture, (0, 0, 8, 8))
        self.assertEqual(inventory.auxiliary_ids(1), (2,))
        self.assertEqual(inventory.auxiliary_ids(3), ())

    def test_bmff_dangling_auxiliary_cycles_and_invalid_apertures_are_refused(self) -> None:
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff, validate_relationships

        inventory = inspect_bmff(HEVC_COLLECTION)
        red, alpha, blue = inventory.items
        for items in (
            (red, replace(alpha, references=()), blue),
            (replace(red, references=((b"auxl", (2,)),)), alpha, blue),
            (red, replace(alpha, references=((b"auxl", (99,)),)), blue),
            (replace(red, hidden=True), alpha, blue),
        ):
            with (
                self.subTest(ids=[item.item_id for item in items]),
                self.assertRaises(SourceArchiveError),
            ):
                validate_relationships(items, inventory.primary_id)
        clap = next(data for kind, data in red.properties if kind == b"clap")
        for invalid in (
            struct.pack("!8I", 65, 1, 8, 1, 0, 1, 0, 1),
            struct.pack("!8I", 8, 0, 8, 1, 0, 1, 0, 1),
            struct.pack("!8I", 8, 1, 8, 1, 0x7FFFFFFF, 1, 0, 1),
        ):
            with self.subTest(aperture=invalid.hex()), self.assertRaises(SourceArchiveError):
                inspect_bmff(HEVC_COLLECTION.replace(clap, invalid))

    def test_complete_item_envelope_remains_separate_from_pixel_decoding(self) -> None:
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff

        inventory = inspect_bmff(avif_structure())
        self.assertEqual(inventory.format, "avif")
        self.assertEqual(inventory.primary_id, 1)
        self.assertEqual(inventory.metadata_categories, ())
        self.assertEqual([(item.item_id, item.kind) for item in inventory.items], [(1, b"av01")])
        self.assertEqual(inventory.items[0].data, b"\x0a\1\0\x32\1\0")

    def test_unknown_properties_unreferenced_data_and_partial_boxes_are_refused(self) -> None:
        from tedd_knowledge_graph.source_image_bmff import inspect_bmff

        raw = avif_structure()
        for malformed in (
            avif_structure(property_bytes=box(b"uuid", b"private fields")),
            avif_structure(unused=b"private suffix"),
            raw + b"suffix",
            raw[:-1],
        ):
            with self.subTest(size=len(malformed)), self.assertRaises(SourceArchiveError):
                inspect_bmff(malformed)
