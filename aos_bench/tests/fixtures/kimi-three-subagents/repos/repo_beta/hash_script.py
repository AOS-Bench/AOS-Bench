import hashlib

last_hash = None
for i in range(1, 5000001):
    last_hash = hashlib.sha256(str(i).encode()).hexdigest()

print(last_hash[:8])
