# MicroPython Raw Flash Exploration — Part 2
### Reading Flash via XIP, Scanning the Full Map, and Finding Your Files

---

> **Disclaimer:** This is not official documentation. It was developed through curiosity, hands-on REPL experimentation, and AI-assisted learning. Read Part 1 first.  Assume innocent mistakes.

---

## What You'll Learn

- How to read raw flash directly via `machine.mem32` and XIP
- How 32-bit integers map to bytes (shifting and masking)
- How to scan the entire 2MB flash chip from MicroPython
- How to find your own files in raw flash
- Why deleted files aren't really gone
- How littlefs stores small files inline (no separate data block)
- How to dynamically map the firmware/filesystem boundary

---

## Prerequisites

- Completed Part 1
- A Raspberry Pi Pico (non-W) with MicroPython
- A serial terminal

---

## Part 5 — Reading Flash via XIP with `machine.mem32`

### Background

`rp2.Flash` only exposes the filesystem region. But there's another way to read flash — directly through the XIP memory map using `machine.mem32`.

The RP2040 maps all 2MB of flash into the CPU's address space starting at `0x10000000`. Every raw flash address has a corresponding XIP address:

```
raw flash 0x000000  →  XIP 0x10000000
raw flash 0x0A0000  →  XIP 0x100A0000
raw flash 0x1FFFFF  →  XIP 0x101FFFFF
```

`machine.mem32` reads 4 bytes at a time from any memory address — including XIP flash addresses. No SPI commands, no block numbers, no abstraction. The XIP hardware handles the SPI transaction invisibly.

**Limitation:** Read-only. Writes via `mem32` into the XIP region don't work — you'd need to suspend XIP, which requires C.

---

### Task 5.1 — Read the Stage2 Bootloader

The very first bytes of flash (`0x10000000`) are the RP2040 stage2 bootloader — 256 bytes of ARM Thumb-2 code that configures the flash chip for XIP before MicroPython starts.

```python
import machine

for i in range(0, 16, 4):
    print(hex(machine.mem32[0x10000000 + i]))
```

**Expected output (approximate):**

```
0x4b32b500
0x60582021
0x21026898
0x60984388
```

That's real ARM machine code. Not gibberish.

**Prove it — read the vector table:**

The ARM vector table starts at `0x10000100` (right after the 256-byte stage2). The first entry is the initial stack pointer — always a RAM address starting with `0x2`:

```python
print(hex(machine.mem32[0x10000100]))
```

**Expected output:**

```
0x20042000
```

`0x20042000` is the top of the RP2040's 264KB SRAM (`0x20000000` base + 264KB). That's the stack pointer sitting exactly where ARM convention puts it. Documented, verifiable, not coincidence.

---

### Task 5.2 — Understand Shifting and Masking

`machine.mem32` returns a 32-bit integer — 4 bytes packed together. To work with individual bytes you need two operations:

**Right shift (`>>`):** Moves bits to the right, bringing a higher byte down to the lowest position.

**Bitwise AND mask (`& 0xFF`):** Zeroes out everything except the bottom 8 bits.

Truth table for AND:
```
0 AND 0 = 0
0 AND 1 = 0
1 AND 0 = 0
1 AND 1 = 1
```

`0xFF` in binary is `00000000 00000000 00000000 11111111` — a stencil that lets only the bottom 8 bits through.

**Example — unpacking one 32-bit word:**

```python
word = 0x74657374  # 4 bytes packed together

byte0 = word & 0xFF          # 0x74  't'
byte1 = (word >> 8)  & 0xFF  # 0x73  's'
byte2 = (word >> 16) & 0xFF  # 0x65  'e'
byte3 = (word >> 24) & 0xFF  # 0x74  't'
```

**To read 32 bytes cleanly from a flash address:**

```python
import machine

addr = 0x100A1000  # filesystem block 1 in XIP

result = []
for i in range(0, 32, 4):
    word = machine.mem32[addr + i]
    result.append(word & 0xFF)
    result.append((word >> 8)  & 0xFF)
    result.append((word >> 16) & 0xFF)
    result.append((word >> 24) & 0xFF)

print(bytes(result))
```

**Expected output:**

```
b'littlefs/...'
```

The littlefs magic string — right there in raw flash, read via XIP with no filesystem abstraction.

---

### Task 5.3 — Scan All 512 Blocks via XIP

The entire 2MB flash chip is 512 blocks of 4096 bytes. One `mem32` read per block gives you a signature for the entire chip:

```python
import machine

for block in range(512):
    addr = 0x10000000 + (block * 4096)
    word = machine.mem32[addr]
    
    chars = ''
    for shift in [0, 8, 16, 24]:
        b = (word >> shift) & 0xFF
        chars += chr(b) if 32 <= b <= 126 else '.'
    
    print(f"block {block:3d} @ {hex(addr)}: {hex(word):12} {chars}")
```

