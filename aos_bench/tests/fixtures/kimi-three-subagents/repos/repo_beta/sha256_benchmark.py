#!/usr/bin/env python3
"""Benchmark script: compute SHA256 hashes for numbers 1 through 350000."""

import hashlib

last_hash_hex = None

for i in range(1, 350001):
    hash_obj = hashlib.sha256(str(i).encode('utf-8'))
    last_hash_hex = hash_obj.hexdigest()

print(f"Last digest prefix (16 hex chars): {last_hash_hex[:16]}")
