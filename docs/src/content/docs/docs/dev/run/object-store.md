---
title: "Object storage and compression"
description: "How stored artifacts are compressed, which formats are accepted, and how to compact a store that predates the current policy."
---

AIZK keeps artifact bytes outside the SQL database in an S3-compatible object store. The `blob`
table holds their metadata. crAIZK uses Amazon S3, while the self-hosted profile uses SeaweedFS.
This page explains compression, accepted formats, and safe migration of older objects.

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

The content hash describes the original bytes, not the compressed container. It stays stable
across compression levels and identifies duplicate uploads. `ArtifactIntegrity`
restores the object and compares the result against the recorded hash, so a pass proves both that
the container is intact and that it still decodes to the file that was accepted.

The S3 backend also sets `checksum_algorithm="SHA256"`, a separate transport guarantee covering
the bytes as uploaded, meaning the compressed form. It protects the write in flight but does not
verify the restored original. AIZK's content hash performs that check.

### Expected effect

Plain text, Markdown, and HTML usually shrink substantially. JPEG, EPUB, and other compressed
formats often do not. The savings threshold handles that difference per object and keeps the
original representation whenever compression adds no useful value. Smaller objects also require
fewer bytes from remote storage, though the exact effect depends on the corpus and network.

### Choosing a level

`AIZK_OBJECT_STORE_COMPRESSION_LEVEL` defaults to 9 as a balance between write cost and stored
size. Compression runs outside the request thread. A deployment may choose another level after
measuring its own document mix and hardware.

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

The pass restores each object and verifies its content hash. It then writes a replacement under a
fresh key. One conditional transaction repoints the blob and records the old
key for deferred retirement. This prevents a reader that loaded the old pointer just before the
swap from seeing missing bytes. The grace defaults to one hour and must exceed the lifetime of
every signed download URL. Nightly cleanup leases due retirements in bounded batches before it
deletes them. A failed delete keeps its durable row and becomes eligible again after the lease.
When the replacement is not smaller the object stays where it is and only its level is stamped.

The compaction report separates active-layout savings from bytes awaiting retirement. A compare
and swap loss is reported as a conflict rather than a successful unchanged evaluation. The
winning worker keeps its candidate and every losing worker deletes only the fresh candidate it
created.

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

Accepted formats include PDF, PNG, JPEG, GIF, TIFF, BMP, WebP, WAV, EPUB, and OOXML documents. The
text family covers plain text, Markdown, HTML, XHTML, XML, AsciiDoc, JSON, CSV, and TSV. Anything
else is refused with a message that identifies the declared type and detected content.

Video is not accepted because AIZK has no video reader. Storing it would create an opaque object
that recall cannot search.
