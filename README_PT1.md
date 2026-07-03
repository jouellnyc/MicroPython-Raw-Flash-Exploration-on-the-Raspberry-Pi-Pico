# MicroPython Raw Flash Exploration on the Raspberry Pi Pico
### A Hands-On Courseware Guide

---



> **Disclaimer:** This is not official documentation. It was developed through curiosity, datasheet reading, source code spelunking, and AI-assisted learning over the course of an evening. Expect rough edges. Expect to hit walls. Probably errors. That's the point, we are learning, documenting, and discovering right now, we are not deploying production devices.

---

## What You'll Learn

By the end of this guide you will understand:

- How SPI NOR flash works at the command level
- The Pico's flash memory map and how it's carved up
- What XIP (execute-in-place) is and why it matters
- How MicroPython's `rp2.Flash` API works and what it actually exposes
- Where the boundaries are between firmware, filesystem, and raw flash
- Why some things that seem possible aren't — and what that teaches you

---

## Prerequisites

- A Raspberry Pi Pico (non-W, plain RP2040)
- MicroPython flashed (stable build from [micropython.org](https://micropython.org/download/RPI_PICO/))
- A serial terminal (Thonny, mpremote, screen, etc.)
- The [MicroPython source repo](https://github.com/micropython/micropython) cloned locally
- The [Winbond W25Q16 datasheet](https://www.winbond.com/hq/product/code-storage-flash-memory/serial-nor-flash/?__locale=en&partNo=W25Q16JV) open in a browser

---

## Part 1 — Read the Datasheet Like a Systems Programmer

The flash chip on your Pico is a Winbond W25Q16. Every SPI NOR flash chip speaks a command set. Before writing a single line of code, you need to know the commands.

### Task 1.1 — Find the Instruction Table

Open the W25Q16 datasheet and find the instruction set table.

**Find these three things:**

1. The byte value (opcode) for the **Read Data** instruction
2. The byte value for the **Sector Erase** instruction
3. The **sector size** and **page size**

**Expected findings:**

```
Read Data:     03h
Sector Erase:  20h
Sector size:   4KB (4096 bytes)
Page size:     256 bytes
```

**Why this matters:** The sector is your minimum erase unit. The page is your maximum write unit. These two numbers govern everything about how you interact with flash.

---

### Task 1.2 — Understand the Erase/Write Hierarchy

From the datasheet, find this passage about programmable pages:

> 8,192 programmable pages of 256-bytes each. Pages can be erased in groups of 16 (4KB sector erase)...

**Internalize this hierarchy:**

```
256 bytes  = 1 page       (write unit)
4096 bytes = 1 sector     (minimum erase unit)
           = 16 pages per sector
```

**The key insight:** You erase 4KB at a time but write 256 bytes at a time. To change a single byte you must:

1. Read the entire 4KB sector into RAM
2. Modify your byte
3. Erase the sector (sets all bits to 1 / `FFh`)
4. Write back 256 bytes at a time

This is called **read-modify-write** and it's the fundamental pain of flash storage.

---

### Task 1.3 — Find Write Enable

Before any erase or write, the chip requires an explicit unlock. Find the **Write Enable** instruction.

**Expected finding:**

```
Write Enable:  06h
```

From the datasheet:

> After power-up the device is automatically placed in a write-disabled state with the Status Register Write Enable Latch (WEL) set to a 0. A Write Enable instruction must be issued before a Page Program, Sector Erase, Block Erase, Chip Erase or Write Status Register instruction will be accepted. After completing a program, erase or write instruction the Write Enable Latch (WEL) is automatically cleared.

**Why this matters:** The chip boots write-protected. You must explicitly unlock it before every write or erase, and it automatically re-locks after each operation.

---

### Task 1.4 — Find the Busy Flag

Erase operations take time — up to 400ms for a sector. Find **tSE** in the timing table and the **Read Status Register** instruction.

**Expected findings:**

```
Sector Erase Time (tSE):  45ms typical, 400ms max
Read Status Register:     05h
BUSY bit:                 bit 0 of Status Register 1
```

**A complete sector erase sequence looks like this:**

```
1. CS low → send: 06h              → CS high   (Write Enable)
2. CS low → send: 20h 00 10 00     → CS high   (Sector Erase at address 0x001000)
3. Loop:
       CS low → send: 05h → read status byte → CS high
       check bit 0 (BUSY) — if 1, still erasing, loop
       if 0, done
```

A read is simpler — no write enable, no busy wait:

```
1. CS low → send: 03h 00 10 00     (Read Data at address 0x001000)
2. Clock out as many bytes as you want
3. CS high
```

---

## Part 2 — Understand the RP2040 Flash Map

### Background: XIP

The RP2040 can't execute code from a SPI flash chip directly — SPI is a serial protocol, not a memory bus. **XIP (execute-in-place)** is hardware that sits between the CPU and the flash chip. It speaks SPI to the flash on one side and looks like regular memory to the CPU on the other.

The CPU thinks it's reading from address `0x10000000`. Underneath, the XIP hardware is doing a SPI transaction to fetch those bytes from the Winbond chip.

**Two address spaces for the same physical flash:**

| Space | Base Address | Used by |
|---|---|---|
| Raw flash | `0x000000` | SPI commands, datasheet |
| XIP (memory-mapped) | `0x10000000` | CPU, linker, ELF files |

To convert: subtract `0x10000000` from any XIP address to get the raw flash offset.

**Important constraint:** You cannot use XIP and raw SPI at the same time. They're both talking to the same physical chip. MicroPython disables XIP (and interrupts) during raw flash operations, which is why flash access in MicroPython must run from RAM, not flash.

---

### Task 2.1 — Find the Filesystem Size in Source

Clone the MicroPython repo and find where the filesystem size is defined for your board.

```bash
grep -r MICROPY_HW_FLASH_STORAGE_BYTES ports/rp2/
```

**Expected finding (RPI_PICO):**

```
ports/rp2/mpconfigport.h:#define MICROPY_HW_FLASH_STORAGE_BYTES (1408 * 1024)
```

**Expected finding (RPI_PICO_W):**

```
ports/rp2/boards/RPI_PICO_W/mpconfigboard.cmake:set(MICROPY_HW_FLASH_STORAGE_BYTES 868352)  # 848 * 1024
```

The W variant reserves more flash for the CYW43 WiFi driver. This is why you should use a plain Pico for this exercise.

**Flash layout comparison:**

```
Pico (RPI_PICO)                      Pico W (RPI_PICO_W)
2MB total                             2MB total

0x000000  ┐                           0x000000  ┐
          │  MicroPython firmware                │  MicroPython firmware
          │  + CYW43 WiFi driver
          │                                      │
0x0A0000  ┼  littlefs (1408KB)        0x12C000  ┼  littlefs (848KB)
          │                                      │
0x200000  ┘                           0x200000  ┘
```

---

### Task 2.2 — Find Where the Filesystem Starts

```bash
grep -r MICROPY_HW_FLASH_STORAGE_BASE ports/rp2/rp2_flash.c
```

**Expected finding:**

```c
#define MICROPY_HW_FLASH_STORAGE_BASE (PICO_FLASH_SIZE_BYTES - MICROPY_HW_FLASH_STORAGE_BYTES)
```

The filesystem lives at the **top** of flash. Do the math in Python:

```python
total = 2 * 1024 * 1024        # 2MB flash
fs_size = 1408 * 1024          # 1408KB filesystem
fs_start = total - fs_size

print(f"fs starts at: {fs_start} ({hex(fs_start)})")
```

**Expected output:**

```
fs starts at: 655360 (0xa0000)
```

---

### Task 2.3 — Find the Firmware End Address

You need `arm-none-eabi-objdump` installed (`apt install gcc-arm-none-eabi`). Then:

```bash
arm-none-eabi-objdump -t ports/rp2/build-RPI_PICO/firmware.elf | grep __flash_binary_end
```

**Expected output (approximate):**

```
10086500 g    .stack_dummy   00000000 __flash_binary_end
```

Strip the XIP offset:

```python
xip_end = 0x10086500
fw_end = xip_end - 0x10000000
print(hex(fw_end))  # 0x86500 — firmware ends here in raw flash
```

---

### Task 2.4 — Calculate Your Playground

```python
total = 2 * 1024 * 1024
fs_size = 1408 * 1024
fs_start = total - fs_size
fw_end = 0x086500

print(f"fs starts at:   {hex(fs_start)}")
print(f"firmware ends:  {hex(fw_end)}")
print(f"playground:     {fs_start - fw_end} bytes")
print(f"                {(fs_start - fw_end) // 4096} blocks")
```

**Theoretical output (based on source):**

```
fs starts at:   0xa0000
firmware ends:  0x86500
playground:     105216 bytes
                25 blocks
```

**Flash map (theoretical, stable build):**

```
0x000000  ┐
          │  MicroPython firmware (~537KB)
          │
0x086500  ┼  firmware ends here
          │
          │  ~115KB unused (your playground)
          │  blocks 134–159
          │
0x0A0000  ┼  littlefs filesystem starts (1408KB)
          │
0x200000  ┘  end of flash
```

> **Note:** The ELF analysis is based on a specific build. The actual firmware running on your Pico may be larger (especially preview builds), leaving no gap. See Part 3 for how to verify empirically.

---

## Part 3 — Explore Flash with `rp2.Flash`

### Task 3.1 — Query the Block Device

```python
import rp2

f = rp2.Flash()
block_size  = f.ioctl(5, 0)
block_count = f.ioctl(4, 0)

print(f"block size:  {block_size} bytes")
print(f"block count: {block_count}")
print(f"total:       {block_size * block_count} bytes ({block_size * block_count // 1024}KB)")
```

**Expected output (RPI_PICO stable):**

```
block size:  4096 bytes
block count: 352
total:       1441792 bytes (1408KB)
```

**Important:** `rp2.Flash` is scoped to the **filesystem region only**. Block 0 here is not raw flash address `0x000000` — it's the start of the filesystem at `0x0A0000`. You cannot access firmware via this API. That's a deliberate safety boundary.

---

### Task 3.2 — Read Raw Blocks

`readblocks` requires a pre-allocated buffer. This is not obvious from the docs.

```python
buf = bytearray(4096)
f.readblocks(0, buf)
print(buf[:64])
```

**What you might see:**

```
bytearray(b'2\x01\x00\x00\xff\xef\xff\xf4__init__.py 0\x00\x03\x8a...')
```

That's littlefs directory metadata. You're looking at the raw on-disk format of the filesystem — filenames, block references, checksums — all as bytes.

---

### Task 3.3 — Scan for Empty Blocks

Unused flash blocks read back as all `0xFF`. Scan to find where your files live vs. where there's free space:

```python
buf = bytearray(4096)
for block in range(0, 20):
    f.readblocks(block, buf)
    all_ff = buf == bytearray(b'\xff' * 4096)
    print(f"block {block}: {'empty' if all_ff else 'has data'}")
```

---

### Task 3.4 — Find the Real Firmware Boundary

The gap between firmware and filesystem (if any) is not accessible via `rp2.Flash`. But you can verify the filesystem start empirically by reading from the bottom of the `rp2.Flash` block space and seeing what's there.

If all blocks have data, the firmware end on your running binary aligns exactly with the filesystem start — no gap. This is common on preview builds which tend to be larger.

---

## Part 4 — What You Hit (The Ceiling)

### Why There's No Playground on Preview Builds

The theoretical gap between firmware end and filesystem start exists when firmware is small. Stable builds (~537KB) leave ~115KB of gap. Preview builds are larger and consume it entirely.

### Why `rp2.Flash` Can't Access Firmware

`rp2.Flash` is intentionally scoped. From `rp2_flash.c`:

```c
static rp2_flash_obj_t rp2_flash_obj = {
    .flash_base = MICROPY_HW_FLASH_STORAGE_BASE,
    .flash_size = MICROPY_HW_FLASH_STORAGE_BYTES,
};
```

It only knows about the filesystem partition. The firmware region is protected. This is not a bug.

**What `rp2.Flash` can and cannot see:**

```
0x000000  ┐
          │  MicroPython firmware       ← NOT accessible via rp2.Flash
          │
0x086500  ┼  firmware end
          │
          │  playground gap             ← NOT accessible via rp2.Flash
          │
0x0A0000  ┼─────────────────────────── ← rp2.Flash block 0 starts here
          │
          │  littlefs filesystem        ← rp2.Flash blocks 0–351
          │
0x200000  ┘─────────────────────────── ← rp2.Flash block 351 ends here
```

### Why XIP and Raw SPI Can't Coexist

From `rp2_flash.c`:

```c
static uint32_t begin_critical_flash_section(void) {
    multicore_lockout_start_blocking();
    uint32_t state = save_and_disable_interrupts();
    return state;
}
```

Before any flash write or erase, MicroPython disables interrupts and locks out core 1. XIP is suspended. The code doing the flash operation must already be in RAM. This is why raw flash access is a privileged, carefully managed operation — not something you can do casually from a Python loop.

---

## Summary — What You Actually Learned

| Concept | Where You Learned It |
|---|---|
| SPI NOR flash command set | W25Q16 datasheet |
| Erase/write granularity | Datasheet + math |
| Write enable / busy polling | Datasheet |
| XIP and dual address spaces | RP2040 architecture |
| Flash memory map | MicroPython source + `objdump` |
| `rp2.Flash` block API | REPL experimentation |
| littlefs on-disk format | Reading raw blocks |
| Where MicroPython ends and C begins | Hitting the ceiling |

---

## Going Further

To access raw flash addresses below the filesystem boundary you need to leave MicroPython and go to C:

- **Pico C SDK:** `flash_range_erase()` and `flash_range_program()` in `hardware/flash.h`
- **`@micropython.viper`:** Gets you closer to the metal but still can't bypass the Flash object's scope
- **Custom MicroPython C module:** The right path if you want raw flash access with a Python interface

The MicroPython layer is powerful but it has deliberate boundaries. Hitting those boundaries is how you learn where they are.

---

*Built with curiosity, a datasheet, and too many REPL sessions. Not affiliated with Raspberry Pi, MicroPython, or Winbond. Corrections welcome.*

