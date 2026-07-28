# MicroPython Raw Flash Exploration — Part 4
### Building MYFS: A Minimal Filesystem on External SPI Flash

---

> **Disclaimer:** This is not official documentation. It was developed through curiosity, hands-on experimentation, and AI-assisted learning. Complete Parts 1, 2, and 3 first. Expect mistakes. That's the point.

---

## What You'll Learn

- Why filesystems exist — the raw flash constraints that make them necessary
- How to design a directory entry and superblock from scratch
- How to pack and unpack integers into bytes for flash storage
- Why you can't just overwrite data in place
- How to build a working filesystem in MicroPython on a Winbond W25QXX chip
- The design decisions behind MYFS v0.9 and what v1 will improve

---

## Prerequisites

- Completed Parts 1, 2, and 3
- Working `flash_lib.py` with `read()`, `write()`, `erase()`, `block_erase()`
- External W25QXX chip wired to Pico SPI1 bus

---

## Why Do We Need a Filesystem?

You've been reading and writing raw bytes to flash. It works. So why add a filesystem on top?

Because raw flash has brutal constraints:

```
Problem 1: You can only flip bits 1→0 (write), never 0→1
Problem 2: Minimum erase unit is 4KB — you can't erase 1 byte
Problem 3: After erasing, you must rewrite 256 bytes at a time (one page)
Problem 4: There's no concept of "filename" — just addresses
Problem 5: There's no concept of "file size" — you must know how many bytes to read
```

A filesystem solves all of these by adding a metadata layer on top of raw blocks. It answers:
- Where does this file's data live?
- How big is it?
- What is it called?
- When was it created?

MYFS v0.9 is the simplest possible answer to those questions.

---

## Part 11 — MYFS v0.9 Design

### Constraints (Non-Negotiable for v0.9)

```
Max file size:    255 bytes  (fits in 1 byte size field)
Max files:        12         (directory fits in page 0 after superblock)
No deletion:      files are permanent until reformat
No updates:       files cannot be modified after creation
No wrapping:      each file occupies exactly one 256-byte page
No wear leveling: writes go sequentially, no spreading
```

These constraints make the implementation simple enough to understand completely. Every design decision has a reason rooted in flash physics.

---

### Physical Layout

```
Page 0  (bytes 0x000000 – 0x0000FF)   Superblock + Directory Table
Page 1  (bytes 0x000100 – 0x0001FF)   File 1 data
Page 2  (bytes 0x000200 – 0x0002FF)   File 2 data
Page 3  (bytes 0x000300 – 0x0003FF)   File 3 data
...
Page 12 (bytes 0x000C00 – 0x000CFF)   File 12 data
```

Page 0 is the filesystem's control center. Everything else is data.

---

### Superblock Layout (16 bytes)

The superblock is the filesystem's identity card. It lives at the very start of flash (byte 0) and is the first thing read on mount.

```
Offset 0–3:   Magic bytes b'MYFS'    4 bytes   identifies a formatted MYFS volume
Offset 4:     Version byte 0x01      1 byte    filesystem format version
Offset 5–6:   num_entries            2 bytes   file count (updated in v1)
Offset 7–15:  Reserved 0xFF × 9     9 bytes   future use
               ─────────────────────────────
               Total:                16 bytes
```

The magic bytes `b'MYFS'` are the most important field. On mount, if these 4 bytes aren't present, the chip is either unformatted or corrupt — abort immediately.

---

### Directory Entry Layout (19 bytes)

Each file has one directory entry in page 0, starting at byte 0x10 (right after the superblock). Entries are fixed-size — 19 bytes each, always.

```
Offset 0–7:   Filename   8 bytes   space-padded ASCII  e.g. b'hello   '
Offset 8–10:  Extension  3 bytes   space-padded ASCII  e.g. b'txt'
Offset 11–12: Start page 2 bytes   big-endian uint16   which page holds the data
Offset 13:    Size       1 byte    file size in bytes  max 255
Offset 14:    Status     1 byte    0xFF=empty 0x01=active
Offset 15–18: Timestamp  4 bytes   Unix epoch uint32
               ─────────────────────────────────────
               Total:    19 bytes
```

