# =============================================================================
# flash_lib.py — MYFS: A Minimal SPI NOR Flash Filesystem for MicroPython
# =============================================================================
#
# This library implements a simple filesystem called MYFS on top of a raw
# Winbond W25QXX SPI NOR flash chip (e.g. W25Q128JV — 16MB).
#
# It communicates directly with the chip using SPI opcodes from the datasheet.
# No OS, no abstraction layer — just bytes on a wire.
#
# MYFS V1 Constraints (by design):
#   - Max file size: 255 bytes (fits in 1 byte)
#   - Max files:     ~11 (directory fits in first page, after superblock)
#   - No deletion:   status byte marks files active but never removed
#   - No wrapping:   each file occupies exactly one 256-byte page
#   - No wear leveling, no journaling, no error correction
#
# Physical Flash Layout (W25Q128, 16MB):
#   Page 0   (bytes 0x000000 – 0x0000FF):  superblock + directory entries
#   Page 1   (bytes 0x000100 – 0x0001FF):  file 1 data
#   Page 2   (bytes 0x000200 – 0x0002FF):  file 2 data
#   ...and so on
#
# Superblock Layout (16 bytes, starting at byte 0x000000):
#   Offset 0–3:   Magic bytes b'MYFS'   — identifies a formatted MYFS volume
#   Offset 4:     Version byte 0x01     — filesystem format version
#   Offset 5–6:   num_entries (2 bytes) — count of files (big-endian uint16)
#   Offset 7–15:  Reserved (0xFF × 9)  — future use, left as erased flash
#
# Directory Entry Layout (19 bytes each, starting at byte 0x000010):
#   Offset 0–7:   Filename  (8 bytes, space-padded, ASCII)
#   Offset 8–10:  Extension (3 bytes, space-padded, ASCII)
#   Offset 11–12: Start page (2 bytes, big-endian uint16)
#   Offset 13:    Size in bytes (1 byte, max 255)
#   Offset 14:    Status (0xFF = empty, 0x01 = active file)
#   Offset 15–18: Timestamp (4 bytes, Unix epoch, big-endian uint32)
#
# SPI Commands Used (from Winbond W25Q128 datasheet):
#   0x03  Read Data          — stream bytes from any address
#   0x02  Page Program       — write up to 256 bytes to one page
#   0x20  Sector Erase (4KB) — erase 4KB sector back to 0xFF
#   0xD8  Block Erase (64KB) — erase 64KB block back to 0xFF
#   0x06  Write Enable       — unlock chip for write/erase (auto-relocks after)
#   0x05  Read Status Reg 1  — check BUSY bit before next command
#
# Key Flash Memory Rules:
#   1. You can only flip bits from 1 → 0 (writing). Never 0 → 1.
#   2. To flip bits back to 1, you must ERASE (minimum 4KB sector).
#   3. Max write unit = 256 bytes (one page). Writes wrap at page boundaries.
#   4. Always poll BUSY bit after erase/write — chip ignores commands while busy.
#   5. Write Enable must be sent before EVERY erase or write operation.
#
# =============================================================================

from spi_config import spi, cs   # SPI bus and chip select pin, pre-configured
import time


# =============================================================================
# ADDRESS HELPERS
# Flash addresses are 24-bit (3 bytes) because 16MB = 2^24 bytes.
# SPI sends one byte at a time, so we must split integers into byte lists.
# Bit shifting extracts each byte: >> moves the target byte to position 0,
# & 0xFF masks off everything except the lowest 8 bits.
# =============================================================================

def int_to_addr(val):
    """
    Convert an integer to a 3-element list of bytes for use as a SPI address.

    Flash addresses are 24-bit (3 bytes). SPI sends one byte at a time,
    so we must split the integer manually using bit shifting and masking.

    Example:
        int_to_addr(256)  →  [0x00, 0x01, 0x00]  (page 1 start address)
        int_to_addr(512)  →  [0x00, 0x02, 0x00]  (page 2 start address)

    Args:
        val (int): Address value, 0 to 16,777,215 (0x000000 to 0xFFFFFF)

    Returns:
        list: 3-byte address [high, mid, low]
    """
    return [
        (val >> 16) & 0xFF,   # high byte: bits 23–16
        (val >> 8)  & 0xFF,   # mid byte:  bits 15–8
        val & 0xFF            # low byte:  bits 7–0
    ]


