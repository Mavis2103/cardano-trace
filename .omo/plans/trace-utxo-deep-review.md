# UTXO Tracer — Deep Review Fixes

## TL;DR

> **Quick Summary**: Fix 10 bugs found in address trace logic, cache mechanism, and terminal logging. Setup pytest from scratch, apply TDD, make address trace behave correctly like UTXO trace.
>
> **Deliverables**:
> - pytest test suite for address_interactions, cache, forward, backward
> - Fixed depth off-by-one (B1)
> - Multi-hop cache incremental reuse (B2, B6)
> - Real-time terminal logging for batch path (B9, B10)
> - Minor fixes: B3, B5, B7, B8
>
> **Estimated Effort**: Medium
> **Parallel Execution**: YES — 4 waves
> **Critical Path**: Task 1 → Tasks 3-6 → Task 7 → Tasks 8-10 → F1-F4

---

## Context

### Original Request
Deep review tool trace address/UTXO. Fix bugs ở address trace logic, cache mechanism, và terminal logging. Address trace phải hoạt động đúng như UTXO trace.

### Interview Summary
**Key Discussions**:
- Address trace: đã code nhưng hoạt động sai. 5 explore agents đã deep review toàn bộ codebase.
- Cache: chỉ fix B2 (multi-hop cache bypass) + B5 (no-cache vẫn write). Không cần TTL/eviction.
- Multi-hop cache: phải dùng incremental — trace max-depth=10 trước, sau đó trace max-depth=5 chỉ query từ cache, trace max-depth=11 query thêm data mới.
- Test: TDD với pytest (chưa có test infrastructure, phải setup từ đầu).
- Logging: B9 (batch path yield burst) là vấn đề chính.
- Golden test case: không có sẵn, cần tìm/tạo từ cache DB.

**Research Findings**:
- 10 bugs identified across 5 files. B1 (depth off-by-one) và B2 (cache bypass) là critical.
- Cache DB tại `.utxo-cache/cache.db` có 1 address trace manifest incomplete (`addr_ba272f7f92d08ce1`, max_depth=3, chưa completed).
- Không có test file, không có pytest config.

### Metis Review
**Identified Gaps** (addressed):
- Q1 (golden test case): Tự tìm từ cache DB — dùng address incomplete trace làm reference.
- Q2 (cache TTL): Không cần, chỉ fix B2+B5.
- Q3 (multi-hop cache bypass): Bug, cần incremental cache.
- Q4 (test framework): pytest, setup từ đầu.
- Q5 (logging scope): B9 là vấn đề chính.

---

## Work Objectives

### Core Objective
Fix address trace để hoạt động đúng: depth chính xác, cache incremental, logging real-time. Match behavior của UTXO trace.

### Concrete Deliverables
- `tests/test_address_interactions.py` — TDD tests cho B1, B2, B3, B6, B9
- `tests/test_cache.py` — cache bypass test (B2, B5)
- `tests/test_forward.py` — cached_outputs test (B7)
- `tests/test_backward.py` — invalid OutRef test (B8)
- `tests/conftest.py`, `pytest.ini` — test infrastructure
- Fixed `address_interactions.py` — B1, B2, B3, B6
- Fixed `cli.py` — B2, B5, B9, B10
- Fixed `cache.py` — B2, B5
- Fixed `forward.py` — B7
- Fixed `backward.py` — B8

### Definition of Done
- [ ] `pytest tests/` → all tests pass (0 failures)
- [ ] Address trace `--max-depth 1` shows correct depth=1 for counterparties (B1)
- [ ] Two consecutive `--max-depth 2` calls use cache for second call (B2)
- [ ] Terminal output appears per-transaction during batch path (B9)
- [ ] `--no-cache` trace does NOT write to cache (B5)
- [ ] `cached_outputs` used in forward.py (B7)

### Must Have
- B1: Depth off-by-one fix
- B2: Multi-hop incremental cache
- B5: `--no-cache` respects write behavior
- B9: Real-time logging in batch path
- pytest test suite with all fixes covered by TDD

### Must NOT Have (Guardrails)
- Không thay đổi UTXO trace behavior (backward.py, forward.py ngoài B7, B8)
- Không thay đổi public API của cache.py
- Không thêm TTL/eviction vào cache
- Không thêm native asset tracking
- Không touch CEX modules, dash_app.py, config.py, providers
- Không refactor toàn bộ trace algorithm

---

## Verification Strategy

### Test Decision
- **Infrastructure exists**: NO (setup from scratch)
- **Automated tests**: TDD
- **Framework**: pytest

### QA Policy
Every task MUST include agent-executed QA scenarios.
Evidence saved to `.omo/evidence/task-{N}-{scenario-slug}.{ext}`.

- **CLI**: Use Bash — run `utxo-tracer trace-address`, capture stdout/stderr, assert output
- **API/Module**: Use Bash (python3 REPL) — import module, call functions, compare output
- **Cache**: Use Bash — sqlite3 queries to verify cache state