**Why fixed-size entries?** Because you can jump directly to entry N by computing `0x10 + (N × 19)`. No scanning, no delimiters, no variable-length parsing. The same reason FAT used 32-byte fixed directory entries in 1977.

**Why 8.3 filenames?** Same reason FAT did — it's the simplest format that's human-readable and fits in a fixed field. 8 characters for name, 3 for extension, space-padded.

**Directory capacity:**
```
Page 0 = 256 bytes
Superblock = 16 bytes
Remaining = 240 bytes
240 / 19 bytes per entry = 12 entries maximum
```

12 files. That's the v0.9 ceiling.

---

### Why Bits and Bytes Matter

Before implementing, two concepts you need cold:

**Shifting and masking** — the only way to split an integer into bytes for the SPI wire:

```python
# Split address 256 (0x000100) into 3 bytes for SPI:
addr = 256
high = (addr >> 16) & 0xFF   # 0x00 — shift 16 bits right, keep lowest 8
mid  = (addr >> 8)  & 0xFF   # 0x01 — shift 8 bits right, keep lowest 8
low  = addr & 0xFF            # 0x00 — keep lowest 8 bits
# Result: [0x00, 0x01, 0x00] — page 1 start address
```

**Timestamps as integers, not strings:**

```python
# WRONG — stores 10 ASCII bytes on flash:
'1609459200'.encode()      # b'1609459200'  10 bytes

# RIGHT — stores 4 bytes on flash:
int_to_4bytes(1609459200)  # b'_\xf0\x98\x00'  4 bytes

# Read back:
int.from_bytes(b'_\xf0\x98\x00', 'big')  # 1609459200
```

---

## Part 12 — Helper Functions

### Address Conversion

```python
def int_to_addr(val):
    """Convert integer to 3-byte list for SPI addresses."""
    return [
        (val >> 16) & 0xFF,   # high byte: bits 23–16
        (val >> 8)  & 0xFF,   # mid byte:  bits 15–8
        val & 0xFF            # low byte:  bits 7–0
    ]

def int_to_4bytes(val):
    """Convert integer to 4-byte big-endian bytes (for timestamps)."""
    return bytes([
        (val >> 24) & 0xFF,
        (val >> 16) & 0xFF,
        (val >> 8)  & 0xFF,
        val & 0xFF
    ])
```

---

## Part 13 — Filesystem Operations

### Task 13.1 — Format

`format_disk()` must erase before writing — flash bits only go 1→0. The entire first block (64KB) is erased to guarantee all 12 data pages start as `0xFF`.

**Why `block_erase()` and not `erase()`?**
`erase()` clears 4KB (one sector = 16 pages). `block_erase()` clears 64KB (256 pages). Since MYFS v0.9 uses pages 0-12, one sector erase would cover them — but `block_erase()` gives a clean slate with one command.

```python
def format_disk(start_block=[0x00, 0x00, 0x00]):
    block_erase(start_block)    # erase 64KB — all data pages guaranteed 0xFF
    
    sb_data = (b'MYFS'             +
               bytes([0x01])       +   # version 1
               bytes([0x00, 0x00]) +   # num_entries = 0
               bytes([0xFF]) * 9)      # reserved
    
    write(sb_data, start_block)
    
    version, _ = validate_superblock()
    if version is not None:
        print("Format successful!")
        return True
    print("Format FAILED!")
    return False
```

**Verify format worked:**

```python
format_disk()
a = read(4096, [0x00, 0x10, 0x00])   # read first data sector
print(all(b == 0xFF for b in a))      # True = clean slate confirmed
```

---

### Task 13.2 — Mount

