from __future__ import annotations

import argparse
import hashlib
import logging
import os
import subprocess
import struct
import sys
from importlib.util import find_spec
from pathlib import Path
from typing import Optional, Union

def _ensure_dependencies() -> None:
    """
    Auto-install dependencies if missing.

    Disable with env `DEXENC_NO_AUTO_INSTALL=1`.
    """
    if os.environ.get("DEXENC_NO_AUTO_INSTALL", "").strip() == "1":
        return

    required = {
        "PIL": "Pillow",
        "Crypto": "pycryptodome",
    }

    missing: list[str] = []
    for module_name, pip_name in required.items():
        if find_spec(module_name) is None:
            missing.append(pip_name)

    if not missing:
        return

    try:
        # Ensure pip exists in minimal Python installs.
        if find_spec("pip") is None:
            subprocess.check_call([sys.executable, "-m", "ensurepip", "--upgrade"])
    except Exception:
        # If ensurepip isn't available, pip install will still be attempted below.
        pass

    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *missing]
    sys.stderr.write(f"Installing missing dependencies: {', '.join(missing)}\n")
    sys.stderr.flush()
    subprocess.check_call(cmd)

    # Re-exec to guarantee fresh import state/path resolution.
    os.execv(sys.executable, [sys.executable, *sys.argv])


_ensure_dependencies()

from Crypto.Cipher import AES  # noqa: E402
from Crypto.Random import get_random_bytes  # noqa: E402
from PIL import Image  # noqa: E402

# ----------------------------
# Logging (tanpa custom UI)
# ----------------------------
# Kamu bisa custom log tanpa ngubah kode lain:
# - env `DEXENC_LOG_LEVEL`  : DEBUG/INFO/WARNING/ERROR (default: INFO)
# - env `DEXENC_LOG_FORMAT` : format logging (default: "%(levelname)s: %(message)s")
# - env `DEXENC_LOG_FILE`   : path file log (default: kosong = stderr)
LOG_LEVEL = os.environ.get("DEXENC_LOG_LEVEL", "INFO").strip() or "INFO"
LOG_FORMAT = os.environ.get("DEXENC_LOG_FORMAT", "%(levelname)s: %(message)s")
LOG_FILE = os.environ.get("DEXENC_LOG_FILE", "").strip()


def _configure_logging(
    *,
    level: Optional[str] = None,
    fmt: Optional[str] = None,
    filename: Optional[str] = None,
) -> None:
    level_name = (level or LOG_LEVEL).upper().strip()
    log_level = getattr(logging, level_name, logging.INFO)
    log_format = fmt or LOG_FORMAT
    log_file = filename if filename is not None else LOG_FILE

    handlers: list[logging.Handler] = []
    if log_file:
        Path(log_file).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    else:
        handlers.append(logging.StreamHandler())

    logging.basicConfig(level=log_level, format=log_format, handlers=handlers)


logger = logging.getLogger("dexenc")


