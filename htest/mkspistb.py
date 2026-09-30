"""spis 的行为测试台：测试台当 SPI 主机逐位打拍，后面挂一个总线模型。

总线模型分三段：0x000–0x3FF 是 256 字存储，0x2000 起回错，0x3000 起要等 400 拍。
逐字节核对 MISO，最后再直接查存储：

- 标识读得出 "SPIS"；
- 写两字再读回，连读四字拿到存储原有的图样；
- 读到存储最后一个字、连读正好收在最后一个字，状态字都是 0：说几个字就读几个，
  不会为了赶时间多读下一个（下一个地址回错，多读一次就会置位）；
- 写到回错的那一段，状态字 bit0 置位，读一次就清；
- 地址只给了一半就拉高 cs_n，下一笔照常；
- 读慢的那一段，数据来不及，状态字 bit1 置位——主机快过总线时它必须说出来。

SCK 取系统时钟的八分之一，正好是 README 写的上限。
"""
import json
import pathlib
import sys

out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
out.mkdir(parents=True, exist_ok=True)
cfg = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}

HALF = 4


def pat(i: int) -> int:
    i &= 0xFF
    return (0xA5 << 24) | (i << 16) | (0x5A << 8) | (~i & 0xFF)


def be(w: int) -> list[int]:
    return [(w >> s) & 0xFF for s in (24, 16, 8, 0)]


def addr(a: int) -> list[int]:
    return be(a)


seq = []   # (发的字节, 期望收到的字节或 None, 本笔最后一字节)


def tx(send: list, want: list):
    assert len(send) == len(want)
    for i, (s, w) in enumerate(zip(send, want)):
        seq.append((s, w, i == len(send) - 1))


def read(a: int, words: list):
    """一个字用 0x03，多个字用 0x0B 并说明个数。"""
    head = [0x03, *addr(a), 0] if len(words) == 1 else [0x0B, *addr(a), len(words) - 1, 0]
    exp = [None] * len(head)
    for w in words:
        exp += [None] * 4 if w is None else be(w)
    tx(head + [0] * 4 * len(words), exp)


W0, W1 = 0x11223344, 0x55667788
X = [0xCAFEF00D, 0x0BADBEEF, 0x13579BDF]

tx([0x9F, 0, 0, 0, 0, 0], [None, 0x53, 0x50, 0x49, 0x53, 0x53])
tx([0x02, *addr(0x10), *be(W0), *be(W1)], [None] * 13)
read(0x10, [W0, W1])
read(0x40, [pat(0x10), pat(0x11), pat(0x12), pat(0x13)])
tx([0x05, 0], [None, 0x00])
# 存储的最后一个字后面紧挨着回错的地址：只读说好的那几个字，状态字就还是 0。
# 读完多给几个字节也只出 0，不上总线
read(0x3FC, [pat(0xFF)])
tx([0x05, 0], [None, 0x00])
read(0x3F0, [pat(0xFC), pat(0xFD), pat(0xFE), pat(0xFF)])
tx([0x05, 0], [None, 0x00])
tx([0x03, *addr(0x3FC), 0] + [0] * 12, [None] * 6 + be(pat(0xFF)) + [0] * 8)
tx([0x05, 0], [None, 0x00])
tx([0x02, *addr(0x2000), *be(0xDEADBEEF)], [None] * 9)
tx([0x05, 0], [None, 0x01])
tx([0x05, 0], [None, 0x00])
tx([0x02, 0x00, 0x00], [None] * 3)
read(0x14, [W1])
read(0x3000, [None])
tx([0x05, 0], [None, 0x02])
tx([0x02, *addr(0x80), *(b for w in X for b in be(w))], [None] * 17)
read(0x80, X)
tx([0x05, 0], [None, 0x00])

N = len(seq)
rows = "\n".join(f"    {i}: return {{1'b{int(last)}, 1'b{int(w is not None)}, 8'h{w or 0:02X}, 8'h{s:02X}}};"
                 for i, (s, w, last) in enumerate(seq))
# 存储只有五个读口，总线模型占着一个，逐拍查一处
CHECKS = [(4, W0), (5, W1), (0x20, X[0]), (0x21, X[1]), (0x22, X[2]), (0x23, pat(0x23))]
checks = "\n".join(f"    {j}: return {{8'h{i:02X}, 32'h{v:08X}}};" for j, (i, v) in enumerate(CHECKS))