```python
def validate_superblock():
    data = read(16)
    if data[0:4] == b'MYFS':
        version     = data[4]
        num_entries = int.from_bytes(data[5:7], 'big')
        return version, num_entries
    return None, None

def mount():
    version, num_entries = validate_superblock()
    if version is not None:
        print(f"MYFS Mounted! (Version: {version}, Files: {num_entries})")
        return version, num_entries
    raise RuntimeError("Mount Error: Invalid or missing MYFS Superblock!")
```

Always validate before any file operation. If magic bytes are wrong — stop.

---

### Task 13.3 — Directory Table

Reading the directory table once into RAM is far more efficient than one SPI read per entry:

```python
def read_directory_table():
    # One SPI read covers all 12 possible entries (240 bytes)
    # vs 12 separate reads in a naive implementation
    return read(addr=[0x00, 0x00, 0x10], num_bytes=256-0x10)
```

All subsequent directory operations work on this in-memory copy — no further SPI reads needed.

---

### Task 13.4 — Create File

The most complex operation. Three things must happen in order:

1. Check for duplicate filename
2. Write file content to the next free page
3. Write directory entry to the next empty slot

**Why content before directory entry?** If power fails between the two writes, an orphaned data page (no directory entry) is safer than a directory entry pointing to garbage data.

```python
def create_file(file_name, content):
    dir_data = read_directory_table()   # one read, used for everything
    
    next_slot = None
    for slot in range(len(dir_data) // 19):
        offset = slot * 19
        if dir_data[offset] == 0xFF:    # empty slot found
            if next_slot is None:
                next_slot = slot
            break
        # Check for duplicate filename
        fname = dir_data[offset:offset+8].rstrip(b' \x00\xff').decode('latin-1')
        fext  = dir_data[offset+8:offset+11].rstrip(b' \x00\xff').decode('latin-1')
        if fname + '.' + fext == file_name:
            raise RuntimeError(f"File already exists: {file_name}")
    
    if next_slot is None:
        raise RuntimeError("Directory full — MYFS v0.9 supports 12 files maximum")
    
    start_page  = next_slot + 1                          # page 0 = directory
    dentry_addr = [0x00, 0x00, 0x10 + (next_slot * 19)] # slot's flash address
    
    dentry = format_meta_data(file_name, content, start_page)
    write(content, addr=int_to_addr(start_page * 256))  # write data first
    write(dentry,  addr=dentry_addr)                     # then directory entry
```

---

### Task 13.5 — Read File

```python
def read_file(file_name):
    dir_data = read_directory_table()   # one SPI read
    for slot in range(len(dir_data) // 19):
        offset = slot * 19
        if dir_data[offset] == 0xFF:
            break
        fname = dir_data[offset:offset+8].rstrip(b' \x00\xff').decode('latin-1')
        fext  = dir_data[offset+8:offset+11].rstrip(b' \x00\xff').decode('latin-1')
        if fname + '.' + fext == file_name:
            start_page = int.from_bytes(dir_data[offset+11:offset+13], 'big')
            size       = dir_data[offset+13]
            return read(num_bytes=size, addr=int_to_addr(start_page * 256))
    return None   # file not found
```

---

### Task 13.6 — List Files

```python
def list_files():
    dir_data = read_directory_table()   # one SPI read
    files = []
    for slot in range(len(dir_data) // 19):
        offset = slot * 19
        if dir_data[offset] == 0xFF:
            break
        fname = dir_data[offset:offset+8].rstrip(b' \x00\xff').decode('latin-1')
        fext  = dir_data[offset+8:offset+11].rstrip(b' \x00\xff').decode('latin-1')
        files.append(f"{fname}.{fext}")
    return files
```

---

## Part 14 — Testing MYFS v0.9

### test_start.py — Format and Verify

```python
from flash_lib import *

format_disk()
print(f"Files after format: {list_files()}")

# Verify all data pages are clean
a = read(4096, [0x00, 0x10, 0x00])
print(f"Data pages clean: {all(b == 0xFF for b in a)}")
```

**Expected output:**
```
Format successful!
Files after format: []
Data pages clean: True
```

---