---

## Execution Strategy

### Parallel Execution Waves

```
Wave 1 (Start Immediately — test infrastructure):
├── Task 1: pytest setup + conftest [quick]
└── Task 2: Golden test case [quick]

Wave 2a (After Wave 1 — independent core fixes, MAX PARALLEL):
├── Task 3: Fix B1 — depth off-by-one [deep]
├── Task 4: Fix B2 — multi-hop cache bypass [deep]
├── Task 5: Fix B3 — batch path error tracking [quick]
├── Task 6: Fix B5 — --no-cache write behavior [quick]
└── Task 7: Fix B9 — batch path real-time logging [deep]

Wave 2b (After Task 4 — cache-dependent fix):
└── Task 8: Fix B6 — skip_tx_hashes for all addresses (depends: 4) [deep]

Wave 3 (After Wave 2b — minor fixes + regression):
├── Task 9: Fix B7 — cached_outputs in forward.py [quick]
├── Task 10: Fix B8 — invalid OutRef in backward.py [quick]
└── Task 11: Run full regression + integration [deep]

Wave FINAL (After ALL tasks — 4 parallel reviews):
├── Task F1: Plan compliance audit (oracle)
├── Task F2: Code quality review (unspecified-high)
├── Task F3: Real manual QA (unspecified-high)
└── Task F4: Scope fidelity check (deep)
```

Critical Path: Task 1 → Task 3 → Task 7 → Task 11 → F1-F4
Parallel Speedup: ~65% faster than sequential
Max Concurrent: 6 (Wave 2)

### Agent Dispatch Summary
- **1**: 2 — T1-T2 → `quick`
- **2**: 6 — T3-T4 → `deep`, T5-T6 → `quick`, T7-T8 → `deep`
- **3**: 3 — T9-T10 → `quick`, T11 → `deep`
- **FINAL**: 4 — F1 → `oracle`, F2 → `unspecified-high`, F3 → `unspecified-high`, F4 → `deep`

---

## TODOs

- [x] 1. **Pytest infrastructure setup**

  **What to do**:
  - Create `tests/` directory at project root
  - Create `tests/conftest.py` with shared fixtures: mock provider, real provider config, cache DB path
  - Create `pytest.ini` with `[tool:pytest]` section: testpaths=tests, pythonpath=src, asyncio_mode=auto
  - Check if `pytest` + `pytest-asyncio` in dependencies; if not, add to pyproject.toml dev deps
  - Verify: `pytest --collect-only` discovers tests (0 tests initially — expect "no tests collected")

  **Must NOT do**:
  - Don't add to existing dependencies — use `pip install -e ".[dev]"` pattern or just pytest + pytest-asyncio
  - Don't create test files for modules outside scope (CEX, dash_app, providers)

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with Task 2)
  - **Blocks**: Tasks 3-11
  - **Blocked By**: None

  **References**:
  - `pyproject.toml` — current deps list, add pytest-asyncio there
  - `src/utxo_tracer/cache.py:162-175` — `_get_db()` pattern for cache paths

  **Acceptance Criteria**:
  - [ ] `tests/` directory with `conftest.py` and `__init__.py`
  - [ ] `pytest.ini` with asyncio_mode=auto
  - [ ] `pip install pytest pytest-asyncio` succeeds
  - [ ] `pytest --collect-only` runs without errors

  **QA Scenarios**:
  ```
  Scenario: pytest discovers test files
    Tool: Bash
    Steps:
      1. cd /home/mavis/Documents/trace-utxo
      2. python3 -m pytest --collect-only tests/
    Expected Result: Exit code 0, output shows "no tests collected" or lists discovered tests
    Failure Indicators: Import errors, missing deps, config errors
    Evidence: .omo/evidence/task-1-pytest-collect.txt

  Scenario: asyncio tests work
    Tool: Bash
    Steps:
      1. Create a minimal async test in tests/test_smoke.py
      2. python3 -m pytest tests/test_smoke.py -v
    Expected Result: Test passes, no asyncio warnings
    Evidence: .omo/evidence/task-1-asyncio-smoke.txt
  ```

  **Commit**: YES
  - Message: `test(infra): add pytest setup with asyncio support`
  - Files: `tests/conftest.py`, `tests/__init__.py`, `pytest.ini`, `pyproject.toml`