Watch for:
- `from`, `impo`, `## `, `#inc` — Python source in the filesystem
- `littlefs` — filesystem superblock
- Non-printable hex — ARM machine code in firmware

**Key observation:** Two tools, one chip:

| Tool | Blocks visible | Address space |
|---|---|---|
| `rp2.Flash` | 352 (filesystem only) | block 0 = `0x0A0000` |
| `machine.mem32` | 512 (all of flash) | block 0 = `0x10000000` |

The same filesystem block 1 (`rp2.Flash`) maps to XIP block 161 (`machine.mem32`):

```
0x100A1000 - 0x10000000 = 0x0A1000
0x0A1000 - 0x0A0000 = 0x1000 = block 1 in filesystem terms
0x0A1000 / 4096 = 161 in absolute terms
```

---

## Part 6 — Finding Your Files in Raw Flash

### Task 6.1 — Search for File Content with `rp2.Flash`

`rp2.Flash.readblocks()` requires a pre-allocated `bytearray` as a buffer — the docs don't mention this. The `in` operator works on bytearrays, not integers, which is why you need one.

```python
import rp2

f = rp2.Flash()
buf = bytearray(4096)
target = b'your text here'

for block in range(352):
    f.readblocks(block, buf)
    if target in buf:
        print(f"found in block {block}")
        idx = buf.index(target)
        print(buf[idx-4:idx+30])
        break
```

**Why `rp2.Flash` is faster than `mem32` for this:** `readblocks` copies an entire 4096-byte block in one C `memcpy`. `mem32` requires 1024 individual reads plus 4096 Python unpack operations per block. Python interpreter overhead is significant.

---

### Task 6.2 — Discover littlefs Inline Storage

Create a small file and search for its content:

```python
# First write a small file
with open('test.txt', 'w') as f:
    f.write('hello world')

# Then search for it
import rp2
f = rp2.Flash()
buf = bytearray(4096)

for block in range(352):
    f.readblocks(block, buf)
    if b'hello world' in buf:
        print(f"found in block {block}")
        idx = buf.index(b'hello world')
        print(buf[idx-8:idx+20])
```

**What you'll likely see:** The content appears in block 0 or 1 — the same block as the directory metadata. That's **littlefs inline storage**: files small enough to fit in the metadata block get stored right alongside the filename. No separate data block allocated.

This is a real filesystem optimization you can observe directly in raw flash.

---

### Task 6.3 — Discover That Deleted Files Aren't Gone

Delete your file, then search again:

```python
import os
os.remove('test.txt')

# Now search raw flash
import rp2
f = rp2.Flash()
buf = bytearray(4096)

for block in range(352):
    f.readblocks(block, buf)
    if b'hello world' in buf:
        print(f"still found in block {block}")
```

**Expected:** Still found. littlefs marks blocks as free in its metadata but never erases them. The data physically remains until that sector is reused and overwritten.

**Implication:** Sensitive data written to flash doesn't disappear when you delete the file. This is true of all flash-based storage systems.

---

### Task 6.4 — Search All 512 Blocks via `mem32`

This searches the entire flash including firmware — not just the filesystem:

```python
import machine

target = b'hello world'
buf = bytearray(4096)

for block in range(512):
    addr = 0x10000000 + (block * 4096)
    for i in range(0, 4096, 4):
        word = machine.mem32[addr + i]
        buf[i]   =  word        & 0xFF
        buf[i+1] = (word >> 8)  & 0xFF
        buf[i+2] = (word >> 16) & 0xFF
        buf[i+3] = (word >> 24) & 0xFF
    if target in buf:
        print(f"found in block {block} @ {hex(addr)}")
        idx = buf.index(target)
        print(buf[idx-4:idx+20])
```

This is slow — 512 blocks × 1024 `mem32` reads × 4 Python unpack operations. Use `rp2.Flash` when you only need the filesystem. Use `mem32` when you need the full picture.

---

## Part 7 — Dynamically Map the Flash Boundary

### Task 7.1 — Let the OS Tell You

Instead of hardcoding addresses or guessing from heuristics, query the OS directly. This script derives the complete flash map from live runtime data:

