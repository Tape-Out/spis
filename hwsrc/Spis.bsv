package Spis;

import RegIf::*;

// SPI 从口：片外主机经它当片上总线的发起方。无核的芯片（交换机、协处理器）靠它管理。
//
// 模式 0（CPOL=0、CPHA=0），高位先出，cs_n 低有效。引脚按系统时钟过采样，
// SCK 不能超过系统时钟的八分之一；cs_n 落下后至少隔四个系统时钟再给第一个 SCK。
//
//   写    0x02  a3 a2 a1 a0  d3 d2 d1 d0 [d3 …]      每满四字节写一笔，地址加四
//   读    0x03  a3 a2 a1 a0  xx  q3 q2 q1 q0 [q3 …]  xx 是等总线的那一字节
//   状态  0x05  s                                    s[0] 总线回过错，s[1] 主机快过总线；读完清零
//   标识  0x9F  53 50 49 53                          "SPIS"，之后循环
//
// 字按大端上线，先出最高字节；地址的低两位不看。

typedef struct {
  Bit#(0) none;
} SpisCfg;

interface SpisPins;
  (* always_ready, always_enabled, prefix = "" *)
  method Action pins_in((* port = "sck" *) Bit#(1) sck,
                        (* port = "cs_n" *) Bit#(1) csn,
                        (* port = "mosi" *) Bit#(1) mosi);
  (* always_ready, result = "miso" *)    method Bit#(1) miso;
  (* always_ready, result = "miso_oe" *) method Bit#(1) miso_oe;
endinterface

interface SpisIfc#(numeric type aw, numeric type dw);
  interface RegManager#(32, 32) mgr;
  interface SpisPins pins;
endinterface

typedef enum { Cmd, Addr, Dummy, Data, Status, Ident, Skip } Ph deriving (Bits, Eq, FShow);

function Bit#(8) ident(Bit#(2) k);
  case (k)
    0: return 8'h53;
    1: return 8'h50;
    2: return 8'h49;
    default: return 8'h53;
  endcase
endfunction

module mkSpis#(SpisCfg cfg)(SpisIfc#(aw, dw));
  // 两级同步；取沿用的是第二级与它的上一拍，三根线经同样的延迟，相互对齐
  Reg#(Bit#(3)) s1   <- mkReg(3'b010);
  Reg#(Bit#(3)) s2   <- mkReg(3'b010);
  Reg#(Bit#(1)) sckp <- mkReg(0);

  Reg#(Ph)       ph    <- mkReg(Cmd);
  Reg#(Bit#(3))  bitc  <- mkReg(0);
  Reg#(Bit#(7))  rx    <- mkReg(0);
  Reg#(Bit#(2))  k     <- mkReg(0);
  Reg#(Bool)     rd    <- mkReg(False);
  Reg#(Bit#(32)) addr  <- mkReg(0);
  Reg#(Bit#(24)) wacc  <- mkReg(0);
  Reg#(Bit#(32)) cur   <- mkReg(0);
  Reg#(Bit#(32)) rbuf  <- mkReg(0);
  Reg#(Bit#(8))  txsh  <- mkReg(0);
  Reg#(Bool)     busErr <- mkReg(False);
  Reg#(Bool)     fast  <- mkReg(False);

  Reg#(Bool)            reqV <- mkReg(False);
  Reg#(RegReq#(32, 32)) reqR <- mkReg(unpack(0));

  Wire#(Bool)        rspV <- mkBypassWire;
  Wire#(RegRsp#(32)) rspX <- mkBypassWire;
  Wire#(Bool)        rdy  <- mkBypassWire;
  // 引脚方法只写线、不写寄存器：它若直接写同步器，调用方读 MISO 与驱引脚的两条规则
  // 就会和 step 绕成调度环，bsc 挡掉的恰是 step
  Wire#(Bit#(3))     pin  <- mkBypassWire;

  Bit#(1) sck  = s2[2];
  Bit#(1) csn  = s2[1];
  Bit#(1) mosi = s2[0];

  // MISO 在检到上升沿时就换下一位：主机在上升沿已经采过，下一位有整整一个周期站稳。
  // 等下降沿再换的话，同步的三拍延迟会把它挤到下一个上升沿跟前
  rule step;
    Bool pend = reqV;
    Bit#(32) rb = rbuf;
    Bool be = busErr;
    Bool fst = fast;
    if (rspV) begin
      pend = False;
      if (rspX.err) be = True;
      if (!reqR.write) rb = rspX.rdata;
    end

    Ph p = ph;
    Bit#(3) bc = bitc;
    Bit#(7) r = rx;
    Bit#(2) kk = k;
    Bool isRd = rd;
    Bit#(32) a = addr;
    Bit#(24) wa = wacc;
    Bit#(32) cw = cur;
    Bit#(8) tx = txsh;
    Maybe#(RegReq#(32, 32)) go = tagged Invalid;

    if (csn == 1) begin
      p = Cmd;
      bc = 0;
      kk = 0;
      tx = 0;
    end else if (sck == 1 && sckp == 0) begin
      Bit#(8) b = {r, mosi};
      tx = tx << 1;
      if (bc != 7) begin
        r = b[6:0];
        bc = bc + 1;
      end else begin
        bc = 0;
        case (p)
          Cmd: begin
            kk = 0;
            case (b)
              8'h02: begin p = Addr; isRd = False; end
              8'h03: begin p = Addr; isRd = True; end
              8'h05: begin
                p = Status;
                tx = {6'b0, pack(fst), pack(be)};
                be = False;
                fst = False;
              end
              8'h9F: begin p = Ident; tx = ident(0); kk = 1; end
              default: p = Skip;
            endcase
          end
          Addr: begin
            a = {a[23:0], b};
            if (kk != 3) kk = kk + 1;
            else begin
              kk = 0;
              if (isRd) begin
                p = Dummy;
                go = tagged Valid RegReq { addr: {a[31:2], 2'b00}, write: False,
                                           wdata: 0, wstrb: 0 };
                a = a + 4;
              end
              else p = Data;
            end
          end
          Dummy, Data: begin
            if (!isRd) begin
              if (kk != 3) begin
                wa = {wa[15:0], b};
                kk = kk + 1;
              end else begin
                go = tagged Valid RegReq { addr: {a[31:2], 2'b00}, write: True,
                                           wdata: {wa, b}, wstrb: 4'hF };
                a = a + 4;
                kk = 0;
              end
            end else if (p == Dummy || kk == 3) begin
              // 换下一个字，并立刻去取再下一个：主机出完这一字的 32 位之前它得回来。
              // 这一字没回来时上一笔必然还在途，下面发请求那里会记成主机太快
              cw = rb;
              tx = rb[31:24];
              go = tagged Valid RegReq { addr: {a[31:2], 2'b00}, write: False,
                                         wdata: 0, wstrb: 0 };
              a = a + 4;
              kk = 0;
              p = Data;
            end else begin
              tx = (kk == 0) ? cw[23:16] : ((kk == 1) ? cw[15:8] : cw[7:0]);
              kk = kk + 1;
            end
          end
          Ident: begin
            tx = ident(kk);
            kk = kk + 1;
          end
          default: begin
            p = Skip;
            tx = 0;
          end
        endcase
      end
    end

    if (go matches tagged Valid .q) begin
      if (pend) fst = True;
      else begin
        pend = True;
        reqR <= q;
      end
    end

    s1 <= pin;
    s2 <= s1;
    sckp <= sck;
    ph <= p;
    bitc <= bc;
    rx <= r;
    k <= kk;
    rd <= isRd;
    addr <= a;
    wacc <= wa;
    cur <= cw;
    txsh <= tx;
    rbuf <= rb;
    busErr <= be;
    fast <= fst;
    reqV <= pend;
  endrule

  interface RegManager mgr;
    method Bool valid = reqV;
    method RegReq#(32, 32) req = reqR;
    method Action ready(Bool x);
      rdy <= x;
    endmethod
    method Action resp(Bool v, RegRsp#(32) x);
      rspV <= v;
      rspX <= x;
    endmethod
  endinterface

  interface SpisPins pins;
    method Action pins_in(Bit#(1) sck_, Bit#(1) csn_, Bit#(1) mosi_);
      pin <= {sck_, csn_, mosi_};
    endmethod
    method Bit#(1) miso = txsh[7];
    method Bit#(1) miso_oe = ~csn;
  endinterface
endmodule

endpackage