- [x] 2. **Golden test case — find/create reference address trace**

  **What to do**:
  - Query `.utxo-cache/cache.db` trace_manifests for the incomplete address trace: `addr_ba272f7f92d08ce1`, address `addr1qx7lzh4zew0pen6g6a327ax5qymjp8km67n30rumtya29zgsyl0900j53wv5uuhj3wlll3fwzhzsqlukq7huwxf5sy9qjarrwg`
  - Run `utxo-tracer trace-address <ADDR> --max-depth 1 --no-dash --provider blockfrost` to get depth-1 output
  - Capture the output as golden reference in `tests/fixtures/golden_depth1.json`
  - Run `utxo-tracer trace-address <ADDR> --max-depth 2 --no-dash --provider blockfrost` to get depth-2 output
  - Capture as `tests/fixtures/golden_depth2.json`
  - Extract expected behavior: number of addresses, expected depth values, interaction counts per edge
  - Document in `tests/fixtures/README.md` what the golden data represents

  **Must NOT do**:
  - Don't hardcode specific address values in test assertions (addresses change) — assert structural properties
  - Don't create golden data from incorrect output (run against fixed code or accept current output as baseline for regression)

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with Task 1)
  - **Blocks**: Tasks 3, 4, 5, 8
  - **Blocked By**: None

  **References**:
  - `.utxo-cache/cache.db` — trace_manifests table has `addr_ba272f7f92d08ce1`
  - `src/utxo_tracer/cli.py:735-998` — trace-address command args
  - `src/utxo_tracer/models.py:135-143` — AddressTraceResult schema

  **Acceptance Criteria**:
  - [ ] `tests/fixtures/golden_depth1.json` exists with valid AddressTraceResult JSON
  - [ ] `tests/fixtures/golden_depth2.json` exists with valid AddressTraceResult JSON
  - [ ] `tests/fixtures/README.md` documents the fixtures

  **QA Scenarios**:
  ```
  Scenario: Golden data is valid JSON
    Tool: Bash
    Steps:
      1. python3 -c "import json; json.load(open('tests/fixtures/golden_depth1.json'))"
      2. python3 -c "import json; json.load(open('tests/fixtures/golden_depth2.json'))"
    Expected Result: No errors, JSON parses successfully
    Evidence: .omo/evidence/task-2-golden-valid.txt

  Scenario: Golden data has expected structure
    Tool: Bash
    Steps:
      1. python3 -c "
    import json
    d = json.load(open('tests/fixtures/golden_depth1.json'))
    assert 'target_address' in d
    assert 'addresses' in d
    assert 'edges' in d
    assert len(d['addresses']) > 0
    print(f'OK: {len(d[\"addresses\"])} addresses, {len(d[\"edges\"])} edges')
    "
    Expected Result: Prints OK with counts > 0
    Evidence: .omo/evidence/task-2-golden-structure.txt
  ```

  **Commit**: YES
  - Message: `test(fixtures): add golden test case for address trace`
  - Files: `tests/fixtures/golden_depth1.json`, `tests/fixtures/golden_depth2.json`, `tests/fixtures/README.md`

- [x] 3. **Fix B1: Depth off-by-one in `_update_addr_data`**

  **What to do**:
  - Write failing test first: create `tests/test_address_interactions.py` with `test_depth_assignment()` using mock provider
  - Test: target address at depth 0, direct counterparty at depth 1, counterparty-of-counterparty at depth 2
  - Fix in `src/utxo_tracer/tracing/address_interactions.py` L404-405 and L414-415: change `addr_depth[addr] = current_depth` to `addr_depth[addr] = current_depth + 1`
  - Also fix L370 in `_record_tx_edges`: edge depth stored as `current_depth` — counterparty edge should use `current_depth + 1`
  - Run test → should now pass
  - Verify with golden test case: `pytest tests/ -k depth`

  **Must NOT do**:
  - Don't change target address depth (must stay 0)
  - Don't change the BFS queue depth logic (queue uses correct `current_depth + 1` already)

  **Recommended Agent Profile**:
  - **Category**: `deep`
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 2 (with Tasks 4-8)
  - **Blocks**: Task 11
  - **Blocked By**: Tasks 1, 2

  **References**:
  - `src/utxo_tracer/tracing/address_interactions.py:387-419` — `_update_addr_data` function
  - `src/utxo_tracer/tracing/address_interactions.py:365-376` — `_record_tx_edges` edge depth
  - `src/utxo_tracer/models.py:113` — `AddressInteractionNode.depth` doc: "hop distance from target address"

  **Acceptance Criteria**:
  - [ ] Test file: `tests/test_address_interactions.py::test_depth_assignment` → PASS
  - [ ] `pytest tests/ -k depth` → all depth tests pass
  - [ ] `AddressInteractionNode.depth` values: target=0, direct counterparty=1, 2-hop=2
  - [ ] `AddressInteractionEdge.source_depth` values match node depths at discovery point

  **QA Scenarios**:
  ```
  Scenario: Depth assignment correct in mock trace
    Tool: Bash
    Preconditions: Mock provider returning known txs
    Steps:
      1. python3 -m pytest tests/test_address_interactions.py::test_depth_assignment -v
    Expected Result: Test passes. Target depth=0, counterparty depth=1.
    Failure Indicators: Counterparty depth=0, assertion error on depth values
    Evidence: .omo/evidence/task-3-depth-test.txt

  Scenario: Real trace depth-1 shows correct depths
    Tool: Bash
    Steps:
      1. utxo-tracer trace-address <GOLDEN_ADDR> --max-depth 1 --no-dash --output json 2>/dev/null | python3 -c "
    import json, sys
    d = json.load(sys.stdin)
    for a in d['addresses']:
        if a['is_target']: assert a['depth'] == 0, f'Target depth {a[\"depth\"]}'
        else: assert a['depth'] == 1, f'Non-target depth {a[\"depth\"]}'
    print('OK: all depths correct')
    "
    Expected Result: Prints "OK: all depths correct"
    Failure Indicators: Non-target depth is 0 instead of 1
    Evidence: .omo/evidence/task-3-depth-real.txt
  ```

  **Commit**: YES
  - Message: `fix(address): correct depth off-by-one in address trace nodes and edges`
  - Files: `src/utxo_tracer/tracing/address_interactions.py`, `tests/test_address_interactions.py`