def int_to_4bytes(val):
    """
    Convert an integer to a 4-byte big-endian bytes object.

    Used for packing 32-bit values (e.g. Unix timestamps) into flash storage.
    A Unix timestamp like 1609459200 looks like 10 ASCII characters as a string,
    but only needs 4 bytes as a raw integer — much more efficient.

    Example:
        int_to_4bytes(1609459200)  →  b'_\xf0\x98\x00'  (4 bytes)
        '1609459200'.encode()      →  b'1609459200'      (10 bytes — wasteful!)

    Args:
        val (int): 32-bit unsigned integer (0 to 4,294,967,295)

    Returns:
        bytes: 4-byte big-endian representation
    """
    return bytes([
        (val >> 24) & 0xFF,   # highest byte: bits 31–24
        (val >> 16) & 0xFF,   # high byte:    bits 23–16
        (val >> 8)  & 0xFF,   # low byte:     bits 15–8
        val & 0xFF            # lowest byte:  bits 7–0
    ])


# =============================================================================
# LOW-LEVEL SPI FLASH PRIMITIVES
# These functions talk directly to the Winbond chip using datasheet opcodes.
# CS (chip select) must go LOW before a command and HIGH after.
# The chip is active-low: CS=0 means "I'm talking to you", CS=1 means "ignore".
# The rising edge of CS commits write/erase operations — critical timing.
# =============================================================================

def wait_busy():
    """
    Poll the chip's Status Register 1 (opcode 05h) until the BUSY bit clears.

    After any erase or write command, the chip sets bit 0 of Status Register 1
    (the BUSY bit) to 1. It stays 1 until the internal operation completes.
    Any commands sent while BUSY=1 are silently ignored by the chip.

    Status Register 1 bit map:
        bit 0 = BUSY  (1 = operation in progress, 0 = ready)
        bit 1 = WEL   (1 = write enabled, 0 = write protected)
        bits 2–7 = other flags (not used here)

    Typical wait times:
        Page Program (write): ~0.4ms typical, 3ms max
        Sector Erase (4KB):   ~45ms typical, 400ms max
        Block Erase (64KB):   ~150ms typical, 2000ms max
    """
    while True:
        cs.value(0)                    # select chip — begin SPI transaction
        spi.write(bytes([0x05]))       # send Read Status Register 1 opcode
        status = spi.read(1)[0]       # read 1 byte; [0] extracts int from bytes
        cs.value(1)                    # deselect chip — ALWAYS release CS!
        if not (status & 0x01):       # check bit 0: if 0, chip is ready
            break
        time.sleep_ms(1)              # wait 1ms before polling again


def en_write():
    """
    Send the Write Enable command (opcode 06h).

    The Winbond chip boots in write-protected mode. Before ANY erase or write
    operation, you MUST send Write Enable to set the Write Enable Latch (WEL).

    The WEL bit automatically clears after every completed erase or write —
    so Write Enable must be re-sent before each operation. This is a hardware
    safety mechanism to prevent accidental writes.

    Transaction: CS low → 06h → CS high
    The WEL bit is SET on the rising edge of CS after the 06h command.
    """
    cs.value(0)                    # select chip
    spi.write(bytes([0x06]))       # Write Enable opcode
    cs.value(1)                    # WEL latch sets on this rising CS edge


def erase(addr=[0x00, 0x00, 0x00]):
    """
    Erase a 4KB sector at the given address (opcode 20h — Sector Erase).

    Sets all 4,096 bytes in the sector to 0xFF (all bits = 1).
    This is the MINIMUM erase unit — you cannot erase less than 4KB.

    Flash memory can only flip bits from 1→0 (writing) or 0→1 (erasing).
    Erasing is the only way to restore bits to 1, and it always affects
    the entire 4KB sector containing the target address.

    IMPORTANT: The address is rounded DOWN to the nearest 4KB sector boundary
    by the chip. Erasing address 0x001234 erases the sector 0x001000–0x001FFF.

    Args:
        addr (list): 3-byte address list e.g. [0x00, 0x00, 0x00]
                     Defaults to sector 0 (the superblock/directory sector).

    After this call returns, the erase is guaranteed complete (busy-polled).
    """
    en_write()                              # unlock chip for erase
    cs.value(0)                             # select chip
    spi.write(bytes([0x20] + addr))         # Sector Erase opcode + 24-bit address
    cs.value(1)                             # erase begins inside chip on this rising edge
    wait_busy()                             # block until erase completes (up to 400ms)


