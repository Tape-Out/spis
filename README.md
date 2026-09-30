# spis

An SPI slave that masters the on-chip bus. An off-chip host reads and writes any address over four pins, so a chip without a core can still be configured and inspected.

![maturity](https://img.shields.io/badge/maturity-simulated-yellow) ![license](https://img.shields.io/badge/license-MIT%20OR%20Apache--2.0%20OR%20MulanPSL--2.0-blue)

Part of the [Tape-Out](https://github.com/Tape-Out) IP library: Bluespec IP over the bus-neutral contracts in [`hwcore`](https://github.com/Tape-Out/hwcore), assembled by [`xirang`](https://github.com/Tape-Out/xirang).

## Protocol

SPI mode 0 (CPOL 0, CPHA 0), most significant bit first, `cs_n` active low. Words go over the wire big-endian; the low two address bits are ignored.

| Command | Host sends | Chip answers |
|:--:|:--:|:--:|
| `0x02` write | `a3 a2 a1 a0`, then `d3 d2 d1 d0` per word | — |
| `0x03` read | `a3 a2 a1 a0`, one dummy byte | `q3 q2 q1 q0` per word |
| `0x05` status | one byte | bit 0: the bus answered an error; bit 1: the host outran the bus. Cleared by the read |
| `0x9F` identify | four bytes | `53 50 49 53` ("SPIS") |

Each full word of a write becomes one bus write, and the address steps by four. A read fetches the next word while the current one shifts out, so a burst keeps going as long as the bus answers within 32 SCK periods. Raising `cs_n` ends the transaction at any byte.

The pins are oversampled by the system clock through a two-stage synchroniser: SCK must stay at or below one eighth of the system clock, and the host waits at least four system clocks after `cs_n` falls before the first SCK edge. `miso_oe` is high only while `cs_n` is low, so several chips can share MISO.

## Interface

- `mgr`: a `RegManager#(32, 32)`. In an assembly it joins the on-chip bus next to the other managers; with `bus: none` it is the only one.
- `pins`: `sck`, `cs_n` and `mosi` in, `miso` and `miso_oe` out.

## Testing

`htest/mkspistb.py` generates a testbench that bit-bangs the host side against a bus model with plain memory, an erroring range and a slow range. It checks the identify bytes, a write read back, a four-word burst, the error flag and its clearing, a transaction cut off mid-address, and the too-fast flag on the slow range. Eight single-line mutations of the design each fail it.

```console
$ ran test spis
```

## License

任选其一：

- [MIT](LICENSE-MIT)
- [Apache 2.0](LICENSE-APACHE)
- [木兰宽松许可证 第2版](LICENSE-MULAN)

`SPDX-License-Identifier: MIT OR Apache-2.0 OR MulanPSL-2.0`

除非另行说明，你提交的贡献按上述三者同时授权，不附加其他条件。
