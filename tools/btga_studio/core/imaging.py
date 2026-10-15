"""Image files from outside the games: PNG, JPEG, BMP and TGA to RGBA8, and texture fitting.

Pure Python plus numpy (no imaging library is bundled).  Every decoder checks
sizes before allocating and raises ``ImageError`` with a sentence a user can
act on.  Supported:

* PNG: every colour type and bit depth of the specification, palette and
  ``tRNS`` transparency, Adam7 interlacing.
* JPEG: baseline and extended sequential Huffman (SOF0/SOF1), greyscale,
  YCbCr and Adobe RGB/CMYK-free three-component images, any sampling factors,
  restart intervals.  Progressive and arithmetic JPEGs are refused with advice.
* BMP: uncompressed 1/4/8/24/32-bit and bit-field 16/32-bit, top-down or bottom-up.
* TGA: true-colour, greyscale and colour-mapped, raw or run-length encoded.

Decoded rows run top to bottom, matching Studio's texture convention
(v = 0 is the first row).
"""

from __future__ import annotations

import struct
import zlib

import numpy as np

MAX_INPUT_PIXELS = 8192 * 8192
MAX_INPUT_SIDE = 8192
TEXTURE_SIZES = (64, 128, 256, 512, 1024, 2048, 4096)  # 2048 and 4096 are for sky panoramas


class ImageError(ValueError):
    pass


def sniff(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:2] == b"BM":
        return "bmp"
    return None


def decode(data: bytes, name: str = "image") -> tuple[int, int, bytes]:
    """(width, height, RGBA8 bytes) of a PNG, JPEG, BMP or TGA file."""

    kind = sniff(data)
    if kind is None and name.lower().endswith((".tga", ".targa")):
        kind = "tga"
    try:
        if kind == "png":
            pixels = _png(data)
        elif kind == "jpeg":
            pixels = _jpeg(data)
        elif kind == "bmp":
            pixels = _bmp(data)
        elif kind == "tga":
            pixels = _tga(data)
        else:
            raise ImageError(f"{name} is not a PNG, JPEG, BMP or TGA image")
    except ImageError:
        raise
    except (struct.error, IndexError, ValueError, zlib.error) as error:
        raise ImageError(f"{name} is damaged or truncated ({error})") from error
    height, width = pixels.shape[:2]
    return width, height, np.ascontiguousarray(pixels, dtype=np.uint8).tobytes()


def _check_size(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise ImageError("the image has no pixels")
    if width > MAX_INPUT_SIDE or height > MAX_INPUT_SIDE or width * height > MAX_INPUT_PIXELS:
        raise ImageError(f"the image is {width}x{height}; images up to {MAX_INPUT_SIDE}x{MAX_INPUT_SIDE} can be imported")


# ---------------------------------------------------------------------- PNG --

ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))