def block_erase(addr=[0x00, 0x00, 0x00]):
    """
    Erase a 64KB block at the given address (opcode D8h — Block Erase).

    Erases 65,536 bytes (64KB = 16 sectors = 256 pages) back to 0xFF.
    16x larger than sector erase, but proportionally faster per byte.

    Use this when you need to erase large regions quickly.
    Use sector erase (erase()) when you only need to clear a small area
    to minimize wear — every erase cycle degrades the flash cells slightly.

    The W25Q128 is rated for ~100,000 erase cycles per sector.
    Unnecessary block erases waste that budget.

    Args:
        addr (list): 3-byte address list e.g. [0x00, 0x00, 0x00]
                     Address is rounded down to nearest 64KB boundary.

    After this call returns, the erase is guaranteed complete (busy-polled).
    """
    en_write()                              # unlock chip for erase
    cs.value(0)                             # select chip
    spi.write(bytes([0xD8] + addr))         # Block Erase opcode + 24-bit address
    cs.value(1)                             # erase begins on this rising CS edge
    wait_busy()                             # block until complete (up to 2000ms)


def write(b_text, addr=[0x00, 0x00, 0x00]):
    """
    Write up to 256 bytes to flash at the given address (opcode 02h — Page Program).

    PAGE BOUNDARY WARNING: Flash pages are 256-byte aligned. If your write
    starts at address 0x0000F0 (240) and is longer than 16 bytes, it will
    WRAP AROUND to the beginning of the page — silently overwriting earlier data.
    Always start writes at page-aligned addresses (multiples of 256).

    PRE-ERASE REQUIREMENT: Flash bits can only go from 1→0 (programming).
    The target region MUST be erased (all 0xFF) before writing, or existing
    0-bits will corrupt your data. No error is raised — you just get wrong data.

    The chip auto-converts str to bytes via UTF-8 encoding, so you can pass
    either a Python string or a bytes object.

    Args:
        b_text (str or bytes): Data to write. Max 255 bytes in MYFS V1.
        addr (list): 3-byte address list e.g. [0x00, 0x01, 0x00] for page 1.

    After this call returns, the write is guaranteed complete (busy-polled).
    """
    # SPI speaks raw bytes only. Python strings are Unicode objects — not the same.
    # encode('utf-8') converts each ASCII character to its 1-byte value.
    # e.g. 'A' → 0x41, 'hello' → b'\x68\x65\x6c\x6c\x6f'
    if isinstance(b_text, str):
        b_text = b_text.encode('utf-8')

    en_write()                                          # unlock chip for write
    cs.value(0)                                         # select chip
    spi.write(bytes([0x02] + addr) + b_text)            # Page Program opcode + address + data
    cs.value(1)                                         # write commits on this rising CS edge
    wait_busy()                                         # block until write completes (~3ms)


def read(num_bytes=32, addr=[0x00, 0x00, 0x00]):
    """
    Read bytes from flash starting at the given address (opcode 03h — Read Data).

    Unlike writes, reads have NO page boundary limit — you can read across
    pages, sectors, and blocks continuously in one transaction. The chip
    auto-increments the address with every clock pulse.

    Reads are PASSIVE — no voltage stress, no wear, no BUSY wait needed.
    The chip responds immediately with data on every clock edge.
    Reading the same address 1 million times causes zero degradation.

    Args:
        num_bytes (int): Number of bytes to read. Default 32. No hardware limit.
        addr (list): 3-byte address list e.g. [0x00, 0x00, 0x00]

    Returns:
        bytes: Raw bytes read from flash. Index with [n] to get individual
               byte values as integers (e.g. result[0] gives an int, not a char).
    """
    cs.value(0)                             # select chip
    spi.write(bytes([0x03] + addr))         # Read Data opcode + 24-bit start address
    result = spi.read(num_bytes)            # clock out num_bytes, capture on MISO
    cs.value(1)                             # deselect chip
    return result                           # returns bytes object


