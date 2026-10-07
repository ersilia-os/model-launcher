#!/usr/bin/env python3
"""Split a failed chunk into pieces, and merge the pieces' results back.

Used by bisect.sh, the end-of-run rescue for chunks that keep failing because a
few molecules break the model. Standard library only: it runs on the head node.

  ranges <start> <end> [n]          up to n (default 10) sub-ranges, "S E" per line
  split  <chunk> <workdir> <S> <E>  write <workdir>/piece_<S>_<E>.csv (header + rows S..E)
  merge  <chunk> <workdir> <out> <bad>

Rows are 0-based and ranges inclusive, counted after the chunk's header line.
merge expects, in <workdir>, a result_<S>_<E>.csv for every range that succeeded
and an empty bad_<S> file for every single molecule that failed on its own. It
writes the chunk's result in the original order, a bad molecule as a row with the
results' own columns: `key` = md5 of the SMILES (as ersilia keys a row),
`input`/`smiles` = the SMILES, everything else empty. <bad> lists those molecules.

Exit codes for merge: 0 written; 1 the pieces do not cover the chunk exactly
(missing, overlapping or wrong-length results); 2 no piece succeeded, so there
are no result columns to copy.
"""

from __future__ import annotations

import csv
import hashlib
import math
import os
import re
import sys

# Every file written here uses "\n", like the chunks and every other result:
# csv's default "\r\n" would put a stray \r on each line a model reads.
RESULT = re.compile(r"^result_(\d+)_(\d+)\.csv$")
BAD = re.compile(r"^bad_(\d+)$")


def read_chunk(path):
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    return rows[0], rows[1:]


def ranges(start, end, n=10):
    size = end - start + 1
    step = max(1, math.ceil(size / n))
    out = []
    s = start
    while s <= end:
        out.append((s, min(end, s + step - 1)))
        s += step
    return out


def split(chunk, workdir, start, end):
    header, rows = read_chunk(chunk)
    os.makedirs(workdir, exist_ok=True)
    path = os.path.join(workdir, f"piece_{start}_{end}.csv")
    with open(path, "w", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows[start : end + 1])
    print(path)


def merge(chunk, workdir, out, bad_out):
    _, rows = read_chunk(chunk)
    n = len(rows)
    filled = [None] * n
    header = None
    bad = []
    for name in sorted(os.listdir(workdir)):
        match = RESULT.match(name)
        if match:
            start, end = int(match.group(1)), int(match.group(2))
            with open(os.path.join(workdir, name), newline="") as f:
                got = list(csv.reader(f))
            if len(got) - 1 != end - start + 1:
                print(
                    f"merge: {name} has {len(got) - 1} rows, expected {end - start + 1}",
                    file=sys.stderr,
                )
                return 1
            if header is None:
                header = got[0]
            elif got[0] != header:
                print(f"merge: {name} has a different header", file=sys.stderr)
                return 1
            for i, row in enumerate(got[1:]):
                if not 0 <= start + i < n or filled[start + i] is not None:
                    print(
                        f"merge: {name} overlaps or overruns the chunk", file=sys.stderr
                    )
                    return 1
                filled[start + i] = row
            continue
        match = BAD.match(name)
        if match:
            bad.append(int(match.group(1)))
    if header is None:
        print(
            "merge: no piece succeeded; nothing to take the result columns from",
            file=sys.stderr,
        )
        return 2
    for index in sorted(bad):
        if not 0 <= index < n or filled[index] is not None:
            print(f"merge: bad row {index} overlaps a result", file=sys.stderr)
            return 1
        smiles = rows[index][0] if rows[index] else ""
        key = hashlib.md5(smiles.encode("utf-8")).hexdigest()
        filled[index] = [
            key if col == "key" else smiles if col in ("input", "smiles") else ""
            for col in header
        ]
    missing = [i for i, row in enumerate(filled) if row is None]
    if missing:
        print(
            f"merge: {len(missing)} rows not covered (first: {missing[0]})",
            file=sys.stderr,
        )
        return 1
    tmp = out + ".tmp"
    with open(tmp, "w", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(filled)
    os.replace(tmp, out)
    with open(bad_out, "w", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["row", "key", "smiles"])
        for index in sorted(bad):
            smiles = rows[index][0] if rows[index] else ""
            writer.writerow(
                [index, hashlib.md5(smiles.encode("utf-8")).hexdigest(), smiles]
            )
    print(len(bad))
    return 0


def main(argv):
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    verb, args = argv[1], argv[2:]
    if verb == "ranges" and len(args) in (2, 3):
        n = int(args[2]) if len(args) == 3 else 10
        for s, e in ranges(int(args[0]), int(args[1]), n):
            print(s, e)
        return 0
    if verb == "count" and len(args) == 1:
        print(len(read_chunk(args[0])[1]))
        return 0
    if verb == "split" and len(args) == 4:
        split(args[0], args[1], int(args[2]), int(args[3]))
        return 0
    if verb == "merge" and len(args) == 4:
        return merge(*args)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
