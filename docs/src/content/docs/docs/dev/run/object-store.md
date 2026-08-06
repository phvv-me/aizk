---
title: "Object storage and compression"
description: "How stored artifacts are compressed, which formats are accepted, and how to compact a store that predates the current policy."
---

Aizk keeps artifact bytes outside PostgreSQL, in an S3-compatible object store, and keeps only
metadata in the `blob` table. This page covers what gets written there, why almost all of it is
compressed, which formats are refused at the door, and how to bring an older store up to the
current policy.

## Everything readable is compressed, losslessly

`ByteStore.put` in `src/aizk/storage.py` compresses with Zstandard before the bytes reach the
backend, and `ByteStore.get` restores them on the way back. Nothing above the byte store ever
sees a compressed frame. Docling, the image embedder, and the MCP artifact resource all receive
the original bytes.

Compression is adaptive rather than blind. An object is only stored compressed when Zstandard
saves at least `AIZK_OBJECT_STORE_COMPRESSION_MIN_SAVINGS` of it, which keeps already-compressed
formats such as JPEG and EPUB from paying a container for nothing. Each blob records which way it
went in `encoding`, so every read knows what it is holding.

```text
  put(data)                                    get(key, encoding=...)
    ├─ content_hash = sha256 over the ORIGINAL   ├─ fetch stored bytes
    ├─ zstd, keep only if it wins                ├─ decode by recorded encoding
    └─ write under a fresh random key            └─ verify size and hash of the ORIGINAL
```

The content hash always describes the original bytes, never the compressed container. That keeps
the hash stable when an object is recompressed at a different level, lets two uploads of the same
file be recognised as the same content, and makes `ArtifactIntegrity` meaningful. Its check
restores the object and compares the result against the recorded hash, so a pass proves both that
the container is intact and that it still decodes to the file that was accepted.

The S3 backend also sets `checksum_algorithm="SHA256"`, a separate transport guarantee covering
the bytes as uploaded, meaning the compressed form. It protects the write in flight and says
nothing about the original, which is why aizk keeps its own hash.

### What it saves in practice

Here is a copy of the live deployment, 327 objects and 459.8 MB of originals.

| Media type | Objects | Original | Stored | Saved |
| --- | ---: | ---: | ---: | ---: |
| `application/pdf` (compressed) | 107 | 333 MB | 178 MB | 46.5% |
| `application/pdf` (kept verbatim) | 11 | 53 MB | 53 MB | 0% |
| `text/html` | 176 | 35 MB | 9.7 MB | 72.5% |
| `text/markdown` | 22 | 6.0 MB | 1.6 MB | 73.0% |
| `text/plain` | 7 | 202 kB | 83 kB | 58.8% |
| `application/epub+zip` | 4 | 11 MB | 10.4 MB | 5.5% |
| **Whole store** | **327** | **459.8 MB** | **265.3 MB** | **42.3%** |

### Reads get faster, not slower

Zstandard decompresses at roughly 1.7 GB/s here, and that rate barely moves with the compression
level. Reading a compressed object is therefore faster than reading a verbatim one whenever the
link to the object store is slower than `savings x decompression rate`. At the 42.3% the live
store actually achieves, that break-even sits near 720 MB/s. Every real path into the object
store, whether the Compose network, a LAN, or a tunnel, is far below that, so compression is a
read win as well as a storage win.

### Choosing a level

`AIZK_OBJECT_STORE_COMPRESSION_LEVEL` defaults to 9, measured as the knee on a 165 MB corpus of
real documents.

| Level | Stored | Saved | Compression | Decompression |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 128.5 MB | 22.1% | 1017 MB/s | 1707 MB/s |
| 3 | 123.1 MB | 25.4% | 599 MB/s | 1691 MB/s |
| 9 | 118.7 MB | 28.0% | 197 MB/s | 1746 MB/s |
| 12 | 118.5 MB | 28.2% | 95 MB/s | 1749 MB/s |
| 19 | 115.9 MB | 29.7% | 6.7 MB/s | 1447 MB/s |

Level 9 takes most of what is available while still compressing a full-size upload in well under a
second, on a path that already runs off the request thread. Past 12 the write cost rises by two
orders of magnitude to buy about one more point, and decompression never improves.

### Turning it off

`AIZK_OBJECT_STORE_COMPRESSION_ENABLED=0` stops new objects from being compressed. It is safe to
flip at any time in either direction, because reads are driven by each blob's recorded `encoding`
rather than by the current setting, so objects written under the old policy keep working
untouched. Objects written while it is off record no level, which means a later compaction pass
will pick them up once compression is back on.

## Compacting a store that predates the policy

Raising the level, or turning compression on for the first time, only changes how *new* objects
are written. Everything already stored keeps its old layout. `blob.encoding_level` records the
level each object's layout was last evaluated under, and null means the policy is unknown, which
is what every object created before this column existed reports.

```sh
aizk admin storage compact --limit 200
```

Run it until `examined` comes back zero. Each pass takes the largest objects still below the
configured level, so the reclaimable bytes come back first and a store can be compacted in as
many short passes as you like.

Per object the pass restores the bytes, verifies them against the content hash, writes a
replacement under a fresh key, repoints PostgreSQL, and only then deletes the old key. An
interruption leaves an unreferenced object behind rather than a blob row pointing at bytes that
are gone. When the replacement is not smaller the object stays where it is and only its level is
stamped.

Because a rewrite requires verifying the original, the pass doubles as an integrity check and
records itself as one. A failure is reported and left unstamped, so the object stays a candidate.
Compaction refuses to run while compression is disabled, since there would be no policy to
compact toward.

## Formats are refused at the door

Aizk stores information, not files. A format it cannot open is one it cannot convert, index, or
ever recall, so it is refused while the caller still holds it instead of becoming an opaque blob
that only costs storage.

`FormatPolicy` in `src/aizk/artifacts/formats.py` decides from the bytes. A declared media type
is a claim, the leading signature is the evidence, and both have to agree. A file whose
declaration and content disagree is refused rather than quietly relabelled, because only the
caller can settle which of the two is wrong.

The check runs twice. `UploadRequest` validates the declared type when a capability is minted, so
an unreadable format costs one round trip rather than a whole upload, and `ArtifactIntake.accept`
re-checks the delivered bytes before anything is scanned or stored.

Accepted today are PDF, the image formats PNG, JPEG, GIF, TIFF, BMP and WebP, WAV audio, EPUB and
the OOXML documents, and the textual family, meaning plain text, Markdown, HTML, XHTML, XML,
AsciiDoc, JSON, CSV and TSV. Anything else is refused with a message naming what was declared and
what the bytes turned out to be.

Video is refused today, deliberately. Aizk has no video reader, so accepting it would store an
opaque object nobody could search. Accepting video means first giving it a reader.
