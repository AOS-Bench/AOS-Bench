# CPU-intensive nested modular accumulation loop
# Triple nested loop with approximately 5-10 million iterations

MODULUS = 1000000007
result = 0

N = 170  # 170^3 = 4,913,000 iterations

for i in range(N):
    for j in range(N):
        for k in range(N):
            # Modular arithmetic multiplication and accumulation
            result = (result + (i * j * k) % MODULUS) % MODULUS

print(result)