def _unfilter(raw: memoryview, width: int, height: int, bits_per_pixel: int, offset: int) -> tuple[np.ndarray, int]:
    """Undo the PNG row filters of one image (or interlace pass); returns (rows as uint8 [h, stride], bytes used)."""

    stride = (width * bits_per_pixel + 7) // 8
    bpp = max(1, bits_per_pixel // 8)
    out = np.zeros((height, stride), dtype=np.uint8)
    previous = np.zeros(stride, dtype=np.int32)
    for row in range(height):
        start = offset + row * (stride + 1)
        if start + stride + 1 > len(raw):
            raise ImageError("the PNG image data is truncated")
        kind = raw[start]
        line = np.frombuffer(raw[start + 1:start + 1 + stride], dtype=np.uint8).astype(np.int32)
        if kind == 0:
            pass
        elif kind == 1:
            line = (np.cumsum(np.pad(line, (0, (-stride) % bpp)).reshape(-1, bpp), axis=0).reshape(-1)[:stride]) & 0xFF
        elif kind == 2:
            line = (line + previous) & 0xFF
        elif kind in (3, 4):
            values = line.tolist()
            up = previous.tolist()
            for i in range(stride):
                left = values[i - bpp] if i >= bpp else 0
                if kind == 3:
                    values[i] = (values[i] + ((left + up[i]) >> 1)) & 0xFF
                else:
                    corner = up[i - bpp] if i >= bpp else 0
                    estimate = left + up[i] - corner
                    pa, pb, pc = abs(estimate - left), abs(estimate - up[i]), abs(estimate - corner)
                    values[i] = (values[i] + (left if pa <= pb and pa <= pc else up[i] if pb <= pc else corner)) & 0xFF
            line = np.asarray(values, dtype=np.int32)
        else:
            raise ImageError(f"the PNG uses an unknown row filter ({kind})")
        out[row] = line
        previous = line
    return out, height * (stride + 1)


def _samples(rows: np.ndarray, width: int, depth: int, channels: int) -> np.ndarray:
    """Unpacked samples [h, w, channels] scaled to 8 bits (palette indices are left unscaled)."""

    if depth == 8:
        return rows[:, :width * channels].reshape(rows.shape[0], width, channels)
    if depth == 16:
        return rows[:, :width * channels * 2].reshape(rows.shape[0], width, channels, 2)[..., 0]
    bits = np.unpackbits(rows, axis=1)[:, :width * depth].reshape(rows.shape[0], width, depth)
    weights = (1 << np.arange(depth - 1, -1, -1)).astype(np.uint8)
    return (bits * weights).sum(axis=2, dtype=np.uint16).astype(np.uint8)[..., None]


def _png(data: bytes) -> np.ndarray:
    position, idat, header, palette, transparency = 8, bytearray(), None, None, None
    while position + 8 <= len(data):
        length, kind = struct.unpack_from(">I4s", data, position)
        body = data[position + 8:position + 8 + length]
        if len(body) != length:
            raise ImageError("the PNG file is truncated")
        position += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = np.frombuffer(body, dtype=np.uint8).reshape(-1, 3)
        elif kind == b"tRNS":
            transparency = bytes(body)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if header is None:
        raise ImageError("the PNG has no header")
    width, height, depth, colour, compression, filtering, interlace = header
    _check_size(width, height)
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colour)
    allowed = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
    if channels is None or depth not in allowed[colour] or compression or filtering or interlace > 1:
        raise ImageError("the PNG uses a layout outside the PNG specification")
    if colour == 3 and palette is None:
        raise ImageError("the palette PNG has no palette")
    limit = height * (((width * channels * depth + 7) // 8) + 1) + 8 * height + 64
    decompressor = zlib.decompressobj()
    raw = decompressor.decompress(bytes(idat), limit)
    if decompressor.unconsumed_tail:
        raise ImageError("the PNG image data is larger than its header says")
    raw = memoryview(raw)
    bits = channels * depth
    if not interlace:
        rows, _ = _unfilter(raw, width, height, bits, 0)
        samples = _samples(rows, width, depth, channels)
    else:
        samples = np.zeros((height, width, channels), dtype=np.uint8)
        offset = 0
        for x0, y0, dx, dy in ADAM7:
            pass_width, pass_height = (width - x0 + dx - 1) // dx, (height - y0 + dy - 1) // dy
            if pass_width <= 0 or pass_height <= 0:
                continue
            rows, used = _unfilter(raw, pass_width, pass_height, bits, offset)
            offset += used
            samples[y0::dy, x0::dx] = _samples(rows, pass_width, depth, channels)
    if colour == 3:
        indices = samples[..., 0]
        if int(indices.max(initial=0)) >= len(palette):
            raise ImageError("the PNG refers to a colour outside its palette")
        alpha = np.full(256, 255, dtype=np.uint8)
        if transparency:
            alpha[:len(transparency)] = np.frombuffer(transparency[:256], dtype=np.uint8)
        return np.dstack([palette[indices], alpha[indices]])
    scale = {1: 255, 2: 85, 4: 17}.get(depth, 1)
    samples = samples.astype(np.uint16) * scale if depth < 8 else samples
    samples = samples.astype(np.uint8)
    if colour in (0, 4):
        grey = samples[..., 0]
        alpha = samples[..., 1] if colour == 4 else np.full_like(grey, 255)
        if colour == 0 and transparency and len(transparency) >= 2:
            key = struct.unpack(">H", transparency[:2])[0]
            key = key >> 8 if depth == 16 else key * scale
            alpha = np.where(grey == key, 0, alpha).astype(np.uint8)
        return np.dstack([grey, grey, grey, alpha])
    if colour == 2:
        alpha = np.full(samples.shape[:2], 255, dtype=np.uint8)
        if transparency and len(transparency) >= 6:
            key = [v >> 8 if depth == 16 else v for v in struct.unpack(">HHH", transparency[:6])]
            alpha = np.where(np.all(samples == np.asarray(key, dtype=np.uint8), axis=2), 0, 255).astype(np.uint8)
        return np.dstack([samples, alpha])
    return samples


# --------------------------------------------------------------------- JPEG --

ZIGZAG = np.array([0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5, 12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28,
                   35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51, 58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63])


def _idct_matrix() -> np.ndarray:
    matrix = np.zeros((8, 8))
    for k in range(8):
        for n in range(8):
            matrix[k, n] = (np.sqrt(0.125) if k == 0 else 0.5) * np.cos((2 * n + 1) * k * np.pi / 16)
    return matrix


IDCT = _idct_matrix()


class _Bits:
    def __init__(self, data: bytes, position: int):
        self.data, self.position, self.value, self.count = data, position, 0, 0

    def bit(self) -> int:
        if self.count == 0:
            if self.position >= len(self.data):
                raise ImageError("the JPEG image data is truncated")
            byte = self.data[self.position]
            self.position += 1
            if byte == 0xFF:
                following = self.data[self.position] if self.position < len(self.data) else 0
                if following == 0:
                    self.position += 1
                elif 0xD0 <= following <= 0xD7:
                    raise ImageError("the JPEG has a misplaced restart marker")
                else:
                    byte = 0  # a marker ends the data; pad with zero bits
                    self.position -= 1
            self.value, self.count = byte, 8
        self.count -= 1
        return (self.value >> self.count) & 1

    def bits(self, n: int) -> int:
        value = 0
        for _ in range(n):
            value = (value << 1) | self.bit()
        return value

    def restart(self) -> None:
        self.count = 0
        while self.position + 1 < len(self.data) and not (self.data[self.position] == 0xFF and 0xD0 <= self.data[self.position + 1] <= 0xD7):
            self.position += 1
        self.position += 2


def _huffman(counts: bytes, symbols: bytes) -> dict:
    table, code, index = {}, 0, 0
    for length in range(1, 17):
        for _ in range(counts[length - 1]):
            table[(length, code)] = symbols[index]
            code += 1
            index += 1
        code <<= 1
    return table


def _decode_symbol(bits: _Bits, table: dict) -> int:
    code = 0
    for length in range(1, 17):
        code = (code << 1) | bits.bit()
        symbol = table.get((length, code))
        if symbol is not None:
            return symbol
    raise ImageError("the JPEG has a bad Huffman code")


def _extend(value: int, size: int) -> int:
    return value - (1 << size) + 1 if size and value < (1 << (size - 1)) else value


def _jpeg(data: bytes) -> np.ndarray:
    position, quant, dc_tables, ac_tables, frame, restart, adobe = 2, {}, {}, {}, None, 0, None
    while position < len(data):
        if data[position] != 0xFF:
            raise ImageError("the JPEG structure is damaged")
        marker = data[position + 1]
        position += 2
        if marker == 0xD9:
            break
        if marker in (0x01,) or 0xD0 <= marker <= 0xD7:
            continue
        length = struct.unpack_from(">H", data, position)[0]
        segment = data[position + 2:position + length]
        if len(segment) != length - 2:
            raise ImageError("the JPEG file is truncated")
        if marker in (0xC2, 0xC6, 0xCA, 0xCE):
            raise ImageError("progressive JPEGs are not supported; save the picture as a standard (baseline) JPEG or as PNG")
        if marker in (0xC3, 0xC5, 0xC7, 0xC9, 0xCB, 0xCD, 0xCF):
            raise ImageError("lossless and arithmetic-coded JPEGs are not supported; save the picture as PNG")
        if marker == 0xDB:
            offset = 0
            while offset < len(segment):
                precision, table = segment[offset] >> 4, segment[offset] & 15
                size = 128 if precision else 64
                values = np.frombuffer(segment[offset + 1:offset + 1 + size], dtype=">u2" if precision else np.uint8).astype(np.float64)
                quant[table] = np.zeros(64)
                quant[table][ZIGZAG] = values
                offset += 1 + size
        elif marker == 0xC4:
            offset = 0
            while offset < len(segment):
                kind, table = segment[offset] >> 4, segment[offset] & 15
                counts = segment[offset + 1:offset + 17]
                total = sum(counts)
                (ac_tables if kind else dc_tables)[table] = _huffman(counts, segment[offset + 17:offset + 17 + total])
                offset += 17 + total
        elif marker in (0xC0, 0xC1):
            precision, height, width, count = struct.unpack_from(">BHHB", segment, 0)
            if precision != 8:
                raise ImageError("only 8-bit JPEGs are supported")
            _check_size(width, height)
            if count not in (1, 3):
                raise ImageError("CMYK JPEGs are not supported; save the picture as RGB")
            components = []
            for index in range(count):
                identifier, sampling, table = struct.unpack_from(">BBB", segment, 6 + 3 * index)
                components.append({"id": identifier, "h": sampling >> 4, "v": sampling & 15, "q": table})
            if any(not 1 <= c["h"] <= 4 or not 1 <= c["v"] <= 4 for c in components):
                raise ImageError("the JPEG has invalid sampling factors")
            frame = {"width": width, "height": height, "components": components}
        elif marker == 0xDD:
            restart = struct.unpack_from(">H", segment, 0)[0]
        elif marker == 0xEE and segment[:5] == b"Adobe":
            adobe = segment[11] if len(segment) > 11 else None
        elif marker == 0xDA:
            if frame is None:
                raise ImageError("the JPEG has no frame header")
            count = segment[0]
            if count != len(frame["components"]):
                raise ImageError("multi-scan JPEGs are not supported; save the picture as a standard JPEG or PNG")
            by_id = {c["id"]: c for c in frame["components"]}
            for index in range(count):
                identifier, tables = segment[1 + 2 * index], segment[2 + 2 * index]
                by_id[identifier]["dc"], by_id[identifier]["ac"] = tables >> 4, tables & 15
            position = _scan(data, position + length, frame, quant, dc_tables, ac_tables, restart)
            return _jpeg_colour(frame, adobe)
        position += length
    raise ImageError("the JPEG has no image data")


def _scan(data: bytes, position: int, frame: dict, quant: dict, dc_tables: dict, ac_tables: dict, restart: int) -> int:
    components = frame["components"]
    hmax, vmax = max(c["h"] for c in components), max(c["v"] for c in components)
    mcus_x = (frame["width"] + 8 * hmax - 1) // (8 * hmax)
    mcus_y = (frame["height"] + 8 * vmax - 1) // (8 * vmax)
    for c in components:
        if c["q"] not in quant or c["dc"] not in dc_tables or c["ac"] not in ac_tables:
            raise ImageError("the JPEG refers to a missing table")
        c["coefficients"] = np.zeros((mcus_y * c["v"], mcus_x * c["h"], 64), dtype=np.float64)
        c["previous"] = 0
    bits = _Bits(data, position)
    single = len(components) == 1
    if single:  # non-interleaved: blocks cover the component, not whole MCUs
        c = components[0]
        blocks_x, blocks_y = (frame["width"] + 7) // 8, (frame["height"] + 7) // 8
        c["coefficients"] = np.zeros((blocks_y, blocks_x, 64), dtype=np.float64)
        units = [(y, x) for y in range(blocks_y) for x in range(blocks_x)]
    else:
        units = [(y, x) for y in range(mcus_y) for x in range(mcus_x)]
    for number, (uy, ux) in enumerate(units):
        if restart and number and number % restart == 0:
            bits.restart()
            for c in components:
                c["previous"] = 0
        for c in components:
            blocks = [(0, 0)] if single else [(by, bx) for by in range(c["v"]) for bx in range(c["h"])]
            dc, ac = dc_tables[c["dc"]], ac_tables[c["ac"]]
            for by, bx in blocks:
                block = np.zeros(64)
                size = _decode_symbol(bits, dc)
                c["previous"] += _extend(bits.bits(size), size)
                block[0] = c["previous"]
                k = 1
                while k < 64:
                    symbol = _decode_symbol(bits, ac)
                    run, size = symbol >> 4, symbol & 15
                    if size == 0:
                        if run != 15:
                            break
                        k += 16
                        continue
                    k += run
                    if k > 63:
                        raise ImageError("the JPEG has a damaged block")
                    block[ZIGZAG[k]] = _extend(bits.bits(size), size)
                    k += 1
                if single:
                    c["coefficients"][uy, ux] = block
                else:
                    c["coefficients"][uy * c["v"] + by, ux * c["h"] + bx] = block
        for c in components:
            c.setdefault("q_table", quant[c["q"]])
    return bits.position


def _jpeg_colour(frame: dict, adobe: int | None) -> np.ndarray:
    components = frame["components"]
    hmax, vmax = max(c["h"] for c in components), max(c["v"] for c in components)
    width, height = frame["width"], frame["height"]
    planes = []
    for c in components:
        coefficients = c["coefficients"] * c["q_table"]
        rows, columns = coefficients.shape[:2]
        blocks = coefficients.reshape(rows, columns, 8, 8)
        pixels = np.einsum("ki,rckl,lj->rcij", IDCT, blocks, IDCT) + 128.0
        plane = pixels.transpose(0, 2, 1, 3).reshape(rows * 8, columns * 8)
        plane = np.repeat(np.repeat(plane, vmax // c["v"], axis=0), hmax // c["h"], axis=1) if len(components) > 1 else plane
        planes.append(plane[:height, :width])
    if len(planes) == 1:
        grey = np.clip(np.rint(planes[0]), 0, 255).astype(np.uint8)
        return np.dstack([grey, grey, grey, np.full_like(grey, 255)])
    if adobe == 0:  # Adobe transform 0: the components are already RGB
        rgb = np.dstack(planes)
    else:
        y, cb, cr = planes[0], planes[1] - 128.0, planes[2] - 128.0
        rgb = np.dstack([y + 1.402 * cr, y - 0.344136 * cb - 0.714136 * cr, y + 1.772 * cb])
    rgb = np.clip(np.rint(rgb), 0, 255).astype(np.uint8)
    return np.dstack([rgb, np.full(rgb.shape[:2], 255, dtype=np.uint8)])


# ---------------------------------------------------------------------- BMP --

def _bmp(data: bytes) -> np.ndarray:
    offset = struct.unpack_from("<I", data, 10)[0]
    header = struct.unpack_from("<I", data, 14)[0]
    if header == 12:
        width, height, _, depth = struct.unpack_from("<hhHH", data, 18)
        compression, colours = 0, 0
    else:
        width, height, _, depth, compression = struct.unpack_from("<iiHHI", data, 18)
        colours = struct.unpack_from("<I", data, 46)[0] if header >= 40 else 0
    top_down = height < 0
    height = abs(height)
    _check_size(width, height)
    if compression not in (0, 3) or depth not in (1, 4, 8, 16, 24, 32):
        raise ImageError("compressed BMP files are not supported; save the picture as PNG")
    stride = ((width * depth + 31) // 32) * 4
    if offset + stride * height > len(data):
        raise ImageError("the BMP file is truncated")
    rows = np.frombuffer(data, dtype=np.uint8, count=stride * height, offset=offset).reshape(height, stride)
    if not top_down:
        rows = rows[::-1]
    if depth <= 8:
        entry = 3 if header == 12 else 4
        count = colours or (1 << depth)
        table = np.frombuffer(data, dtype=np.uint8, count=count * entry, offset=14 + header).reshape(count, entry)
        indices = _samples(np.ascontiguousarray(rows), width, depth, 1)[..., 0]
        if int(indices.max(initial=0)) >= count:
            raise ImageError("the BMP refers to a colour outside its palette")
        rgb = table[indices][..., 2::-1]
        return np.dstack([rgb, np.full((height, width), 255, dtype=np.uint8)])
    if depth == 24:
        bgr = rows[:, :width * 3].reshape(height, width, 3)
        return np.dstack([bgr[..., ::-1], np.full((height, width), 255, dtype=np.uint8)])
    words = rows[:, :width * depth // 8].copy().view("<u2" if depth == 16 else "<u4").reshape(height, width).astype(np.uint32)
    if compression == 3:
        masks = struct.unpack_from("<III", data, 54)
        alpha_mask = struct.unpack_from("<I", data, 66)[0] if header >= 56 else 0
    elif depth == 16:
        masks, alpha_mask = (0x7C00, 0x03E0, 0x001F), 0
    else:
        masks, alpha_mask = (0xFF0000, 0x00FF00, 0x0000FF), 0
    channels = [_mask(words, m) for m in masks]
    channels.append(_mask(words, alpha_mask) if alpha_mask else np.full((height, width), 255, dtype=np.uint8))
    if depth == 32 and not alpha_mask and header >= 108 and compression == 0:
        channels[3] = np.full((height, width), 255, dtype=np.uint8)
    return np.dstack(channels)


def _mask(words: np.ndarray, mask: int) -> np.ndarray:
    if not mask:
        return np.zeros(words.shape, dtype=np.uint8)
    shift = (mask & -mask).bit_length() - 1
    maximum = mask >> shift
    return ((((words & mask) >> shift) * 255 + maximum // 2) // maximum).astype(np.uint8)


# ---------------------------------------------------------------------- TGA --

def _tga(data: bytes) -> np.ndarray:
    id_length, map_type, kind, map_first, map_length, map_depth, _, _, width, height, depth, descriptor = struct.unpack_from("<BBBHHBHHHHBB", data, 0)
    _check_size(width, height)
    position = 18 + id_length
    palette = None
    if map_type == 1:
        entry = (map_depth + 7) // 8
        palette = _tga_pixels(data[position:position + map_length * entry], map_length, map_depth)
        position += map_length * entry
    base = kind & 7
    if base not in (1, 2, 3):
        raise ImageError("this kind of TGA file is not supported; save the picture as PNG")
    pixel_bytes = (depth + 7) // 8
    count = width * height
    if kind & 8:
        out, read = bytearray(), position
        while len(out) < count * pixel_bytes:
            header = data[read]
            read += 1
            run = (header & 0x7F) + 1
            if header & 0x80:
                out += data[read:read + pixel_bytes] * run
                read += pixel_bytes
            else:
                out += data[read:read + run * pixel_bytes]
                read += run * pixel_bytes
            if read > len(data):
                raise ImageError("the TGA file is truncated")
        raw = bytes(out[:count * pixel_bytes])
    else:
        raw = data[position:position + count * pixel_bytes]
        if len(raw) != count * pixel_bytes:
            raise ImageError("the TGA file is truncated")
    if base == 1:
        if palette is None:
            raise ImageError("the TGA has no colour map")
        indices = np.frombuffer(raw, dtype="<u2" if pixel_bytes == 2 else np.uint8).astype(np.int64) - map_first
        if indices.min(initial=0) < 0 or indices.max(initial=0) >= len(palette):
            raise ImageError("the TGA refers to a colour outside its colour map")
        pixels = palette[indices]
    elif base == 3:
        grey = np.frombuffer(raw, dtype=np.uint8)[::pixel_bytes]
        alpha = np.frombuffer(raw, dtype=np.uint8)[1::2] if pixel_bytes == 2 else np.full(count, 255, dtype=np.uint8)
        pixels = np.stack([grey, grey, grey, alpha], axis=1)
    else:
        pixels = _tga_pixels(raw, count, depth)
    image = pixels.reshape(height, width, 4)
    if not descriptor & 0x20:  # origin at the bottom
        image = image[::-1]
    if descriptor & 0x10:
        image = image[:, ::-1]
    return image


def _tga_pixels(raw: bytes, count: int, depth: int) -> np.ndarray:
    if depth in (15, 16):
        words = np.frombuffer(raw, dtype="<u2", count=count).astype(np.uint32)
        channels = [_mask(words, 0x7C00), _mask(words, 0x03E0), _mask(words, 0x001F), np.full(count, 255, dtype=np.uint8)]
        return np.stack(channels, axis=1)
    if depth not in (24, 32):
        raise ImageError(f"{depth}-bit TGA colours are not supported")
    size = depth // 8
    values = np.frombuffer(raw, dtype=np.uint8, count=count * size).reshape(count, size)
    alpha = values[:, 3] if size == 4 else np.full(count, 255, dtype=np.uint8)
    return np.stack([values[:, 2], values[:, 1], values[:, 0], alpha], axis=1)


# ------------------------------------------------------------------- fitting --

def power_of_two(value: int, limit: int) -> int:
    size = 1
    while size < value and size < limit:
        size *= 2
    if size > value and size // 2 >= 4 and value - size // 2 < size - value:
        size //= 2  # round to the nearer power of two
    return max(4, min(size, limit))


def resize(width: int, height: int, rgba: bytes, new_width: int, new_height: int) -> bytes:
    """Area-averaged (downscale) or bilinear (upscale) resampling with premultiplied alpha."""

    if (width, height) == (new_width, new_height):
        return bytes(rgba)
    image = np.frombuffer(rgba, dtype=np.uint8).reshape(height, width, 4).astype(np.float64)
    alpha = image[..., 3:4] / 255.0
    image = np.concatenate([image[..., :3] * alpha, image[..., 3:4]], axis=2)
    image = _resample_axis(image, new_height, 0)
    image = _resample_axis(image, new_width, 1)
    alpha = image[..., 3:4] / 255.0
    colour = np.where(alpha > 0, image[..., :3] / np.maximum(alpha, 1e-9), 0)
    out = np.concatenate([colour, image[..., 3:4]], axis=2)
    return np.clip(np.rint(out), 0, 255).astype(np.uint8).tobytes()


def _resample_axis(image: np.ndarray, size: int, axis: int) -> np.ndarray:
    old = image.shape[axis]
    if old == size:
        return image
    if size < old:  # area average: integrate the source over each destination cell
        edges = np.linspace(0, old, size + 1)
        cumulative = np.concatenate([np.zeros_like(np.take(image, [0], axis=axis)), np.cumsum(image, axis=axis)], axis=axis)

        def integral(x):
            low = np.clip(np.floor(x).astype(int), 0, old - 1)
            frac = (x - low).reshape([-1 if a == axis else 1 for a in range(image.ndim)])
            return np.take(cumulative, low, axis=axis) + frac * np.take(image, low, axis=axis)

        return (integral(edges[1:]) - integral(edges[:-1])) / (old / size)
    centres = (np.arange(size) + 0.5) * old / size - 0.5
    low = np.clip(np.floor(centres).astype(int), 0, old - 1)
    high = np.clip(low + 1, 0, old - 1)
    frac = np.clip(centres - low, 0, 1).reshape([-1 if a == axis else 1 for a in range(image.ndim)])
    return np.take(image, low, axis=axis) * (1 - frac) + np.take(image, high, axis=axis) * frac


def fit(width: int, height: int, rgba: bytes, limit: int = 512) -> tuple[int, int, bytes]:
    """Resize to power-of-two sides no larger than ``limit`` (what the game runtimes expect)."""

    if limit not in TEXTURE_SIZES:
        raise ImageError(f"texture size limit must be one of {TEXTURE_SIZES}")
    new_width, new_height = power_of_two(width, limit), power_of_two(height, limit)
    return new_width, new_height, resize(width, height, rgba, new_width, new_height)


def alpha_kind(rgba: bytes) -> str:
    alpha = np.frombuffer(rgba, dtype=np.uint8)[3::4]
    if alpha.min(initial=255) == 255:
        return "opaque"
    if np.all((alpha == 0) | (alpha == 255)):
        return "mask"
    return "blend"