- [x] 4. **Fix B2: Multi-hop incremental cache reuse**

  **What to do**:
  - Write failing test: `test_multi_hop_cache_reuse()` — run trace with max_depth=3, then max_depth=2, verify second run uses cache
  - Test must mock provider and count calls — second run should make 0 provider calls
  - Fix in `cli.py` L774: remove `_skip_cache = max_depth > 1` block
  - In `cli.py`, modify cache lookup to use `_find_best_cache()` pattern from UTXO trace (load partial manifest, check cached_max_depth, extend if needed)
  - In `address_interactions.py`, ensure `skip_tx_hashes` accumulates for ALL addresses at ALL depths (not just target)
  - In `cache.py`, ensure `load_address_trace_partial()` returns step data for all depths
  - Verify: `pytest tests/ -k cache_reuse`

  **Must NOT do**:
  - Don't use cached data when cached_max_depth < requested max_depth (must query new data)
  - Don't change the UTXO trace cache lookup pattern
  - Don't add TTL/eviction

  **Recommended Agent Profile**:
  - **Category**: `deep`
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 2 (with Tasks 3, 5-8)
  - **Blocks**: Task 11
  - **Blocked By**: Tasks 1, 2

  **References**:
  - `src/utxo_tracer/cli.py:768-848` — current cache logic for address trace (L774 is the bug)
  - `src/utxo_tracer/cli.py:628-661` — UTXO trace cache lookup (reference pattern)
  - `src/utxo_tracer/cache.py:600-700` — `_find_best_cache()`, `load_trace_partial()`
  - `src/utxo_tracer/tracing/address_interactions.py:138-140` — skip_tx_hashes usage

  **Acceptance Criteria**:
  - [ ] `pytest tests/ -k cache_reuse` → PASS
  - [ ] Run `--max-depth 3` then `--max-depth 2` → second run uses cache (0 provider calls for cached txs)
  - [ ] Run `--max-depth 3` then `--max-depth 5` → second run queries only new depth-4 and depth-5 data
  - [ ] Cache manifest shows `completed=1` after successful trace

  **QA Scenarios**:
  ```
  Scenario: Sequential traces reuse cache
    Tool: Bash
    Steps:
      1. utxo-tracer trace-address <GOLDEN_ADDR> --max-depth 3 --no-dash --no-cache  # baseline
      2. utxo-tracer trace-address <GOLDEN_ADDR> --max-depth 2 --no-dash  # should use cache
      3. Check output contains "Loaded from local cache" or "[dim]address: N cached + M new"
    Expected Result: Second run shows cache usage, no provider calls logged
    Failure Indicators: "Loaded from local cache" not shown, all txs fetched fresh
    Evidence: .omo/evidence/task-4-cache-reuse.txt

  Scenario: Deeper trace extends cache
    Tool: Bash
    Steps:
      1. utxo-tracer trace-address <GOLDEN_ADDR> --max-depth 2 --no-dash  # cache depth 2
      2. utxo-tracer trace-address <GOLDEN_ADDR> --max-depth 3 --no-dash  # extend to depth 3
      3. Check output shows cache extension message
    Expected Result: "Cache: depth 2 → 3 — extending" or similar
    Evidence: .omo/evidence/task-4-cache-extend.txt
  ```

  **Commit**: YES
  - Message: `fix(cache): enable incremental cache reuse for multi-hop address traces`
  - Files: `src/utxo_tracer/cli.py`, `src/utxo_tracer/cache.py`, `tests/test_address_interactions.py`