# =============================================================================
# FILESYSTEM OPERATIONS
# Built on top of the low-level SPI primitives above.
# =============================================================================

def validate_superblock():
    """
    Read and validate the MYFS superblock at address 0x000000.

    The superblock is the filesystem's identity card. On every mount,
    we read it first to confirm this chip contains a valid MYFS volume
    (not random data, a different filesystem, or an unformatted chip).

    The magic bytes b'MYFS' are the identifier. If they're present,
    we trust the rest of the superblock and return its contents.

    Superblock layout (16 bytes):
        [0:4]  Magic bytes b'MYFS'
        [4]    Version (currently 0x01)
        [5:7]  num_entries as big-endian uint16
        [7:16] Reserved (0xFF × 9)

    Returns:
        tuple: (version: int, num_entries: int) if valid
               (None, None) if superblock is missing or corrupt
    """
    data = read(16)                                 # read full 16-byte superblock
    if data[0:4] == b'MYFS':                        # check magic identifier
        version = data[4]                           # filesystem version number
        # int.from_bytes() converts raw bytes to integer.
        # b'\x00\x05' with 'big' endian = (0 × 256) + 5 = 5 files
        num_entries = int.from_bytes(data[5:7], 'big')
        return version, num_entries
    return None, None                               # not a valid MYFS volume


def mount():
    """
    Mount the MYFS filesystem and verify the superblock.

    'Mounting' means verifying the chip is formatted and readable before
    performing any file operations. Real operating systems do this too —
    Linux runs fsck on mount, macOS checks the journal, etc.

    In MYFS V1, mounting just validates the magic bytes and reports the
    filesystem version and file count. No caching, no state — stateless.

    Returns:
        tuple: (version: int, num_entries: int) on success

    Raises:
        RuntimeError: If the superblock is missing or invalid.
    """
    version, num_entries = validate_superblock()
    if version is not None:
        print(f"MYFS Mounted Successfully! (Version: {version}, Files: {num_entries})")
        return version, num_entries
    # raise stops execution and propagates the error to the caller
    raise RuntimeError("Mount Error: Invalid or missing MYFS Superblock!")


def format_disk(start_block=[0x00, 0x00, 0x00]):
    """
    Erase sector 0 and write a fresh MYFS superblock. Destructive — all data lost.

    'Formatting' erases the directory sector and writes a clean superblock.
    This is equivalent to mkfs on Linux or Format Disk on Windows.

    After format_disk(), the chip has:
        - A valid MYFS superblock at byte 0
        - Zero directory entries
        - All data pages untouched (still contain old data until overwritten)

    Note: Only sector 0 (4KB) is erased. Old file data on pages 1+ remains
    physically on the chip until overwritten — it's just unreachable without
    directory entries pointing to it.

    Args:
        start_block (list): Address of the superblock sector. Default [0,0,0].

    Returns:
        bool: True if format verified successfully, False if readback failed.
    """
    # erase only sector 0 (directory) + sectors covering 12 data pages
    # sector 0 covers pages 0-15, more than enough for V1
    for sector in range(1):     
        erase(int_to_addr(sector * 4096))

    # Build the 16-byte superblock as a single bytes object.
    # bytes([n]) creates a single byte with integer value n.
    # Concatenating bytes objects with + produces one contiguous bytes object.
    sb_data = (b'MYFS'              +   # 4-byte magic identifier
               bytes([0x01])        +   # version 1
               bytes([0x00, 0x00])  +   # num_entries = 0 (no files yet)
               bytes([0xFF]) * 9)       # 9 reserved bytes (0xFF = erased state)

    write(sb_data, start_block)         # write superblock to flash

    # Verify the write took by reading back and checking magic bytes.
    # If this fails, the chip may be damaged or wiring is wrong.
    version, _ = validate_superblock()
    if version is not None:
        print("Format successful!")
        return True
    else:
        print("Format FAILED! Silicon readback mismatch.")
        return False