```python
import os
import sys
import gc

def map_pico_flash():
    # Query VFS internals for block device geometry
    try:
        vfs_mount = os.VFS.mounts()[0]
        raw_bdev = vfs_mount[0]
    except (AttributeError, IndexError):
        raw_bdev = None

    if raw_bdev and hasattr(raw_bdev, 'ioctl'):
        try:
            bdev_block_size = raw_bdev.ioctl(4, 0)
            lfs_total_blocks = raw_bdev.ioctl(5, 0)
        except Exception:
            bdev_block_size = 4096
            lfs_total_blocks = os.statvfs('/')[2]
    else:
        vfs_stats = os.statvfs('/')
        bdev_block_size = vfs_stats[0]
        lfs_total_blocks = vfs_stats[2]

    FLASH_BASE_ADDR = 0x10000000
    TOTAL_FLASH_SIZE = 2 * 1024 * 1024

    lfs_total_bytes = lfs_total_blocks * bdev_block_size
    vfs_stats = os.statvfs('/')
    lfs_free_blocks = vfs_stats[3]

    lfs_start_offset = TOTAL_FLASH_SIZE - lfs_total_bytes
    lfs_start_block = lfs_start_offset // bdev_block_size
    firmware_size_bytes = lfs_start_offset
    firmware_end_block = lfs_start_block - 1

    print("=" * 50)
    print("      PICO HARDWARE FLASH MEMORY MAP       ")
    print("=" * 50)
    print(f"Platform:                  {sys.platform.upper()}")
    print(f"XIP Flash Window Base:     {hex(FLASH_BASE_ADDR)}")
    print(f"Sector Size (Hardware):    {bdev_block_size} bytes ({bdev_block_size // 1024} KB)")
    print("-" * 50)
    print("--- [FIRMWARE SEGMENT] ---")
    print(f"Flash Offset Range:        0x00000000 -> {hex(firmware_size_bytes - 1)}")
    print(f"Physical Memory Address:   {hex(FLASH_BASE_ADDR)} -> {hex(FLASH_BASE_ADDR + firmware_size_bytes - 1)}")
    print(f"Sector Range:              Block 0 -> Block {firmware_end_block}")
    print(f"Space Allocated:           {firmware_size_bytes / 1024:.2f} KB")
    print("-" * 50)
    print("--- [LITTLEFS VOLUME SEGMENT] ---")
    print(f"Flash Offset Range:        {hex(lfs_start_offset)} -> {hex(TOTAL_FLASH_SIZE - 1)}")
    print(f"Physical Memory Address:   {hex(FLASH_BASE_ADDR + lfs_start_offset)} -> {hex(FLASH_BASE_ADDR + TOTAL_FLASH_SIZE - 1)}")
    print(f"Sector Range:              Block {lfs_start_block} -> Block {(lfs_start_block + lfs_total_blocks) - 1}")
    print(f"Total Allocated Blocks:    {lfs_total_blocks}")
    print(f"Filesystem Capacity:       {lfs_total_bytes / 1024:.2f} KB")
    print(f"Available Free Space:      {(lfs_free_blocks * bdev_block_size) / 1024:.2f} KB")
    print("=" * 50)

map_pico_flash()
```

**Expected output (RPI_PICO stable):**

```
==================================================
      PICO HARDWARE FLASH MEMORY MAP       
==================================================
Platform:                  RP2
XIP Flash Window Base:     0x10000000
Sector Size (Hardware):    4096 bytes (4 KB)
--------------------------------------------------
--- [FIRMWARE SEGMENT] ---
Flash Offset Range:        0x00000000 -> 0x9ffff
Physical Memory Address:   0x10000000 -> 0x1009ffff
Sector Range:              Block 0 -> Block 159
Space Allocated:           640.00 KB
--------------------------------------------------
--- [LITTLEFS VOLUME SEGMENT] ---
Flash Offset Range:        0xa0000 -> 0x1fffff
Physical Memory Address:   0x100a0000 -> 0x101fffff
Sector Range:              Block 160 -> Block 511
Total Allocated Blocks:    352
Filesystem Capacity:       1408.00 KB
Available Free Space:      1400.00 KB
==================================================
```

**Why this is better than heuristics:** Byte-level scanning to detect the firmware/filesystem boundary is unreliable — ARM instructions contain coincidental printable ASCII bytes that fool pattern matching. The OS already knows the boundary. Ask it directly.

---

## Summary — What You Added in Part 2

| Concept | How You Learned It |
|---|---|
| `machine.mem32` reads flash via XIP | Read stage2 bootloader bytes directly |
| ARM vector table structure | Read initial stack pointer at `0x10000100` |
| Bit shifting and masking | Unpacked 32-bit words into bytes |
| Full 512-block flash map | Scanned all 2MB via single `mem32` reads |
| Two block numbering systems | `rp2.Flash` vs absolute XIP block numbers |
| littlefs inline storage | Small files stored in directory block itself |
| Deleted files aren't erased | Searched raw flash after `os.remove()` |
| Dynamic flash map | Queried OS instead of hardcoding addresses |

---

## The Three Ways to Read Flash

You now have three ways to read the same physical byte:

```
1. rp2.Flash.readblocks()   — filesystem region only, fast, C-level memcpy
2. machine.mem32            — all 512 blocks, read-only, Python-speed
3. Raw SPI (03h command)    — all 512 blocks, read+write+erase, requires external chip
```

Same chip, same data, three layers of abstraction removed one at a time.

---

*Built with curiosity, a datasheet, and too many REPL sessions. Not affiliated with Raspberry Pi, MicroPython, or Winbond. Corrections welcome.*