class DexCrypter:
    _MAGIC = b"DEX1"
    _MAGIC_V2 = b"DEX2"

    def __init__(self, password: Union[str, bytes]):
        if isinstance(password, bytes):
            self._password_bytes = password
        else:
            self._password_bytes = str(password).encode("utf-8")

    def _derive_key_v1(self) -> bytes:
        return hashlib.sha256(self._password_bytes).digest()

    def _derive_key_v2(self, salt: bytes, iterations: int) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", self._password_bytes, salt, iterations, dklen=32)

    def encrypt(self, data: Union[str, bytes]) -> bytes:
        data_bytes = data.encode("utf-8") if isinstance(data, str) else data

        salt = get_random_bytes(16)
        iterations = 200_000
        key = self._derive_key_v2(salt, iterations)

        nonce = get_random_bytes(12)
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce, mac_len=16)
        ciphertext, tag = cipher.encrypt_and_digest(data_bytes)

        return (
            self._MAGIC_V2
            + bytes([len(salt), len(nonce), len(tag)])
            + struct.pack(">I", iterations)
            + salt
            + nonce
            + tag
            + ciphertext
        )

    def decrypt(self, raw_data: bytes) -> Optional[bytes]:
        try:
            if not isinstance(raw_data, (bytes, bytearray)):
                return None

            raw = bytes(raw_data)

            # V2 format
            if len(raw) >= 11 and raw[:4] == self._MAGIC_V2:
                salt_len = raw[4]
                nonce_len = raw[5]
                tag_len = raw[6]
                iterations = struct.unpack(">I", raw[7:11])[0]
                header_len = 11

                if salt_len < 8 or salt_len > 64:
                    return None
                if nonce_len < 8 or nonce_len > 32:
                    return None
                if tag_len < 12 or tag_len > 16:
                    return None
                if iterations < 10_000 or iterations > 5_000_000:
                    return None
                if len(raw) < header_len + salt_len + nonce_len + tag_len:
                    return None

                salt_start = header_len
                nonce_start = salt_start + salt_len
                tag_start = nonce_start + nonce_len
                ct_start = tag_start + tag_len

                salt = raw[salt_start:nonce_start]
                nonce = raw[nonce_start:tag_start]
                tag = raw[tag_start:ct_start]
                ciphertext = raw[ct_start:]

                key = self._derive_key_v2(salt, iterations)
                cipher = AES.new(key, AES.MODE_GCM, nonce=nonce, mac_len=tag_len)
                return cipher.decrypt_and_verify(ciphertext, tag)

            # V1 format (legacy)
            if len(raw) >= 6 and raw[:4] == self._MAGIC:
                nonce_len = raw[4]
                tag_len = raw[5]
                header_len = 6
                if nonce_len < 8 or nonce_len > 32:
                    return None
                if tag_len < 12 or tag_len > 16:
                    return None
                if len(raw) < header_len + nonce_len + tag_len:
                    return None

                nonce_start = header_len
                tag_start = nonce_start + nonce_len
                ct_start = tag_start + tag_len

                nonce = raw[nonce_start:tag_start]
                tag = raw[tag_start:ct_start]
                ciphertext = raw[ct_start:]

                key = self._derive_key_v1()
                cipher = AES.new(key, AES.MODE_GCM, nonce=nonce, mac_len=tag_len)
                return cipher.decrypt_and_verify(ciphertext, tag)

            # Fallback format lama: nonce(16) + tag(16) + ciphertext
            if len(raw) < 32:
                return None
            nonce = raw[:16]
            tag = raw[16:32]
            ciphertext = raw[32:]
            key = self._derive_key_v1()
            cipher = AES.new(key, AES.MODE_GCM, nonce=nonce, mac_len=16)
            return cipher.decrypt_and_verify(ciphertext, tag)
        except Exception:
            return None