def format_meta_data(file_name, content, start_page=1):
    """
    Build a 19-byte directory entry for a file.

    A directory entry is the filesystem's record of a file — its name,
    where its data lives, how big it is, and when it was created.
    This is the MYFS equivalent of an inode (Linux) or directory entry (FAT).

    Entry layout (19 bytes total):
        [0:8]   Filename, space-padded to 8 bytes   e.g. b'hello   '
        [8:11]  Extension, space-padded to 3 bytes  e.g. b'txt'
        [11:13] Start page as big-endian uint16     e.g. b'\x00\x01' = page 1
        [13]    File size in bytes (max 255)         e.g. b'\x07' = 7 bytes
        [14]    Status byte                          0x01 = active file
        [15:19] Unix timestamp as big-endian uint32  e.g. b'_\xeep\x14'

    Args:
        file_name (str): Filename in 8.3 format e.g. 'hello.txt'
        content (str or bytes): File content (used only to measure size)
        start_page (int): Page number where file data will be stored

    Returns:
        bytes: 19-byte directory entry ready to write to flash
    """
    # Split 'hello.txt' into ('hello', 'txt')
    file_name, file_ext = file_name.split('.')

    # Pad or truncate to exactly 8 and 3 characters (8.3 filename format).
    # '{:<8}'.format(x) left-aligns x in an 8-character field, space-padding.
    # [:8] ensures we never exceed 8 characters even if input is longer.
    file_name = '{:<8}'.format(file_name)[:8]
    file_ext  = '{:<3}'.format(file_ext)[:3]

    # Encode to bytes — SPI only speaks raw bytes, not Python strings.
    # For ASCII text, each character becomes exactly one byte.
    file_name_b = file_name.encode('utf-8')    # b'hello   ' (8 bytes)
    file_ext_b  = file_ext.encode('utf-8')     # b'txt'      (3 bytes)

    # Pack start_page integer into 2 bytes, big-endian.
    # Page 1 = 0x0001 → bytes [0x00, 0x01]
    # Same shifting/masking as int_to_addr, just 2 bytes instead of 3.
    start_page_b = bytes([start_page >> 8, start_page & 0xFF])

    # File size: how many bytes of content. Must fit in 1 byte (max 255 — V1 limit).
    size = bytes([len(content)])

    # Status byte: 0x01 = active file, 0xFF = empty slot (erased flash default)
    status = bytes([0x01])

    # Timestamp: current Unix time packed into 4 bytes.
    # int(time.time()) gives seconds since Jan 1, 1970 as a Python integer.
    # We pack it into 4 bytes rather than storing the 10-character string.
    timestamp = int_to_4bytes(int(time.time()))

    # Concatenate all fields into one 19-byte entry.
    # All fields must be bytes type — mixing bytes with int or list causes TypeError.
    return file_name_b + file_ext_b + start_page_b + size + status + timestamp

def read_directory_table():
    """
    Read the entire directory table in one SPI transaction.
    
    The directory table occupies bytes 0x10–0xFF of page 0 (240 bytes max).
    Reading it all at once is far more efficient than one SPI read per entry.
    
    In MYFS V1, the directory table holds a maximum of 12 entries:
        240 bytes available / 19 bytes per entry = 12 files
    
    Returns:
        bytes: Raw directory table (240 bytes). Index by slot:
               slot 0 → [0:19], slot 1 → [19:38], slot 2 → [38:57], etc.
    """
    # Skip the 16-byte superblock (0x10 = 16) and read the rest of page 0.
    # 256 - 0x10 = 240 bytes — the entire directory region in one shot.
    return read(addr=[0x00, 0x00, 0x10], num_bytes=256-0x10)

