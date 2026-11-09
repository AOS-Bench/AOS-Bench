#!/usr/bin/env python3
MOD = 10**9 + 7
result = 0

for i in range(4000):
    for j in range(5000):
        result = (result + i * j) % MOD

print(result)