(out / "SpisTb.bsv").write_text(f'''package SpisTb;

import RegFile::*;
import RegIf::*;
import Spis::*;

// 由 htest/mkspistb.py 生成，勿手改。

typedef enum {{ Init, Gap, Lead, Low, High, Tail, Done }} St deriving (Bits, Eq);

// {{最后一字节, 要核对, 期望, 要发的}}
function Bit#(18) step(Bit#(12) i);
  case (i)
{rows}
    default: return 0;
  endcase
endfunction

function Bit#(32) pat(Bit#(8) i) = {{8'hA5, i, 8'h5A, ~i}};

// {{字地址, 期望}}
function Bit#(40) check(Bit#(4) j);
  case (j)
{checks}
    default: return 0;
  endcase
endfunction

(* synthesize *)
module mkSpisTb(Empty);
  SpisIfc#(12, 32) dut <- mkSpis(SpisCfg {{ }});
  RegFile#(Bit#(8), Bit#(32)) mem <- mkRegFileFull;

  Reg#(St)       st   <- mkReg(Init);
  Reg#(Bit#(9))  ini  <- mkReg(0);
  Reg#(Bit#(12)) idx  <- mkReg(0);
  Reg#(Bit#(3))  bitn <- mkReg(7);
  Reg#(Bit#(8))  div  <- mkReg(0);
  Reg#(Bit#(8))  got  <- mkReg(0);
  Reg#(Bit#(1))  sck  <- mkReg(0);
  Reg#(Bit#(1))  csn  <- mkReg(1);
  Reg#(Bit#(1))  mosi <- mkReg(0);
  Reg#(Bool)     bad  <- mkReg(False);
  Reg#(Bit#(32)) cyc  <- mkReg(0);
  Reg#(Bit#(4))  ck   <- mkReg(0);

  Reg#(Bool)            busy <- mkReg(False);
  Reg#(Bit#(10))        lat  <- mkReg(0);
  Reg#(RegReq#(32, 32)) q    <- mkReg(unpack(0));
  Reg#(Bit#(32))        nreq <- mkReg(0);

  let cur = step(idx);
  Bit#(8) sendB = cur[7:0];

  rule drive;
    dut.pins.pins_in(sck, csn, mosi);
  endrule

  rule tick;
    cyc <= cyc + 1;
    if (cyc > 400000) begin
      $display("FAIL TIMEOUT at byte %0d", idx);
      $finish(1);
    end
  endrule

  // 总线模型：收下一笔，数够拍数再答，答的那一拍撤掉 busy
  rule bus (st != Init);
    Bool v = False;
    RegRsp#(32) x = RegRsp {{ rdata: 0, err: False }};
    if (!busy && dut.mgr.valid) begin
      let r = dut.mgr.req;
      q <= r;
      busy <= True;
      nreq <= nreq + 1;
      lat <= (r.addr >= 32'h3000) ? 400 : 3;
    end else if (busy && lat != 0) begin
      lat <= lat - 1;
    end else if (busy) begin
      busy <= False;
      v = True;
      Bit#(8) w = q.addr[9:2];
      if (q.addr >= 32'h3000)
        x = RegRsp {{ rdata: pat(w), err: False }};
      else if (q.addr >= 32'h2000)
        x = RegRsp {{ rdata: 0, err: True }};
      else if (q.addr < 32'h400) begin
        if (q.write) mem.upd(w, applyStrb(mem.sub(w), q.wdata, q.wstrb));
        x = RegRsp {{ rdata: mem.sub(w), err: False }};
      end else
        x = RegRsp {{ rdata: 0, err: True }};
    end
    dut.mgr.ready(!busy);
    dut.mgr.resp(v, x);
  endrule

  rule init (st == Init);
    mem.upd(truncate(ini), pat(truncate(ini)));
    ini <= ini + 1;
    if (ini == 255) st <= Gap;
  endrule

  rule master (st != Init && st != Done);
    case (st)
      Gap: begin
        if (div > 4 && dut.pins.miso_oe != 0) begin
          $display("FAIL miso_oe high while cs_n is high");
          bad <= True;
        end
        if (div == 40) begin
          div <= 0;
          csn <= 0;
          st <= Lead;
        end else div <= div + 1;
      end
      Lead: begin
        if (div == 8) begin
          div <= 0;
          bitn <= 7;
          mosi <= sendB[7];
          st <= Low;
        end else div <= div + 1;
      end
      Low: begin
        if (div == {HALF - 1}) begin
          div <= 0;
          sck <= 1;
          got <= {{got[6:0], dut.pins.miso}};
          st <= High;
        end else div <= div + 1;
      end
      High: begin
        if (div == {HALF - 1}) begin
          div <= 0;
          sck <= 0;
          if (bitn == 0) begin
            if (cur[16] == 1 && got != cur[15:8]) begin
              $display("FAIL byte %0d: got %h, want %h", idx, got, cur[15:8]);
              bad <= True;
            end
            if (cur[17] == 1) st <= Tail;
            else begin
              let nx = step(idx + 1);
              idx <= idx + 1;
              bitn <= 7;
              mosi <= nx[7];
              st <= Low;
            end
          end else begin
            bitn <= bitn - 1;
            mosi <= sendB[bitn - 1];
            st <= Low;
          end
        end else div <= div + 1;
      end
      Tail: begin
        if (div == 8) begin
          div <= 0;
          csn <= 1;
          if (idx + 1 == {N}) st <= Done;
          else begin
            idx <= idx + 1;
            st <= Gap;
          end
        end else div <= div + 1;
      end
    endcase
  endrule

  rule verify (st == Done && !busy && !dut.mgr.valid && ck < {len(CHECKS)});
    let c = check(ck);
    if (mem.sub(c[39:32]) != c[31:0]) begin
      $display("FAIL mem[%0d] = %h, want %h", c[39:32], mem.sub(c[39:32]), c[31:0]);
      bad <= True;
    end
    ck <= ck + 1;
  endrule

  rule finish (st == Done && ck == {len(CHECKS)});
    if (bad) begin
      $display("FAIL after %0d bus requests", nreq);
      $finish(1);
    end else begin
      $display("PASS {N} bytes, %0d bus requests, %0d cycles", nreq, cyc);
      $finish(0);
    end
  endrule
endmodule

endpackage
''', encoding="utf-8")