def get_next_page(debug=False):
    """
    Find the next available data page number for a new file.
    
    Reads the directory table once into RAM, then scans entries to count
    how many files already exist. The next free page = file count + 1
    (page 0 is reserved for the superblock and directory).
    
    An empty slot has 0xFF as its first byte — the erased flash default.
    Active entries always start with an ASCII filename character (never 0xFF).
    
    Args:
        debug (bool): If True, prints slot and offset for each entry scanned.
    
    Returns:
        int: Next free page number (1-based), or None if directory is full.
    """
    dir_data = read_directory_table()           # one SPI read for the whole directory

    for slot in range(len(dir_data) // 19):    # 12 slots max in V1
        offset = slot * 19                      # byte offset within dir_data for this slot

        if debug:
            print(slot, offset, dir_data[offset]) # useful for troubleshooting layout issues

        if dir_data[offset] == 0xFF:            # 0xFF = empty slot, no file here
            print(f"slot {slot + 1} is empty")
            return slot + 1                     # +1 because page 0 is the directory

    return None                                 # all 12 slots occupied — directory full

def get_next_dentry_addr():
    """
    Find the flash address of the next empty directory slot.
    
    Same scan logic as get_next_page() but returns the 3-byte flash address
    of the empty slot rather than the page number. This address is passed
    directly to write() when saving a new directory entry.
    
    V1 note: All directory entries live in page 0 (bytes 0x10–0xFF),
    so byte_addr always fits in the low byte of the 3-byte address.
    This means [0x00, 0x00, byte_addr] is sufficient for V1's 12-file limit.
    A V2 implementation with more directory pages should use int_to_addr().
    
    Returns:
        list: 3-byte address of next empty slot e.g. [0x00, 0x00, 0x10]
              for the very first entry, or None if directory is full.
    """
    dir_data = read_directory_table()           # one SPI read for the whole directory

    for slot in range(len(dir_data) // 19):    # 12 slots max in V1
        offset = slot * 19                      # byte offset within dir_data for this slot

        if dir_data[offset] == 0xFF:            # 0xFF = empty slot
            byte_addr = 0x10 + offset           # convert to flash address (add superblock offset)
            return [0x00, 0x00, byte_addr]      # 3-byte address — V1 safe, all entries in page 0

    return None                                 # all 12 slots occupied — directory full


def create_file(file_name, content):
    """
    Create a new file: write content to a data page and add a directory entry.

    MYFS V1 limits:
        - Max file size: 255 bytes (enforced by 1-byte size field)
    """
    dir_data = read_directory_table()  # one read, use for everything
    
    next_slot = None
    for slot in range(len(dir_data) // 19):
        offset = slot * 19
        if dir_data[offset] == 0xFF:
            if next_slot is None:
                next_slot = slot  # first empty slot found
            break
        fname = dir_data[offset:offset+8].rstrip(b' \x00\xff').decode('latin-1')
        fext  = dir_data[offset+8:offset+11].rstrip(b' \x00\xff').decode('latin-1')
        if fname + '.' + fext == file_name:
            raise RuntimeError(f"File already exists: {file_name}")
    
    if next_slot is None:
        raise RuntimeError("Directory full — MYFS V1 supports 12 files maximum")
    
    start_page  = next_slot + 1
    dentry_addr = [0x00, 0x00, 0x10 + (next_slot * 19)]
    
    dentry = format_meta_data(file_name, content, start_page)
    write(content, addr=int_to_addr(start_page * 256))
    write(dentry, addr=dentry_addr)


def read_file(file_name):
    """
    Find a file by name and return its contents from flash.
    Args:
        file_name (str): Filename in 8.3 format e.g. 'hello.txt'
    Returns:
        bytes: File content, or None if not found.
    """
    dir_data = read_directory_table()  # one SPI read
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
    return None


def list_files():
    """
    List all active files in the MYFS directory.

    In MYFS V1, files are written sequentially and never deleted, so the
    directory is always a contiguous block of active entries followed by
    empty slots. There are no gaps to skip.

    List all active files in the MYFS directory.
    Returns a list of filenames in 8.3 format e.g. ['hello.txt', 'world.txt']
    """
    dir_data = read_directory_table()  # one SPI read
    files = []
    for slot in range(len(dir_data) // 19):
        offset = slot * 19
        if dir_data[offset] == 0xFF:   # empty slot — no more files
            break
        fname = dir_data[offset:offset+8].rstrip(b' \x00\xff').decode('latin-1')
        fext  = dir_data[offset+8:offset+11].rstrip(b' \x00\xff').decode('latin-1')
        files.append(f"{fname}.{fext}")
    return files

