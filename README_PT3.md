# MicroPython Raw Flash Exploration — Part 3
### Talking Raw SPI to an External Winbond Chip

---

> **Disclaimer:** This is not official documentation. It was developed through curiosity, hands-on breadboard wiring, and AI-assisted learning. Read Parts 1 and 2 first. Assume innocent mistakes.

---

## What You'll Learn

- Why SPI is a synchronous protocol and what that actually means for timing
- How Chip Select (CS) frames a transaction, and why the rising edge is what commits it
- What "active low" means and why it shows up everywhere in embedded hardware
- Why Write Enable can't be folded into the same CS transaction as the command it unlocks
- Why reads are unbounded but writes are capped at a 256-byte page
- How to poll the BUSY bit instead of blindly waiting
- Why 24-bit addressing is the right size for this chip, and how to split an address into 3 bytes on the wire
- Why NOR flash has no concept of seek time, unlike NAND, SSDs, or hard drives
- Why both the Pico's internal flash and your external Winbond chip use NOR flash as bulk storage, even though that's not the "textbook" use case

---

## Prerequisites

- Completed Parts 1 and 2
- A Raspberry Pi Pico (non-W) with MicroPython
- A Winbond W25Q128 (or similar SPI NOR flash) wired to the Pico on a breadboard
- A serial terminal
- The Winbond datasheet for your specific part number

---

## Part 8 — SPI as a Synchronous Wire Protocol

### Background: Synchronous Means Clock-Driven

Unlike UART, SPI has no baud-rate negotiation and no start/stop bits. Every bit that moves across the MOSI/MISO lines moves in lockstep with the clock line (SCK) that the Pico generates. Nothing happens without the Pico's clock edge — the Winbond chip has no clock of its own to fall back on. This is what "synchronous" means in practice: the master (Pico) is the metronome, and the slave (Winbond) just shifts bits in and out on each tick.

**Why this matters:** there's no ambiguity about timing on the wire — but it also means if your clock stops mid-transaction, the chip just sits there holding its last bit. There's no timeout built into the electrical layer; any timeout has to be built into your code.

---

### Task 8.1 — Understand the Hardware FIFO

The Pico's SPI peripheral has a small hardware FIFO (first-in-first-out buffer) sitting between your MicroPython code and the actual SPI shift register. When you call `spi.write()` or `spi.readinto()`, MicroPython is queuing bytes into this FIFO, and the peripheral drains it onto the wire one byte per clock burst.

**Why this matters:** it's why you can hand MicroPython a whole `bytearray` and not worry about feeding the chip one byte at a time yourself — the FIFO and the SPI peripheral hardware handle the byte-by-byte shifting. Your Python code operates at the "message" level; the hardware operates at the "bit" level.

---

### Task 8.2 — Understand Chip Select as Transaction Framing

It's tempting to think of CS (Chip Select) as just "which chip is listening" — and on a bus with multiple SPI devices, that's part of its job. But on a single-device bus like yours, CS does something more important: **it frames the transaction.**

```
CS goes low   →  transaction begins, chip starts listening
... bytes shift back and forth on MOSI/MISO ...
CS goes high  →  transaction ends, chip commits/latches what it received
```

The **rising edge** of CS (low → high) is the moment the Winbond chip actually acts on what you sent. This is why the order of operations matters so much with flash chips — the command isn't "done" the instant you finish writing the opcode byte, it's done when CS returns high.

**Task:** Confirm this in your own datasheet. Look at the timing diagram for any instruction (e.g. Page Program) and find where `nCS` transitions relative to the last clock pulse. You should see the instruction is only latched in after CS rises.

---

### Task 8.3 — Understand "Active Low"

`nCS`, `nWP`, `nHOLD` — the `n` prefix (or the bar over the signal name in the datasheet) means the signal is **active low**: the "true"/asserted state is 0V, not the logic-HIGH you might intuitively expect.