- [x] 5. **Fix B3: Batch path empty results logged to errors**

  **What to do**:
  - Write test: `test_batch_empty_result_error_tracking()` — mock provider returning empty tx data, verify error appears in result
  - Fix in `address_interactions.py` L215-218: when `has_data` is False, append error to `errors` list (not just step_callback)
  - Also check `_process_tx_data_static` — ensure it returns whether data was valid, so caller can track
  - Verify: `pytest tests/ -k empty_result`

  **Must NOT do**:
  - Don't change error propagation for concurrent path (it already works)
  - Don't add retry logic for empty results

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 2
  - **Blocks**: None
  - **Blocked By**: Tasks 1, 2

  **References**:
  - `src/utxo_tracer/tracing/address_interactions.py:203-222` — batch path loop
  - `src/utxo_tracer/tracing/address_interactions.py:160-171` — concurrent path error handling (reference)

  **Acceptance Criteria**:
  - [ ] `pytest tests/ -k empty_result` → PASS
  - [ ] `AddressTraceResult.error` contains empty-result errors from batch path
  - [ ] `errors` list in address_interactions matches step_callback error count

  **QA Scenarios**:
  ```
  Scenario: Empty tx result tracked in errors
    Tool: Bash
    Steps:
      1. python3 -m pytest tests/test_address_interactions.py::test_batch_empty_result_error_tracking -v
    Expected Result: Test passes, errors list contains "empty result" entries
    Evidence: .omo/evidence/task-5-empty-result.txt
  ```

  **Commit**: YES (groups with T3, T6)
  - Message: `fix(address): track empty batch results in error list`
  - Files: `src/utxo_tracer/tracing/address_interactions.py`, `tests/test_address_interactions.py`

- [x] 6. **Fix B5: `--no-cache` should not write to cache**

  **What to do**:
  - Write test: `test_no_cache_no_write()` — run trace with `--no-cache`, verify no new data in cache DB
  - Fix in `cli.py`: wrap final `save_trace()` / `save_address_trace()` calls in `--no-cache` guard
  - Also check per-step `save_address_trace_step()` inside `_step_callback` — should be guarded too
  - Verify: `pytest tests/ -k no_cache`

  **Must NOT do**:
  - Don't break the per-step manifest save for non-`--no-cache` runs
  - Don't prevent reading from cache (only writing)

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 2
  - **Blocks**: None
  - **Blocked By**: Tasks 1, 2

  **References**:
  - `src/utxo_tracer/cli.py:695-699` — final save_trace call in UTXO trace
  - `src/utxo_tracer/cli.py:878-885` — `_step_callback` save_address_trace_step
  - `src/utxo_tracer/cli.py:961-981` — final save_address_trace call

  **Acceptance Criteria**:
  - [ ] `pytest tests/ -k no_cache` → PASS
  - [ ] `--no-cache` trace: no new rows in trace_manifests, trace_steps, trace_snapshots
  - [ ] `--no-cache` trace: existing cache data unchanged
  - [ ] Without `--no-cache`: cache writes normally

  **QA Scenarios**:
  ```
  Scenario: --no-cache prevents cache writes
    Tool: Bash
    Steps:
      1. utxo-tracer cache clear  # start clean
      2. utxo-tracer trace-address <GOLDEN_ADDR> --max-depth 1 --no-dash --no-cache
      3. python3 -c "
    import sqlite3
    conn = sqlite3.connect('.utxo-cache/cache.db')
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM trace_manifests')
    assert cur.fetchone()[0] == 0, 'No-cache should not write manifests'
    print('OK: no cache written')
    "
    Expected Result: Prints "OK: no cache written"
    Failure Indicators: Manifest rows exist after --no-cache run
    Evidence: .omo/evidence/task-6-no-cache.txt
  ```

  **Commit**: YES (groups with T3, T5)
  - Message: `fix(cache): respect --no-cache flag for write operations`
  - Files: `src/utxo_tracer/cli.py`, `tests/test_cache.py`

