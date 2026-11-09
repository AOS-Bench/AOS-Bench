#!/usr/bin/env bash
# Run inside the sandbox container to verify resource limits are in effect.
# Exit 0 = all checks passed, non-zero = at least one check failed.

set -euo pipefail
PASS=0; FAIL=0

ok()   { echo "[OK]  $*"; ((PASS++)); }
fail() { echo "[FAIL] $*"; ((FAIL++)); }
info() { echo "[INFO] $*"; }

# ── helpers ────────────────────────────────────────────────────────────────

cgroup_v2() { [ -f /sys/fs/cgroup/cgroup.controllers ]; }

read_mem_limit() {
    if cgroup_v2; then
        cat /sys/fs/cgroup/memory.max 2>/dev/null || echo "max"
    else
        cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null || echo "0"
    fi
}

read_cpu_quota() {
    if cgroup_v2; then
        cat /sys/fs/cgroup/cpu.max 2>/dev/null || echo "max"
    else
        quota=$(cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us 2>/dev/null || echo "-1")
        period=$(cat /sys/fs/cgroup/cpu/cpu.cfs_period_us 2>/dev/null || echo "100000")
        echo "${quota}/${period}"
    fi
}

read_cpuset() {
    if cgroup_v2; then
        cat /sys/fs/cgroup/cpuset.cpus.effective 2>/dev/null || echo "unknown"
    else
        cat /sys/fs/cgroup/cpuset/cpuset.cpus 2>/dev/null || echo "unknown"
    fi
}

read_swappiness() {
    if cgroup_v2; then
        cat /sys/fs/cgroup/memory.swap.max 2>/dev/null || echo "unknown"
    else
        cat /sys/fs/cgroup/memory/memory.swappiness 2>/dev/null || echo "unknown"
    fi
}

read_pids_limit() {
    if cgroup_v2; then
        cat /sys/fs/cgroup/pids.max 2>/dev/null || echo "unknown"
    else
        cat /sys/fs/cgroup/pids/pids.max 2>/dev/null || echo "unknown"
    fi
}

bytes_to_gb() { echo "scale=1; $1 / 1073741824" | bc 2>/dev/null || echo "?"; }

# ── checks ─────────────────────────────────────────────────────────────────

echo "═══════════════════════════════════════"
echo " AgentOS-Bench Sandbox Resource Check"
echo "═══════════════════════════════════════"
if cgroup_v2; then info "cgroup version: v2"; else info "cgroup version: v1"; fi
echo ""

# 1. Memory limit
MEM_LIMIT=$(read_mem_limit)
if [ "$MEM_LIMIT" = "max" ] || [ "$MEM_LIMIT" = "0" ]; then
    fail "Memory limit: UNLIMITED (expected hard limit, set HARBOR_MEMORY_MB)"
else
    MEM_GB=$(bytes_to_gb "$MEM_LIMIT")
    ok "Memory limit: ${MEM_GB}G (${MEM_LIMIT} bytes)"
fi

# 2. CPU quota
CPU_INFO=$(read_cpu_quota)
if [[ "$CPU_INFO" == "max"* ]] || [[ "$CPU_INFO" == "-1/"* ]]; then
    fail "CPU quota: UNLIMITED (expected quota, set HARBOR_CPUS)"
else
    ok "CPU quota: ${CPU_INFO}"
fi

# 3. cpuset (optional)
CPUSET=$(read_cpuset)
if [ "$CPUSET" = "unknown" ]; then
    info "cpuset: not readable"
elif [ "$CPUSET" = "0-$(nproc --all | xargs -I{} expr {} - 1)" ] 2>/dev/null; then
    info "cpuset: all cores (HARBOR_CPUSET not set)"
else
    ok "cpuset: ${CPUSET}"
fi

# 4. Swappiness
SWAPPINESS=$(read_swappiness)
if [ "$SWAPPINESS" = "0" ]; then
    ok "mem_swappiness: 0 (swap disabled)"
elif [ "$SWAPPINESS" = "unknown" ]; then
    info "mem_swappiness: not readable"
else
    fail "mem_swappiness: ${SWAPPINESS} (expected 0, set HARBOR_SANDBOX=1)"
fi

# 5. PID limit
PIDS=$(read_pids_limit)
if [ "$PIDS" = "max" ] || [ "$PIDS" = "unknown" ]; then
    info "pids_limit: ${PIDS} (HARBOR_SANDBOX=1 not set or not supported)"
else
    ok "pids_limit: ${PIDS}"
fi

# ── summary ────────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════"
echo " Results: ${PASS} passed, ${FAIL} failed"
echo "═══════════════════════════════════════"
[ "$FAIL" -eq 0 ]
