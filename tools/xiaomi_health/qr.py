"""Small dependency-free QR SVG renderer for the one-time canary login."""
from __future__ import annotations


VERSION = 10
SIZE = 17 + VERSION * 4
DATA_CODEWORDS = 274
BLOCKS = ((68, 18), (68, 18), (69, 18), (69, 18))
ALIGNMENT = (6, 28, 50)


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for _ in range(8):
        if y & 1:
            z ^= x
        y >>= 1
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * degree
    result[-1] = 1
    root = 1
    for _ in range(degree):
        for i in range(degree):
            result[i] = _gf_mul(result[i], root)
            if i + 1 < degree:
                result[i] ^= result[i + 1]
        root = _gf_mul(root, 2)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for i, coefficient in enumerate(divisor):
            result[i] ^= _gf_mul(coefficient, factor)
    return result


def _codewords(text: str) -> list[int]:
    raw = text.encode("utf-8")
    if len(raw) > 271:
        raise ValueError("QR payload exceeds one-time login limit")
    bits: list[int] = []
    def push(value: int, width: int) -> None:
        bits.extend((value >> i) & 1 for i in reversed(range(width)))
    push(0b0100, 4)  # byte mode
    push(len(raw), 16)  # version 10 uses a 16-bit byte count
    for byte in raw:
        push(byte, 8)
    capacity = DATA_CODEWORDS * 8
    bits.extend([0] * min(4, capacity - len(bits)))
    bits.extend([0] * ((-len(bits)) % 8))
    data = []
    for offset in range(0, len(bits), 8):
        value = 0
        for bit in bits[offset:offset + 8]:
            value = (value << 1) | bit
        data.append(value)
    pad = (0xEC, 0x11)
    while len(data) < DATA_CODEWORDS:
        data.append(pad[(len(data) - (len(bits) // 8)) & 1])

    divisor = _rs_divisor(18)
    blocks = []
    offset = 0
    for length, _ecc in BLOCKS:
        chunk = data[offset:offset + length]
        offset += length
        blocks.append((chunk, _rs_remainder(chunk, divisor)))
    interleaved = []
    for i in range(max(len(block[0]) for block in blocks)):
        for chunk, _ in blocks:
            if i < len(chunk):
                interleaved.append(chunk[i])
    for i in range(18):
        for _, ecc in blocks:
            interleaved.append(ecc[i])
    return interleaved


def _bch(value: int, polynomial: int, degree: int) -> int:
    remainder = value << degree
    while remainder.bit_length() >= polynomial.bit_length():
        remainder ^= polynomial << (remainder.bit_length() - polynomial.bit_length())
    return (value << degree) | remainder


def qr_matrix(text: str) -> list[list[bool]]:
    values = _codewords(text)
    modules: list[list[bool | None]] = [[None] * SIZE for _ in range(SIZE)]

    def set_function(x: int, y: int, dark: bool) -> None:
        if 0 <= x < SIZE and 0 <= y < SIZE:
            modules[y][x] = dark

    def finder(cx: int, cy: int) -> None:
        for dy in range(-1, 8):
            for dx in range(-1, 8):
                x, y = cx + dx, cy + dy
                if 0 <= x < SIZE and 0 <= y < SIZE:
                    dark = 0 <= dx <= 6 and 0 <= dy <= 6 and (dx in {0, 6} or dy in {0, 6} or (2 <= dx <= 4 and 2 <= dy <= 4))
                    set_function(x, y, dark)

    finder(0, 0)
    finder(SIZE - 7, 0)
    finder(0, SIZE - 7)
    for i in range(8, SIZE - 8):
        set_function(i, 6, i % 2 == 0)
        set_function(6, i, i % 2 == 0)

    for cy in ALIGNMENT:
        for cx in ALIGNMENT:
            if modules[cy][cx] is not None:
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    set_function(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)

    # Format information: error-correction level L (01), fixed valid mask 0.
    format_value = _bch((0b01 << 3) | 0, 0x537, 10) ^ 0x5412
    for i in range(6):
        set_function(8, i, ((format_value >> i) & 1) != 0)
    set_function(8, 7, ((format_value >> 6) & 1) != 0)
    set_function(8, 8, ((format_value >> 7) & 1) != 0)
    set_function(7, 8, ((format_value >> 8) & 1) != 0)
    for i in range(9, 15):
        set_function(14 - i, 8, ((format_value >> i) & 1) != 0)
    for i in range(8):
        set_function(SIZE - 1 - i, 8, ((format_value >> i) & 1) != 0)
    for i in range(8, 15):
        set_function(8, SIZE - 15 + i, ((format_value >> i) & 1) != 0)
    set_function(8, SIZE - 8, True)

    # Version information is required by QR versions 7 and later.
    version_bits = _bch(VERSION, 0x1F25, 12)
    for i in range(18):
        bit = ((version_bits >> i) & 1) != 0
        a = SIZE - 11 + (i % 3)
        b = i // 3
        set_function(a, b, bit)
        set_function(b, a, bit)

    bit_stream = [(value >> shift) & 1 for value in values for shift in range(7, -1, -1)]
    index = 0
    right = SIZE - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(SIZE):
            y = SIZE - 1 - vert if ((right + 1) & 2) == 0 else vert
            for offset in range(2):
                x = right - offset
                if modules[y][x] is None:
                    bit = bit_stream[index] if index < len(bit_stream) else 0
                    index += 1
                    # Mask 0: (x + y) mod 2 == 0.
                    modules[y][x] = bool(bit ^ ((x + y) % 2 == 0))
        right -= 2
    return [[bool(cell) for cell in row] for row in modules]


def render_qr_svg(text: str, *, scale: int = 8, border: int = 4) -> str:
    matrix = qr_matrix(text)
    extent = len(matrix) + border * 2
    blocks = [f"<rect x='{x + border}' y='{y + border}' width='1' height='1'/>" for y, row in enumerate(matrix) for x, dark in enumerate(row) if dark]
    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{extent * scale}' height='{extent * scale}' "
        f"viewBox='0 0 {extent} {extent}' shape-rendering='crispEdges'>"
        f"<rect width='{extent}' height='{extent}' fill='white'/><g fill='black'>{''.join(blocks)}</g></svg>"
    )