- [x] 7. **Fix B9: Real-time logging for batch path**

  **What to do**:
  - Write integration test: `test_batch_path_real_time_output()` — run trace-address, capture stdout line-by-line, verify timestamps show progressive output (not all at end)
  - Current problem: batch path L203-222 processes ALL txs for one address in tight loop, `asyncio.sleep(0)` at L222 yields ONCE after all txs
  - Fix: restructure batch loop to yield after each tx (not after all). Move `await asyncio.sleep(0)` inside the loop, after step_callback
  - Or: use `progress.update()` which already flushes in pipe mode via `_unbuffered_write()` → check if the issue is that the callback fires but Rich doesn't render until event loop yields
  - Actually fix: ensure `step_callback` → `progress.update()` → Rich renders immediately. Check `_rich_tty.py` — TTY mode works, pipe mode uses `_unbuffered_write()`. The issue might be that batch path fires 1000 callbacks before yielding. Solution: yield after each batch of 10 txs
  - Also add `progress_callback` support to batch path (B10): call it with `(processed, total)` after each batch
  - Verify: `pytest tests/ -k real_time`

  **Must NOT do**:
  - Don't change the UTXO trace logging (already works correctly)
  - Don't change the concurrent path logging (already works correctly)

  **Recommended Agent Profile**:
  - **Category**: `deep`
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 2
  - **Blocks**: Task 11
  - **Blocked By**: Tasks 1, 2

  **References**:
  - `src/utxo_tracer/cli.py:887-913` — `_step_callback` progress update
  - `src/utxo_tracer/cli.py:915-921` — LiveProgress creation
  - `src/utxo_tracer/_rich_tty.py:86-96` — `_unbuffered_write()` for pipe mode
  - `src/utxo_tracer/tracing/address_interactions.py:203-222` — batch path loop (needs restructuring)
  - `src/utxo_tracer/cli.py:449-451` — UTXO trace progress update (reference pattern)

  **Acceptance Criteria**:
  - [ ] `pytest tests/ -k real_time` → PASS
  - [ ] Terminal shows progressive output within 1s of first tx processing
  - [ ] Progress bar updates incrementally (not 0→1000 in one jump)
  - [ ] Batch path calls `progress_callback` for overall completion tracking (B10 fix)

  **QA Scenarios**:
  ```
  Scenario: Real-time output in pipe mode
    Tool: Bash
    Steps:
      1. utxo-tracer trace-address <HIGH_TX_ADDR> --max-depth 1 --no-dash 2>&1 | ts -s
      2. Check that timestamp deltas are small (<5s between first and second tx line)
    Expected Result: Progressive timestamps, not one big burst at end
    Failure Indicators: All tx lines appear in same second, or output arrives only after trace completes
    Evidence: .omo/evidence/task-7-real-time.txt

  Scenario: Progress bar updates incrementally
    Tool: Bash
    Steps:
      1. utxo-tracer trace-address <HIGH_TX_ADDR> --max-depth 1 --no-dash 2>&1
      2. Check output contains multiple progress update lines
    Expected Result: Multiple "[N] address tx=..." lines with increasing N
    Evidence: .omo/evidence/task-7-progress-incremental.txt
  ```

  **Commit**: YES
  - Message: `fix(address): real-time progress output for batch path processing`
  - Files: `src/utxo_tracer/tracing/address_interactions.py`, `src/utxo_tracer/cli.py`, `tests/test_address_interactions.py`

- [x] 8. **Fix B6: `skip_tx_hashes` for all addresses at all depths**

  **What to do**:
  - Write test: `test_skip_tx_hashes_multi_address()` — verify that cached txs for counterparty addresses are also skipped
  - Fix in `address_interactions.py` L139: change from `{address: skip_tx_hashes}` to per-address dict
  - Fix in `cli.py` L929: pass full `skip_tx_hashes` dict keyed by all addresses from cache manifest
  - In `cache.py`, extend `load_address_trace_partial()` to return per-address `processed` set (currently only returns tx hashes for target)
  - Verify: `pytest tests/ -k skip_tx`

  **Must NOT do**:
  - Don't break `skip_tx_hashes` for the target address (must still work)
  - Don't change the manifest schema

  **Recommended Agent Profile**:
  - **Category**: `deep`
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 2
  - **Blocks**: None
  - **Blocked By**: Tasks 1, 2, 4

  **References**:
  - `src/utxo_tracer/tracing/address_interactions.py:138-140` — skip_tx_hashes check
  - `src/utxo_tracer/cli.py:929` — skip_tx_hashes passed as `{address: ...}`
  - `src/utxo_tracer/cache.py:800-850` — `load_address_trace_partial()` return value

  **Acceptance Criteria**:
  - [ ] `pytest tests/ -k skip_tx` → PASS
  - [ ] When expanding counterparty at depth 1, its already-processed txs are skipped
  - [ ] Multi-hop trace with cache: no duplicate provider calls for any address

  **QA Scenarios**:
  ```
  Scenario: Counterparty txs skipped from cache
    Tool: Bash
    Steps:
      1. python3 -m pytest tests/test_address_interactions.py::test_skip_tx_hashes_multi_address -v
    Expected Result: Test passes. Mock provider call count matches expected (no re-fetching)
    Evidence: .omo/evidence/task-8-skip-tx.txt
  ```

  **Commit**: YES (groups with T4)
  - Message: `fix(cache): extend skip_tx_hashes to all addresses at all depths`
  - Files: `src/utxo_tracer/tracing/address_interactions.py`, `src/utxo_tracer/cli.py`, `src/utxo_tracer/cache.py`, `tests/test_address_interactions.py`