### test_create.py — Create and Read Back

```python
from flash_lib import *

# Create a file
create_file('hello.txt', 'wasssup')

# Verify it appears in directory
files = list_files()
assert 'hello.txt' in files, "File not found in directory"

# Read it back and verify content
data = read_file('hello.txt')
assert data == b'wasssup', f"Content mismatch: {data}"

print("File Create Test Success")
```

---

### test_limits.py — Boundary Conditions

```python
from flash_lib import *

format_disk()

# Fill to capacity
for x in range(12):
    create_file(f"file_{x}.txt", f"data {x}")

assert len(list_files()) == 12, "Expected 12 files"

# 13th file should fail
try:
    create_file('overflow.txt', 'boom')
    print("FAILED — should have raised RuntimeError")
except RuntimeError as e:
    print(f"Overflow correctly rejected: {e}")

# Duplicate should fail
try:
    create_file('file_0.txt', 'duplicate')
    print("FAILED — should have raised RuntimeError")
except RuntimeError as e:
    print(f"Duplicate correctly rejected: {e}")

print("Limit Tests Passed")
```

---

## Part 15 — Design Decisions and Lessons

### Why Not Update num_entries in the Superblock?

The superblock lives at the start of flash. Updating it requires:
1. Read all 256 bytes of page 0 into RAM
2. Modify `num_entries` at offset 5-6
3. Erase sector 0 (destroying all directory entries temporarily)
4. Write all 256 bytes back

That's a dangerous operation — if power fails during step 3 or 4, the entire filesystem is gone. This is exactly the problem that journaling filesystems (ext4, APFS) solve. Too complex for v0.9.

In v0.9, `num_entries` in the superblock is always `0x0000`. Use `len(list_files())` instead.

---

### Why No Deletion?

Deleting a file requires erasing its page. But the minimum erase unit is 4KB = 16 pages. Erasing one file's page would also erase 15 other files' pages.

The correct solution (read-modify-write at sector level) is complex and causes extra wear. v0.9 skips it entirely. Reformat to reclaim space.

---

### Why One SPI Read Per Directory Operation?

Early versions of the directory functions did one SPI read per entry — up to 12 reads to scan the full directory. `read_directory_table()` replaced all of them with one read, working from RAM.

One SPI transaction vs 12. The pattern generalizes: **read once, work in RAM, write once**.

This is exactly what real operating systems do — they cache the filesystem metadata in RAM (called the "buffer cache" or "page cache") to avoid repeated disk reads.

---

### What v0.9 Taught You

```
Raw flash constraints   → why filesystems exist
Fixed-size entries      → why FAT used 32-byte directory entries
Magic bytes             → how every filesystem identifies itself
Superblock              → why every filesystem has one
Write-before-directory  → why write ordering matters for consistency
One read, work in RAM   → the buffer cache pattern
12-file limit           → why inode tables exist
```

---

## MYFS v1 — Planned Features

```
update_num_entries()    update superblock file count on create
disk_info()             show free slots, used pages, capacity
verify_file()           read back and checksum against original
file_exists()           exposed as standalone function
Extended directory      span multiple pages, break 12-file limit
Soft delete             mark status 0x00, reclaim slot on reformat
CRC on superblock       detect corruption mid-write
```

---

## If you followed, this is the stack you built

```
read_file('hello.txt')           ← you, today
    ↓
MYFS directory scan              ← Part 4
    ↓
read() / write() / erase()       ← Part 3 (flash_lib.py)
    ↓
SPI opcodes (03h, 02h, 20h...)   ← Part 1 (datasheet)
    ↓
machine.SPI + CS pin             ← MicroPython
    ↓
Transistors switching on/off     ← silicon
    ↓
Electrons on floating gates      ← physics
```

You built every layer from the bottom up.

---

*Built with curiosity, a W25Q128JV, and too many `TypeError: unsupported types for __add__` errors. Not affiliated with Raspberry Pi, MicroPython, or Winbond. Corrections welcome.*