```
CS = HIGH (idle)   → chip is NOT selected, ignoring the bus
CS = LOW (asserted) → chip IS selected, listening
```

**Why chips are designed this way:** it's a defensive default. If a wire breaks, a connector comes loose, or the Pico resets and its GPIO pins float, an active-low CS line pulled up by a resistor defaults to HIGH — meaning the chip defaults to *not* selected, and *not* accepting write commands. A floating active-*high* CS would be far more dangerous, since a disconnected wire could accidentally select the chip and let noise on the bus corrupt your flash.

---

### Task 8.4 — Recognize What's Missing: No Framing, No MTU, No Error Detection

Coming from network protocols, it's worth being explicit about what SPI *doesn't* give you:

```
Ethernet/TCP:  framing, MTU, checksums, retransmission, ACKs
SPI:           none of the above — just raw bits on a wire
```

There's no CRC, no length field, no acknowledgment. If a byte gets corrupted by electrical noise, SPI has no idea and neither does the Winbond chip — it just shifts in whatever voltage it sees at each clock edge. Reliability is entirely the physical layer's job: short wires, decent connections, a sane clock speed, and (if you're being careful) reading back what you wrote to verify it landed correctly.

---

### Task 8.5 — Baudrate Tradeoffs on a Breadboard

The Pico can clock its SPI peripheral fast — but a breadboard is not a PCB. Long jumper wires and breadboard contact resistance introduce capacitance and signal reflection that get worse as clock speed increases.

```
Low baud rate  (e.g. 1 MHz):   reliable on messy breadboard wiring, slower transfers
High baud rate (e.g. 20+ MHz): fine on a clean PCB, unreliable on long breadboard jumpers
```

**Practical takeaway:** if you're seeing garbage bytes back from the chip, or intermittent failures that disappear when you touch the wires, try dropping your SPI baud rate before you assume it's a logic bug. Breadboards are the most common source of "phantom" SPI errors.

---

## Part 9 — Talking to the Winbond: Write Enable, Busy Polling, and Page Limits

### Task 9.1 — Why Write Enable Needs Its Own Transaction

From Part 1 you already know the chip boots write-protected and needs a `06h` Write Enable before any Page Program or Erase. Here's the part that trips people up: **Write Enable and the command it unlocks must be two separate CS transactions**, not one combined transaction.

```
WRONG (single CS transaction):
CS low → send 06h, 02h, addr, data → CS high

RIGHT (two CS transactions):
CS low → send 06h → CS high              (Write Enable — latches WEL bit)
CS low → send 02h, addr, data → CS high  (Page Program — now allowed)
```

**Why:** the WEL (Write Enable Latch) bit is only set once CS rises after the `06h` command. If you keep CS held low and keep clocking bytes, the chip is still in the middle of the *same* instruction — it never sees a completed Write Enable instruction, so WEL never gets set, and your program/erase command will be silently ignored or rejected.

---

### Task 9.2 — Read Is Unbounded, Write Is Capped at One Page

This asymmetry is one of the more surprising things about NOR flash if you're used to reading and writing symmetrically:

```
Read Data (03h):    no upper limit — keep clocking, chip keeps outputting sequential bytes
Page Program (02h): hard capped at 256 bytes (one page)
```

**Why the read side is unbounded:** the Winbond chip has an internal address counter that auto-increments after every byte it shifts out. As long as you keep the clock running and CS low, it just keeps reading the next byte — even wrapping around to address 0 if you read past the top of the chip.

**Why the write side is capped:** internally, Page Program isn't really a "streaming write" — it's a write to an internal 256-byte buffer that gets committed to the flash cells at once. If you send more than 256 bytes in one Page Program instruction, the chip doesn't grow the buffer — the address **wraps around within the same page** and starts overwriting from the beginning of that page, silently corrupting your own write. This is a well-known gotcha and it's on you to chunk your writes into 256-byte pieces (or less) and issue a fresh Write Enable + Page Program per chunk.

---

### Task 9.3 — Poll BUSY, Don't Just Wait

You saw the theoretical busy-poll sequence in Part 1. In practice, you have two options:

```
Option A — Blocking sleep:
    send erase command
    time.sleep_ms(400)   # worst-case tSE, wastes time on the common case
    assume it's done

Option B — Busy polling:
    send erase command
    loop:
        read status register (05h)
        if BUSY bit == 0: break
    # only waits as long as actually necessary
```

**Why polling wins:** sector erase is 45ms typical but 400ms worst case — nearly a 9x difference. A blocking sleep has to assume the worst case every time, or risk reading/writing to a sector that isn't actually done erasing yet. Polling lets you move on the instant the chip reports it's ready, which matters a lot if you're erasing many sectors in a loop.

**Important distinction from Part 2:** this busy-wait only blocks *your SPI transaction loop* — it does **not** freeze the whole Pico the way an internal-flash write does. Internal flash operations (from Part 2) require MicroPython to disable interrupts and lock out the second core, because the CPU is literally trying to execute code from the same chip it's erasing. Your external Winbond chip has no such conflict — the Pico's own firmware lives on a completely separate physical chip, so a busy-wait loop here is just... a normal loop. The rest of your program, interrupts, and the other core are completely unaffected.

---

## Part 10 — Addressing and Why NOR Flash Has No "Seek Time"

### Task 10.1 — Why 24-Bit Addressing

A Winbond W25Q128 is 16MB. `2^24 = 16,777,216` bytes — exactly 16MB. That's not a coincidence: **the address width is sized to exactly span the chip.** A smaller chip (say, a W25Q16 at 2MB) can technically be addressed with fewer bits, but the SPI NOR command set standardized on 3 address bytes (24 bits) across the common capacity range, so the same command structure works whether the chip is 2MB or 16MB — the chip's internal decoder just ignores the address bits it doesn't need.

```
24 bits = 3 bytes = 16,777,216 addressable locations = 16MB
```

---

### Task 10.2 — Splitting an Integer Address Into 3 Wire Bytes

SPI only moves one byte at a time. Your address, however, is a single Python integer. To send it, you have to split it into its 3 constituent bytes using shifting and masking — the same technique from Part 2, just applied to an address instead of a `mem32` word.

```python
addr = 0x00ABCD12

byte0 = (addr >> 16) & 0xFF   # 0xAB — most significant byte, sent first
byte1 = (addr >> 8)  & 0xFF   # 0xCD
byte2 =  addr        & 0xFF   # 0x12 — least significant byte, sent last
```

Without the `& 0xFF` mask, a shift alone can still leave upper bits present in cases where the starting value has them — and just handing the raw shifted integer to `bytes()` or a byte array assignment will raise `ValueError: bytes value out of range` the moment the number exceeds 255. The mask is what guarantees you always get exactly one clean byte, regardless of what's above it.

**The four masks you'll ever need for this kind of work:**

```
0xFF       = 8 bits  = 0–255            (one byte)
0xFFFF     = 16 bits = 0–65,535         (two bytes)
0xFFFFFF   = 24 bits = 0–16,777,215     (three bytes — your whole address space)
0xFFFFFFFF = 32 bits = 0–4,294,967,295  (four bytes)
```

The pattern is always `2^n - 1` — all 1-bits for however many bits you want to keep, zeros everywhere else.

**Full address-to-wire example:**

```python
def address_bytes(addr):
    return bytes([
        (addr >> 16) & 0xFF,
        (addr >> 8)  & 0xFF,
        addr         & 0xFF,
    ])

# Sector Erase at 0x001000:
cmd = bytes([0x20]) + address_bytes(0x001000)
# cmd = b'\x20\x00\x10\x00'
```

If SPI could send a 24-bit integer as a single unit, none of this would be necessary. It can't — so shifting and masking is purely the mechanical cost of talking to a byte-oriented wire protocol with word-oriented data.

---

### Task 10.3 — Confirm There's No Seek-Time Penalty

NOR flash is **true random access** — every address costs the same number of clock cycles to reach, regardless of where it sits in the chip. This is worth confirming for yourself because it's easy to assume flash behaves like a disk.

```
NOR flash:   random access — address 0x000000 and 0xFFFFFF are equally fast
NAND flash:  page-based — must read a whole page, sequential access is faster than random
Hard drive:  seek time varies with the physical location of the data (moving parts)
SSD (NAND):  random access is measurably slower than sequential access
```

**The one exception:** erase timing (45ms typical, 400ms worst case from Part 1's Task 1.4) varies — but that's a *chip characteristic* tied to the physics of clearing floating-gate cells, not an *address-dependent* cost. Erasing sector 0 and erasing the last sector on the chip take the same expected time.

---

### Task 10.4 — Why Both Flash Chips on Your Desk Are "Misused" NOR

Here's a fun bit of context that ties Parts 1–3 together: the conventional wisdom is "NOR flash is for code, NAND flash is for bulk storage" — because NOR is fast at random access (good for executing code) but comparatively expensive per byte, while NAND is cheap and dense but page-based (good for bulk files).

Your Pico breaks that rule with its *internal* flash (Parts 1–2): it uses NOR for both firmware execution via XIP **and** the littlefs filesystem — because at 2MB, the density penalty of NOR doesn't matter, and random access matters a lot for XIP, where the CPU is constantly fetching instructions from arbitrary addresses.

Your *external* Winbond chip does the same thing at larger scale: 16MB of NOR being used purely as bulk storage, with no code execution involved at all. Unconventional against the textbook NOR/NAND split, but perfectly functional — a NAND controller is more complex than most small embedded projects need, and NOR at 16MB is cheap enough that the "should" doesn't really apply here.

---

## Summary — What You Added in Part 3

| Concept                                             | Where You Learned It                       |
| ---------------------------------------------------- | ------------------------------------------- |
| SPI is clock-driven, not self-timed                  | Understanding the master/slave relationship |
| Hardware FIFO buffers bytes between code and wire     | Pico SPI peripheral behavior                |
| CS frames the transaction; rising edge commits it     | Datasheet timing diagrams                   |
| Active-low signaling and why it's the safer default   | `nCS`, `nWP`, `nHOLD` naming convention      |
| Write Enable must be its own CS transaction           | WEL bit only latches after CS rises          |
| Read is unbounded, write is capped at 256 bytes/page  | Internal write-buffer behavior              |
| Busy polling vs. blind blocking sleep                 | tSE typical vs. worst-case timing            |
| External SPI flash doesn't freeze the Pico            | Contrast with Part 2's internal flash lockout|
| 24-bit addressing matches the chip's byte capacity    | `2^24 = 16MB`                                |
| Splitting an address into wire bytes via shift + mask | Extending Part 2's word-unpacking technique  |
| NOR flash has no seek-time penalty                    | Random access vs. NAND/HDD/SSD comparison    |
| NOR flash used for bulk storage on both chips          | Practical exception to the NOR/NAND rule     |

---

## The Three Ways to Read Flash — Now Complete

Part 2 ended with this table, with the third row still theoretical:

```
1. rp2.Flash.readblocks()   — filesystem region only, fast, C-level memcpy
2. machine.mem32            — all 512 blocks, read-only, Python-speed
3. Raw SPI (03h command)    — all 512 blocks, read+write+erase, requires external chip
```

Having now wired up and talked to an external Winbond chip over raw SPI, you've closed that last row yourself — command by command, byte by byte, with nothing between your code and the chip but a clock line and a shift register. Same underlying physics as the Pico's own internal flash from Parts 1 and 2, but with every abstraction layer stripped away.

---

*Built with curiosity, a datasheet, a breadboard, and too many REPL sessions. Not affiliated with Raspberry Pi, MicroPython, or Winbond. Corrections welcome.*