- [x] 9. **Fix B7: Wire `cached_outputs` in forward.py**

  **What to do**:
  - Write test: `test_cached_outputs_used()` — mock provider, verify forward doesn't call `get_spent_utxos` when cached_outputs has data
  - Fix in `forward.py`: add check at L60-65 — if `cached_outputs` has the address, use cached data instead of calling provider
  - Pattern: same as how `cached_nodes` is used at L62-63
  - Verify: `pytest tests/ -k cached_outputs`

  **Must NOT do**:
  - Don't change backward.py or address_interactions.py
  - Don't change the cached_outputs data structure or population in cli.py

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 3 (with Tasks 10, 11)
  - **Blocks**: None
  - **Blocked By**: Task 1

  **References**:
  - `src/utxo_tracer/tracing/forward.py:23` — `cached_outputs` parameter
  - `src/utxo_tracer/tracing/forward.py:60-65` — where `get_spent_utxos` is called
  - `src/utxo_tracer/tracing/backward.py:53-54` — pattern for using cached_nodes

  **Acceptance Criteria**:
  - [ ] `pytest tests/ -k cached_outputs` → PASS
  - [ ] Mock provider `get_spent_utxos` not called when cached_outputs has the address

  **QA Scenarios**:
  ```
  Scenario: Forward trace uses cached outputs
    Tool: Bash
    Steps:
      1. python3 -m pytest tests/test_forward.py::test_cached_outputs_used -v
    Expected Result: Test passes, provider not called for cached addresses
    Evidence: .omo/evidence/task-9-cached-outputs.txt
  ```

  **Commit**: YES
  - Message: `fix(forward): use cached_outputs to skip provider calls`
  - Files: `src/utxo_tracer/tracing/forward.py`, `tests/test_forward.py`

- [x] 10. **Fix B8: Invalid OutRef in backward error yields**

  **What to do**:
  - Write test: `test_backward_error_outref_valid()` — verify error steps have valid OutRef
  - Fix in `backward.py` L118-126: change `OutRef(out_ref.tx_hash, -1)` to use the original `out_ref` (not -1)
  - The error step's `out_ref` should be the UTXO being processed, not a synthetic one
  - Verify: `pytest tests/ -k error_outref`

  **Must NOT do**:
  - Don't change forward.py error handling
  - Don't change how `build_graph_from_steps` handles error steps

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 3 (with Tasks 9, 11)
  - **Blocks**: None
  - **Blocked By**: Task 1

  **References**:
  - `src/utxo_tracer/tracing/backward.py:118-126` — error yield with invalid OutRef
  - `src/utxo_tracer/models.py:10-24` — OutRef class definition

  **Acceptance Criteria**:
  - [ ] `pytest tests/ -k error_outref` → PASS
  - [ ] Error steps have `out_ref.output_index >= 0`
  - [ ] Error steps have same `out_ref` as the UTXO being processed

  **QA Scenarios**:
  ```
  Scenario: Backward error step has valid OutRef
    Tool: Bash
    Steps:
      1. python3 -m pytest tests/test_backward.py::test_backward_error_outref_valid -v
    Expected Result: Test passes, error step out_ref has valid output_index (not -1)
    Evidence: .omo/evidence/task-10-error-outref.txt
  ```

  **Commit**: YES (groups with T9)
  - Message: `fix(backward): use valid OutRef in error step yields`
  - Files: `src/utxo_tracer/tracing/backward.py`, `tests/test_backward.py`

- [x] 11. **Full regression test suite + integration test**

  **What to do**:
  - Run ALL tests: `pytest tests/ -v` — ensure all pass
  - Write integration test: `test_full_address_trace_pipeline()` — mock provider, run end-to-end trace, verify result structure
  - Test all edge cases identified by Metis:
    - Address with 0 transactions → empty result, no crash
    - Circular A→B→A transactions → BFS terminates correctly
    - `--max-depth 0` → returns only target address
    - Self-transactions (change outputs) → handled gracefully
    - Concurrent cache access → no corruption (within single process)
  - Run coverage: `pytest --cov=src/utxo_tracer tests/` — ensure >70% on changed files
  - Verify no regressions in UTXO trace: run `utxo-tracer trace <CACHED_UTXO> --max-depth 2 --no-dash` and check output unchanged

  **Must NOT do**:
  - Don't add tests for modules outside scope (CEX, dash_app, providers)
  - Don't write tests that require real network access (mock all providers)

  **Recommended Agent Profile**:
  - **Category**: `deep`
  - **Skills**: none

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 3 (with Tasks 9, 10)
  - **Blocks**: None
  - **Blocked By**: Tasks 1-8

  **References**:
  - All test files created in Tasks 3-10
  - `src/utxo_tracer/models.py:100-144` — AddressTraceResult, AddressInteractionNode, AddressInteractionEdge
  - `tests/fixtures/golden_depth1.json` — reference data

  **Acceptance Criteria**:
  - [ ] `pytest tests/ -v` → all tests pass, 0 failures
  - [ ] `pytest --cov=src/utxo_tracer/tracing --cov=src/utxo_tracer/cache.py tests/` → coverage ≥ 70% on changed files
  - [ ] UTXO trace regression: output identical to before (no unexpected changes)
  - [ ] Edge case tests: 0-tx address, circular trace, max-depth 0 all pass

  **QA Scenarios**:
  ```
  Scenario: Full test suite passes
    Tool: Bash
    Steps:
      1. python3 -m pytest tests/ -v --tb=short
    Expected Result: All tests pass, no failures, no errors
    Failure Indicators: Any FAILED or ERROR in test output
    Evidence: .omo/evidence/task-11-full-suite.txt

  Scenario: Coverage meets threshold
    Tool: Bash
    Steps:
      1. python3 -m pytest --cov=src/utxo_tracer/tracing --cov=src/utxo_tracer/cache.py --cov-report=term tests/ 2>&1 | tail -20
    Expected Result: Coverage ≥ 70% for address_interactions.py, cache.py, forward.py, backward.py
    Evidence: .omo/evidence/task-11-coverage.txt

  Scenario: UTXO trace regression check
    Tool: Bash
    Steps:
      1. utxo-tracer trace <CACHED_UTXO> --max-depth 2 --no-dash --output json 2>/dev/null | python3 -c "
    import json, sys
    d = json.load(sys.stdin)
    assert 'nodes' in d and 'edges' in d
    print(f'OK: {len(d[\"nodes\"])} nodes, {len(d[\"edges\"])} edges')
    "
    Expected Result: Prints OK with node/edge counts
    Evidence: .omo/evidence/task-11-utxo-regression.txt
  ```

  **Commit**: YES
  - Message: `test(regression): add edge case tests and coverage verification`
  - Files: `tests/test_regression.py`, `tests/test_edge_cases.py`