class DexStego:
    _MAGIC_V2 = b"DSG2"
    _SALT_LEN_V2 = 16
    _MAGIC_V3 = b"DSG3"
    _SALT_LEN_V3 = 16
    _SALT_LEN = 16
    _HEADER_V3_LEN_BYTES = 4 + 16 + 2  # magic + salt + (threshold, flags)
    _FLAGS_V3 = 0x02  # bit1: channel permutation
    _DEFAULT_EDGE_THRESHOLD = 24

    _CHANNEL_PERMS: tuple[tuple[int, int, int], ...] = (
        (0, 1, 2),
        (0, 2, 1),
        (1, 0, 2),
        (1, 2, 0),
        (2, 0, 1),
        (2, 1, 0),
    )

    @staticmethod
    def bytes_to_bits(data: bytes) -> str:
        return "".join(format(b, "08b") for b in data)

    @staticmethod
    def bits_to_bytes(bit_str: str) -> bytes:
        byte_arr = bytearray()
        for i in range(0, len(bit_str), 8):
            byte_arr.append(int(bit_str[i : i + 8], 2))
        return bytes(byte_arr)

    @staticmethod
    def _password_bytes(password: Union[str, bytes]) -> bytes:
        if isinstance(password, bytes):
            return password
        return str(password).encode("utf-8")

    @classmethod
    def _derive_key_v2(cls, password: Union[str, bytes], salt: bytes) -> bytes:
        pw = cls._password_bytes(password)
        return hashlib.sha256(b"DEXSTEGO2" + pw + b"\x00" + salt).digest()

    @classmethod
    def _derive_key_v3(cls, password: Union[str, bytes], salt: bytes) -> bytes:
        pw = cls._password_bytes(password)
        return hashlib.sha256(b"DEXSTEGO3" + pw + b"\x00" + salt).digest()

    @staticmethod
    def _bitpos_to_xyc(bit_pos: int, width: int) -> tuple[int, int, int]:
        pixel_index = bit_pos // 3
        channel = bit_pos % 3  # 0=r 1=g 2=b
        y = pixel_index // width
        x = pixel_index % width
        return x, y, channel

    @staticmethod
    def _base_rgb(pixel: tuple[int, int, int]) -> tuple[int, int, int]:
        r, g, b = pixel
        return (r & ~1, g & ~1, b & ~1)

    @classmethod
    def _texture_score_byte(cls, pixels, x: int, y: int, width: int, height: int) -> int:
        r0, g0, b0 = cls._base_rgb(pixels[x, y])

        if x + 1 < width:
            r1, g1, b1 = cls._base_rgb(pixels[x + 1, y])
        else:
            r1, g1, b1 = cls._base_rgb(pixels[x - 1, y]) if x > 0 else (r0, g0, b0)

        if y + 1 < height:
            r2, g2, b2 = cls._base_rgb(pixels[x, y + 1])
        else:
            r2, g2, b2 = cls._base_rgb(pixels[x, y - 1]) if y > 0 else (r0, g0, b0)

        score = (
            abs(r0 - r1)
            + abs(g0 - g1)
            + abs(b0 - b1)
            + abs(r0 - r2)
            + abs(g0 - g2)
            + abs(b0 - b2)
        )
        return min(score // 6, 255)

    @classmethod
    def _pixel_perm(cls, key: bytes, pixel_index: int) -> tuple[int, int, int]:
        digest = hashlib.sha256(b"ORD3" + key + pixel_index.to_bytes(4, "big")).digest()
        return cls._CHANNEL_PERMS[digest[0] % len(cls._CHANNEL_PERMS)]

    @classmethod
    def _resolve_edge_threshold(cls) -> int:
        raw = os.environ.get("DEXENC_EDGE_THRESHOLD", "").strip()
        if not raw:
            return cls._DEFAULT_EDGE_THRESHOLD
        try:
            v = int(raw)
        except ValueError:
            return cls._DEFAULT_EDGE_THRESHOLD
        return max(0, min(255, v))

    def _get_bit_locations(self, pixels, width, height, threshold, key, flags, start_pos):
        for y in range(height):
            for x in range(width):
                if self._texture_score_byte(pixels, x, y, width, height) < threshold:
                    continue
                pixel_index = (y * width) + x
                perm = self._pixel_perm(key, pixel_index) if (flags & 0x02) else (0, 1, 2)
                for c in perm:
                    bit_pos = (pixel_index * 3) + c
                    if bit_pos >= start_pos:
                        yield (x, y, c)

    def hide_data(
        self,
        img_path: Union[str, os.PathLike[str]],
        data_bytes: bytes,
        output_path: Union[str, os.PathLike[str]],
        password: Optional[Union[str, bytes]] = None,
    ) -> None:
        img = Image.open(img_path).convert("RGB")
        pixels = img.load()

        width, height = img.size
        capacity_bits = width * height * 3

        if password:
            salt = os.urandom(self._SALT_LEN_V3)
            threshold = self._resolve_edge_threshold()
            header = self._MAGIC_V3 + salt + bytes([threshold, self._FLAGS_V3])
            header_bits = self.bytes_to_bits(header)
            header_end_bitpos = len(header_bits)

            key = self._derive_key_v3(password, salt)
            length_mask = int.from_bytes(hashlib.sha256(b"LEN3" + key).digest()[:4], "big")
            length_u32 = len(data_bytes) & 0xFFFFFFFF
            length_obf = (length_u32 ^ length_mask) & 0xFFFFFFFF
            len_bits = format(length_obf, "032b")

            payload_bits = len_bits + self.bytes_to_bits(data_bytes)
            total_bits_needed = len(header_bits) + len(payload_bits)
            if total_bits_needed > capacity_bits:
                raise ValueError(
                    f"Payload terlalu besar untuk gambar. Butuh {total_bits_needed} bit, kapasitas {capacity_bits} bit."
                )

            # 1) Write header sequentially.
            for i, bit in enumerate(header_bits):
                x, y, c = self._bitpos_to_xyc(i, width)
                channels = list(pixels[x, y])
                channels[c] = (channels[c] & ~1) | int(bit)
                pixels[x, y] = tuple(channels)

            available_bits = 0
            for y in range(height):
                for x in range(width):
                    if self._texture_score_byte(pixels, x, y, width, height) < threshold:
                        continue
                    pixel_index = (y * width) + x
                    for c in (0, 1, 2):
                        bit_pos = (pixel_index * 3) + c
                        if bit_pos >= header_end_bitpos:
                            available_bits += 1

            if len(payload_bits) > available_bits:
                raise ValueError(
                    "Area bertekstur tidak cukup untuk payload. "
                    f"Butuh {len(payload_bits)} bit, tersedia {available_bits} bit. "
                    "Coba pakai gambar yang lebih noisy/bertekstur, perbesar resolusi, atau turunkan threshold "
                    "(set env DEXENC_EDGE_THRESHOLD lebih kecil)."
                )

            # 2) Embed payload bits (scan order on textured pixels)
            bit_gen = self._get_bit_locations(pixels, width, height, threshold, key, self._FLAGS_V3, header_end_bitpos)
            bit_index = 0
            for x, y, c in bit_gen:
                if bit_index >= len(payload_bits):
                    break
                channels = list(pixels[x, y])
                channels[c] = (channels[c] & ~1) | int(payload_bits[bit_index])
                pixels[x, y] = tuple(channels)
                bit_index += 1

            if bit_index < len(payload_bits):
                raise ValueError("Kapasitas gambar tidak cukup (area tekstur terlalu sedikit).")
        else:
            bit_data = format(len(data_bytes), "032b") + self.bytes_to_bits(data_bytes)
            if len(bit_data) > capacity_bits:
                raise ValueError(
                    f"Payload terlalu besar untuk gambar. Butuh {len(bit_data)} bit, kapasitas {capacity_bits} bit."
                )

            idx = 0
            for y in range(height):
                for x in range(width):
                    r, g, b = pixels[x, y]
                    channels = [r, g, b]
                    for i in range(3):
                        if idx < len(bit_data):
                            channels[i] = (channels[i] & ~1) | int(bit_data[idx])
                            idx += 1
                    pixels[x, y] = tuple(channels)
                    if idx >= len(bit_data):
                        break
                if idx >= len(bit_data):
                    break

        out_path = Path(output_path)
        if out_path.parent and not out_path.parent.exists():
            out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(output_path, "PNG")

    def extract_data(
        self,
        img_path: Union[str, os.PathLike[str]],
        password: Optional[Union[str, bytes]] = None,
    ) -> bytes:
        img = Image.open(img_path).convert("RGB")
        pixels = img.load()
        width, height = img.size
        capacity_bits = width * height * 3

        if password:
            header_len_bits = self._HEADER_V3_LEN_BYTES * 8
            if capacity_bits >= header_len_bits:
                header_bits = []
                for i in range(header_len_bits):
                    x, y, c = self._bitpos_to_xyc(i, width)
                    header_bits.append("1" if (pixels[x, y][c] & 1) else "0")
                header = self.bits_to_bytes("".join(header_bits))
                magic = header[:4]

                if magic == self._MAGIC_V3 and len(header) >= self._HEADER_V3_LEN_BYTES:
                    salt = header[4:20]
                    threshold = header[20]
                    flags = header[21]
                    key = self._derive_key_v3(password, salt)
                    length_mask = int.from_bytes(hashlib.sha256(b"LEN3" + key).digest()[:4], "big")

                    bit_gen = self._get_bit_locations(pixels, width, height, threshold, key, flags, header_len_bits)
                    len_bits: list[str] = []
                    data_bits: list[str] = []
                    data_len: Optional[int] = None

                    for x, y, c in bit_gen:
                        bit = "1" if (pixels[x, y][c] & 1) else "0"
                        if data_len is None:
                            len_bits.append(bit)
                            if len(len_bits) == 32:
                                data_len = (int("".join(len_bits), 2) ^ length_mask) & 0xFFFFFFFF
                        else:
                            data_bits.append(bit)
                            if len(data_bits) == data_len * 8:
                                return self.bits_to_bytes("".join(data_bits))

                    raise ValueError("Data tidak ditemukan atau threshold salah.")

                # Legacy keyed-scatter V2 (DSG2)
                header_len_bits_v2 = (4 + self._SALT_LEN) * 8
                if capacity_bits >= header_len_bits_v2:
                    header_bits_v2 = []
                    for i in range(header_len_bits_v2):
                        x, y, c = self._bitpos_to_xyc(i, width)
                        header_bits_v2.append("1" if (pixels[x, y][c] & 1) else "0")
                    header_v2 = self.bits_to_bytes("".join(header_bits_v2))
                    magic_v2 = header_v2[:4]
                    if magic_v2 == self._MAGIC_V2:
                        salt = header_v2[4:]
                        key = self._derive_key_v2(password, salt)
                        length_mask = int.from_bytes(hashlib.sha256(b"LEN" + key).digest()[:4], "big")

                        import random

                        shuffle_seed = int.from_bytes(hashlib.sha256(b"SHUF" + key).digest(), "big")
                        rng = random.Random(shuffle_seed)
                        positions = list(range(header_len_bits_v2, capacity_bits))
                        rng.shuffle(positions)

                        if len(positions) < 32:
                            raise ValueError("Header stego invalid atau gambar tidak berisi payload yang valid.")
                        len_bits = []
                        for pos in positions[:32]:
                            x, y, c = self._bitpos_to_xyc(pos, width)
                            len_bits.append("1" if (pixels[x, y][c] & 1) else "0")

                        length_obf = int("".join(len_bits), 2)
                        data_len = (length_obf ^ length_mask) & 0xFFFFFFFF

                        total_bits_needed = 32 + (data_len * 8)
                        if total_bits_needed > len(positions):
                            raise ValueError("Data length marker invalid atau gambar tidak berisi payload yang valid.")

                        data_bits = []
                        for pos in positions[32 : 32 + (data_len * 8)]:
                            x, y, c = self._bitpos_to_xyc(pos, width)
                            data_bits.append("1" if (pixels[x, y][c] & 1) else "0")

                        return self.bits_to_bytes("".join(data_bits))

        # Legacy sequential (no password)
        all_bits = ""
        for y in range(height):
            for x in range(width):
                r, g, b = pixels[x, y]
                all_bits += f"{r & 1}{g & 1}{b & 1}"
                if len(all_bits) >= 32:
                    break
            if len(all_bits) >= 32:
                break

        data_len = int(all_bits[:32], 2)
        total_bits_needed = 32 + (data_len * 8)
        if total_bits_needed > capacity_bits:
            raise ValueError("Data length marker invalid atau gambar tidak berisi payload yang valid.")

        all_bits = ""
        idx = 0
        for y in range(height):
            for x in range(width):
                r, g, b = pixels[x, y]
                for chan in (r, g, b):
                    if idx < total_bits_needed:
                        all_bits += str(chan & 1)
                        idx += 1
                if idx >= total_bits_needed:
                    break
            if idx >= total_bits_needed:
                break

        return self.bits_to_bytes(all_bits[32:])


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="DEX-ENC: Image steganography + AES-256-GCM (single-file)")
    parser.add_argument("action", choices=["hide", "extract", "encrypt", "decrypt"], help="Aksi yang dilakukan")

    parser.add_argument("-i", "--input", required=True, help="File input utama (gambar atau payload)")
    parser.add_argument("-p", "--payload", help="File rahasia yang akan disisipkan (hanya untuk mode 'hide')")
    parser.add_argument(
        "-o",
        "--output",
        help="Path file output hasil proses. Jika kosong pada extract/decrypt, akan print ke terminal.",
    )
    parser.add_argument("-key", "--password", required=True, help="Password enkripsi")

    parser.add_argument("--quiet", action="store_true", help="Minimalkan output (log level jadi WARNING)")
    parser.add_argument("--log-level", help="Override log level (contoh: DEBUG/INFO/WARNING/ERROR)")
    parser.add_argument("--log-format", help="Override log format logging")
    parser.add_argument("--log-file", help="Tulis log ke file (default: stderr)")

    args = parser.parse_args(argv)

    _configure_logging(
        level="WARNING" if args.quiet else args.log_level,
        fmt=args.log_format,
        filename=args.log_file,
    )

    try:
        crypter = DexCrypter(args.password)
        stego = DexStego()

        input_path = Path(args.input)
        if not input_path.exists():
            logger.error("File input tidak ditemukan: %s", args.input)
            return 2

        if args.action == "hide":
            if not args.payload or not args.output:
                logger.error("Mode hide butuh: -i (gambar), -p (file rahasia), -o (hasil)")
                return 2

            payload_path = Path(args.payload)
            if not payload_path.exists():
                logger.error("Payload tidak ditemukan: %s", args.payload)
                return 2

            logger.info("Menyisipkan %s ke %s...", payload_path.name, input_path.name)
            encrypted_data = crypter.encrypt(payload_path.read_bytes())
            stego.hide_data(str(input_path), encrypted_data, args.output, password=args.password)
            logger.info("Selesai. Gambar disimpan di: %s", args.output)
            return 0

        if args.action == "extract":
            logger.info("Mengekstrak data dari gambar...")
            raw_encrypted = stego.extract_data(str(input_path), password=args.password)
            decrypted = crypter.decrypt(raw_encrypted)
            if decrypted is None:
                logger.error("Gagal dekripsi. Password salah atau gambar rusak.")
                return 3

            if args.output:
                out_path = Path(args.output)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(decrypted)
                logger.info("Data disimpan ke: %s", args.output)
            else:
                sys.stdout.write(decrypted.decode("utf-8", errors="replace"))
                if not sys.stdout.isatty():
                    sys.stdout.write("\n")
            return 0

        if args.action == "encrypt":
            out_path = Path(args.output or (input_path.stem + ".dexenc"))
            if out_path.suffix != ".dexenc":
                out_path = out_path.with_suffix(".dexenc")

            logger.info("Mengenkripsi %s...", input_path.name)
            encrypted = crypter.encrypt(input_path.read_bytes())
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(encrypted)
            logger.info("File terenkripsi: %s", str(out_path))
            return 0

        if args.action == "decrypt":
            logger.info("Mendekripsi %s...", input_path.name)
            decrypted = crypter.decrypt(input_path.read_bytes())
            if decrypted is None:
                logger.error("Gagal dekripsi. Password salah.")
                return 3

            if args.output:
                out_path = Path(args.output)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(decrypted)
                logger.info("File didekripsi ke: %s", args.output)
            else:
                sys.stdout.write(decrypted.decode("utf-8", errors="replace"))
                if not sys.stdout.isatty():
                    sys.stdout.write("\n")
            return 0

        logger.error("Aksi tidak dikenal: %s", args.action)
        return 2
    except Exception:
        logger.exception("Terjadi kesalahan.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
