"""Patch the pinned Harvester 1.8.2 ifname generator without rebuilding initrd.

The kernel accepts concatenated compressed cpio archives. A last overlay
replaces only this hook; original microcode, modules and boot files stay intact.
"""
import gzip
import hashlib
import io
import lzma
from pathlib import Path
import shutil
import sys

HOOK = 'var/lib/dracut/hooks/pre-udev/50-ifname-genrules.sh'
EXPECTED = '1530e28e30c0a6b6f2502fa87d807e2226b9a3303467a389045638e4d5081d9e'


def entries(stream):
    while True:
        header = stream.read(110)
        if not header:
            return
        if len(header) != 110 or header[:6] not in (b'070701', b'070702'):
            raise ValueError('unexpected initrd cpio format')
        size, namesize = int(header[54:62], 16), int(header[94:102], 16)
        if not 0 < namesize <= 4096 or size > 1024 * 1024 * 1024:
            raise ValueError('invalid cpio entry size')
        name = stream.read(namesize)[:-1].decode('utf-8')
        stream.read((-(110 + namesize)) % 4)
        if name == 'TRAILER!!!':
            return
        if name == HOOK:
            if size > 65536:
                raise ValueError('unexpected ifname hook size')
            yield name, stream.read(size)
        else:
            remaining = size
            while remaining:
                block = stream.read(min(remaining, 1024 * 1024))
                if not block:
                    raise ValueError('truncated initrd')
                remaining -= len(block)
        stream.read((-size) % 4)


def original_hook(path):
    with open(path, 'rb') as stream:
        list(entries(stream))  # Original early microcode cpio.
        while True:
            offset = stream.tell()
            byte = stream.read(1)
            if byte != b'\0':
                if not byte:
                    raise ValueError('missing main initrd')
                stream.seek(offset)
                break
        if stream.read(6) != b'\xfd7zXZ\0':
            raise ValueError('unsupported main initrd compression')
        stream.seek(offset)
        with lzma.LZMAFile(stream) as archive:
            for name, content in entries(archive):
                if name == HOOK:
                    return content
    raise ValueError('missing original ifname hook')


def patched_hook(content):
    if hashlib.sha256(content).hexdigest() != EXPECTED:
        raise ValueError('unrecognized ifname hook; refusing an unverified patch')
    lines = content.decode().splitlines(True)
    matches = [i for i, line in enumerate(lines)
               if line.lstrip().startswith("printf 'SUBSYSTEM")]
    if len(matches) != 1:
        raise ValueError('ambiguous ifname rule generator')
    # TEST prevents fallback matching of a bond slave's shared current MAC.
    prefix = 'SUBSYSTEM=="net", ACTION=="add", DRIVERS=="?*", '
    suffix = 'ATTR{type}=="1", NAME="%s"'
    rules = [prefix + 'TEST!="bonding_slave", ATTR{address}=="%s", ' + suffix,
             prefix + 'TEST=="bonding_slave", ATTR{bonding_slave/perm_hwaddr}=="%s", ' + suffix]
    lines[matches[0]] = ''.join(
        "        printf '%s\\n' \"$ifname_mac\" \"$ifname_if\"\n" % rule
        for rule in rules)
    return ''.join(lines).encode()


def cpio_entry(name, content, mode=0o100755):
    name = name.encode() + b'\0'
    values = [1, mode, 0, 0, 1, 0, len(content), 0, 0, 0, 0, len(name), 0]
    result = b'070701' + ''.join('%08x' % v for v in values).encode() + name
    result += b'\0' * ((-len(result)) % 4)
    result += content + b'\0' * ((-len(content)) % 4)
    return result


def patch_initrd(source, target):
    source, target = Path(source), Path(target)
    if source.resolve() == target.resolve():
        raise ValueError('source initrd must remain unchanged')
    hook = patched_hook(original_hook(source))
    overlay = cpio_entry(HOOK, hook) + cpio_entry('TRAILER!!!', b'', 0)
    with source.open('rb') as src, target.open('xb') as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
        dst.write(b'\0' * ((-dst.tell()) % 4))
        compressed = io.BytesIO()
        with gzip.GzipFile(fileobj=compressed, mode='wb', mtime=0) as archive:
            archive.write(overlay)
        dst.write(compressed.getvalue())


if __name__ == '__main__':
    patch_initrd(sys.argv[1], sys.argv[2])