---

## Final Verification Wave

- [x] F1. **Plan Compliance Audit** — `oracle`
  Read the plan end-to-end. For each "Must Have": verify implementation exists (read file, run test). For each "Must NOT Have": search codebase for forbidden patterns — reject with file:line if found. Check evidence files exist in `.omo/evidence/`. Compare deliverables against plan.
  Output: `Must Have [N/N] | Must NOT Have [N/N] | Tasks [11/11] | VERDICT: APPROVE/REJECT`

- [x] F2. **Code Quality Review** — `unspecified-high`
  Run `pytest tests/ -v` + `python3 -m py_compile` on all changed files. Review all changed files for: bare `except: pass`, commented-out code, unused imports, `console.log` equivalent in Python. Check AI slop: excessive comments, over-abstraction, generic variable names.
  Output: `Tests [N pass/N fail] | Compile [PASS/FAIL] | Files [N clean/N issues] | VERDICT`

- [x] F3. **Real Manual QA** — `unspecified-high`
  Start from clean cache: `utxo-tracer cache clear`. Execute EVERY QA scenario from EVERY task — follow exact steps, capture evidence. Test cross-task integration: depth fix (T3) + cache reuse (T4) together. Test edge cases: empty address, max-depth 0, circular trace. Save to `.omo/evidence/final-qa/`.
  Output: `Scenarios [N/N pass] | Integration [N/N] | Edge Cases [N tested] | VERDICT`

- [x] F4. **Scope Fidelity Check** — `deep`
  For each task: read "What to do", read actual diff (git diff). Verify 1:1 — everything in spec was built (no missing), nothing beyond spec was built (no creep). Check "Must NOT do" compliance. Detect cross-task contamination. Flag unaccounted changes.
  Output: `Tasks [11/11 compliant] | Contamination [CLEAN/N issues] | Unaccounted [CLEAN/N files] | VERDICT`

---

## Commit Strategy

- **1-2**: `test(infra): add pytest setup and golden test case` — tests/conftest.py, tests/__init__.py, pytest.ini, tests/fixtures/
- **3,5,6**: `fix(address): correct depth, error tracking, --no-cache behavior` — address_interactions.py, cli.py, tests/
- **4,8**: `fix(cache): incremental multi-hop cache with per-address skip` — cli.py, cache.py, address_interactions.py, tests/
- **7**: `fix(address): real-time progress output for batch path` — address_interactions.py, cli.py, tests/
- **9,10**: `fix(tracing): wire cached_outputs in forward, valid OutRef in backward` — forward.py, backward.py, tests/
- **11**: `test(regression): edge case coverage and full integration suite` — tests/

---

## Success Criteria

### Verification Commands
```bash
pytest tests/ -v                                    # All tests pass
pytest --cov=src/utxo_tracer/tracing --cov=src/utxo_tracer/cache.py tests/  # Coverage ≥ 70%
utxo-tracer trace-address <ADDR> --max-depth 1      # Depth labels: target=0, other=1
utxo-tracer trace-address <ADDR> --max-depth 2      # Second run uses cache
utxo-tracer trace <UTXO> --max-depth 2              # UTXO trace unchanged (regression)
```

### Final Checklist
- [ ] All "Must Have" present (B1, B2, B5, B9 fixes + pytest suite)
- [ ] All "Must NOT Have" absent (no UTXO trace changes beyond B7/B8, no TTL, no CEX changes)
- [ ] All tests pass (pytest, 0 failures)
- [ ] Coverage ≥ 70% on changed files
- [ ] Address trace matches UTXO trace behavior for logging cadence
