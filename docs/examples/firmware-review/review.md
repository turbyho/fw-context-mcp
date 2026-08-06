# Code Review: v4.13.0-PRE.32 → HEAD (v4.15.3/devel_work)

**Scope:** 115 firmware files, 10 239 insertions, 5 236 deletions
**Review date:** 2026-07-06
**Method:** Deep recursive — fw-context call graph, find_references, search_bodies, search_content

> **💡 fw-context insight — review scoping:**
> The review started with `get_active_build()`, to verify that the index was
> healthy (52 646 symbols, 1 335 934 references, status "ready"). Then the
> review used `find_hotspots`, to identify the most-called functions. This
> step showed that `zdebug` (2 086 callers) and `get_ctime` (2 038 callers)
> were unchanged. So the review could safely focus on the 115 actually
> modified files, rather than on auditing the entire codebase.

---

## Review plan

| # | Area | Files | Main changes | Status |
|---|--------|---------|-------------|------|
| 1 | CH_ECB cleanup | 16 files | Removal of target, SIM800, sensors, bootloader | ✅ |
| 2 | BLE messaging subsystem | ble_msg.cpp/h, ble_msg_manager.cpp/h, cbor_msg.cpp/h, msg_manager.cpp/h | New BLE protocol (SDKv3), framing, CRC, CBOR | ✅ |
| 3 | nble.cpp/h | nble.cpp (+363), nble.h (+68) | SDKv3 char, MTU adaptation, chunked TX | ✅ |
| 4 | modem_msg.cpp/h | modem_msg.cpp (+718), modem_msg.h (+135) | Keyv3, DSP_COM, async open/cancel | ✅ |
| 5 | ncfgdata_manager/fram | ncfgdata_manager.cpp (+621), ncfgdata_fram.cpp (+237) | Key rotation, FRAM migration v1→v2, generic layout | ✅ |
| 6 | lb_keyboard.cpp/h + ble_cmd | lb_keyboard.cpp (+272), ble_cmd.cpp (+189) | Multipin/multishipment, dsp_com, SDKv2 guard | ✅ |
| 7 | inventory_writer | inventory_writer.cpp (+172) | Snapshot logic, dispatch_num_shipments, conditional set_slot | ✅ |
| 8 | NCbor contracts | lib/ncbor/generation/ (20+ files) | keyv2, keyv3, ble_token, dsp_com, msg_reservation | ✅ |
| 9 | app_manager, batt, download, lb_manager, nuart, nsd, nrtdata | 14 files | CH_ECB removal, BleMsgManager integration, SKEY critical level | ✅ |
| 10 | Configuration and build | nconfig.h, mbed_app.json, profiles, build_app.py, CI | NTP dual-pool, BLE parameters, restrict_size, SKEY_INIT | ✅ |

---

## Structural analysis — key architectural changes

### 1. CH_ECB_SIM800 removal — **CLEAN** ✅

fw-context found that all references to `CH_ECB_BOARD`, `SIM800`, `SIMCOM`, and `TARGET_CH_ECB` are completely removed:
- `search_content("CH_ECB", project_only=True)` → **0 results** (except a comment in `lb_manager.cpp`)
- `search_content("SIM800")` → **0 results**
- `search_content("SIMCOM")` → **0 results**
- 7 files physically deleted (nmodem_checb.cpp, sensors_CH_ECB.cpp, lb_modbus.cpp/h, SIM800 driver, CH_ECB bootloader)
- `find_dead_code` → no orphaned functions from the removal
- Minor issue: 3 dead `#include <nrf_nvmc.h>` (rs485.cpp, nsd.cpp, nble.cpp)

> **💡 fw-context insight — verifying complete removal:**
> `search_content("CH_ECB", project_only=True)` searched all application files
> through the FTS5 index, in a single call. A traditional `grep -r "CH_ECB"`
> would have scanned 2 562 files, and would have returned false positives
> from `#ifdef`-disabled code. `find_dead_code(project_only=true)` then
> confirmed that no orphaned functions remained. A manual check like this
> would be impossible across 67 000 lines of code.

### 2. BLE messaging subsystem — new layered architecture

```
AppManager::_periodic_app_task()
  ├── BleMsgManager::process_cons()  — RX framing, CRC, CBOR decode
  ├── BleMsgManager::process_prod()  — TX framing, CBOR encode, chunked write
  └── BleMsgManager::process_timers()
       ├── _ble_msgs[TOKEN]  — BleMsgToken (active)
       ├── _ble_msgs[STATE]  — BleMsgState (WIP, not yet implemented)
       └── _ble_msgs[DEFAULT] — fallback
```

Inheritance: `MsgManager → BleMsgManager`, `CborMsg → BleMsg → {Token, State, Default}`

> **💡 fw-context insight — mapping new architecture:**
> `get_file_map("ble_msg.cpp")` and `get_file_map("ble_msg_manager.cpp")` gave a
> structured table of contents for the new files instead of dumping 800+ raw
> lines. `get_inheritance_chain("nexbox::MsgManager", transitive=true)` and
> `get_method_overrides("nexbox::BleMsg::process_prod_specialized")` revealed the
> full virtual dispatch design in two calls — without reading a single header file.
> With `grep`, you would need to chase `: public` declarations manually, across 8 files.

### 3. Key management — rotation 4→2 slots

| Before | After |
|------|-----|
| 4 positions + LAST_KEY pointer | 2 fixed positions: ACTIVE (0) + GRACE (1) |
| `get_last_key<KeyType>()` | `get_key(KeyType, KeyStorageType, Key&)` |
| `set_key<KeyType>(Key&)` | `set_key(KeyType, Key&, KeyStorageType)` |
| `NUM_KEYS_FOR_ROTATION = 3` | `NUM_KEYS_FOR_ROTATION = 2` |

### 4. BLE stack — transition to higher throughput

| Parameter | 4.13.0-PRE.32 | HEAD | Factor |
|----------|--------------|------|---------|
| ATT MTU | 23 | 210 | 9.1× |
| ACL buffer | 64 | 214 | 3.3× |
| RX ACL buffer | 27 | 214 | 7.9× |
| Max notifications | 1 | 8 | 8× |
| 2M PHY | 0 | 1 | enabled |

---

## Findings

### 🔴 Critical (2)

#### 1. Memory leak — RX mailbox on `put()` failure

- **File:line** — `src/nble.cpp:564-568`
- **Root cause:** `_rx_mail_box.try_alloc()` allocates a slot. When `_rx_mail_box.put(p_msg_rx)` fails (mailbox full), `_rx_mail_box.free(p_msg_rx)` is missing.

  ```cpp
  osStatus status = _rx_mail_box.put(p_msg_rx);
  if (status != osOK) {
      NBOX_ERROR("rx put err:%d", (int)status);
      break;  // ← MISSING _rx_mail_box.free(p_msg_rx) !
  }
  ```

- **Runtime impact:** Repeated `put()` failure (mailbox full at 4 slots) → permanent loss of all RX slots → BLE message reception stops until reboot.
- **Evidence:** `get_source("nexbox::NBLE::onDataWritten")` — `try_alloc()` at line 547, `break` at line 567 without `free()`.
- **Fix:** Add `_rx_mail_box.free(p_msg_rx);` before `break;` on line 567.

> **💡 fw-context insight — finding memory leaks with precise extents:**
> `get_source("nexbox::NBLE::onDataWritten")` returned **only** the function
> body, from the opening `{` to the closing `}`, through libclang exact
> extents. A manual `read nble.cpp offset=540 limit=40` would require
> guessing where the function ends. Reading too little misses the `break`.
> Reading too much wastes context tokens on neighboring functions. The
> exact extents made the missing `free()` immediately visible.

#### 2. Unnamed variable `error` in `requestConnParamsUpdate()`

- **File:line** — `src/nble.cpp:730`
- **Root cause:** Function declares `ble_error_t err` (line 721), but `NBOX_ERROR` on line 730 uses `(int) error` — variable `error` does not exist in this scope.

  ```cpp
  ble_error_t err = _ble.gap().updateConnectionParameters(...);  // line 721
  ...
  NBOX_ERROR("updateConnectionParameters error, %d", (int) error); // line 730
  ```

- **Runtime impact:** Compilation error, or — if `error` is pulled from an outer scope — the log prints an incorrect value.
- **Evidence:** `get_source("nexbox::NBLE::requestConnParamsUpdate")` — `err` declared line 721, `error` used line 730. `lookup_symbol(exact=true, "error")` confirmed that `error` is **not** a global variable in application code. `lookup_symbol` found only local variables in other functions, and the mbed `error()` function.
- **Fix:** Change `(int) error` → `(int) err`.

> **💡 fw-context insight — cross-referencing symbol existence:**
> `get_source` pinpointed the mismatch. But the key verification came from
> `lookup_symbol(exact=true, "error")`. This tool searched all 52 646
> indexed symbols in milliseconds, and proved that no application-global
> `error` variable exists. A `grep -rn "error"` across 2 562 files would
> have returned hundreds of false positives: local variables, mbed's
> `error()` function, and comments.

### 🟡 Warning (10)

#### 3. Key storage layout breaking change — keys at indices 12-13 invisible after upgrade

- **File:line** — `src/ncfgdata_manager.cpp` (`_initialize_keys()`)
- **Root cause:** Older firmware stored keys at indices 10–13 with `LAST_KEY=14`. New firmware reads only indices 10–11 (ACTIVE/GRACE). A key at index 12/13 is invisible after upgrade.
- **Runtime impact:** PIN operations fail until the server sends new keys via the keyv3 endpoint.
- **Mitigation:** Server should send keys as soon as possible after upgrade.

#### 4. `ModemMsgDspCom::process_cons_specialized()` — ignores response data

- **File:line** — `src/modem_msg.cpp:4387-4414`
- **Root cause:** CBOR decode succeeds, but the code never reads `dsp_com_cons_s.map_shp` and `dsp_com_cons_s.map_st`. The code receives the response, but never uses the status (`st`) and shipment ID (`shp`) from the response.
- **Runtime impact:** The code does not recognize a server error state. The DSP_COM response is effectively "fire and forget".

#### 5. `restrict_size=0xF0000` in debug build — potential flash overflow

- **File:line** — `mbed_app_dbg.json:23`
- **Root cause:** `0xF0000` (983 040 B) + app start `0x10000` = end `0x100000`. nRF52840 has 1 MB flash (`0x00000–0xFFFFF`). App ends at `0x100000` = just past the flash boundary. Additionally, TDB storage starts at `0xFD000` — the app can overflow into TDB.
- **Runtime impact:** The build could fail at the linker stage. Or, worse, the overflow overwrites TDB data.
- **Fix:** Reduce to max `0xEDE00` (= `0xFD000 - 0x10000 - 0x200`).

#### 6. Missing `frame_length` validation in `BleMsgManager::process_cons()`

- **File:line** — `ble_msg_manager.cpp:168`
- **Root cause:** The code does not validate `_cons_header.get_frame_length()` against a maximum size. A corrupted frame with `frame_length = 0xFFFF` causes an infinite loop waiting for data.
- **Runtime impact:** Watchdog timeout on the main periodic loop.
- **Fix:** Add `if (frame_length > MAX_FRAME_LENGTH) { _clean_cons_process(); return; }`.

> **💡 fw-context insight — finding missing validations:**
> `get_source("nexbox::BleMsgManager::process_cons")` returned the complete
> function body with exact extents. The absence of a bounds check between
> the `get_frame_length()` call and the data accumulation loop was
> immediately visible. A `grep` search for `get_frame_length` would show
> only **where** the code calls the function, not **what** happens, or
> does not happen, around it.

#### 7. `_p_msg` not reset after CRC error

- **File:line** — `ble_msg_manager.cpp:251-260`
- **Root cause:** `process_finish(PARSE_ERROR)` → `_clean_cons_process()` resets `_processed_bytes`, `_crc_cons`, `_crc_cons_computed`, but `_p_msg` remains pointing to an instance in `Error` state.
- **Runtime impact:** On the next `process_cons()` call, `_p_msg` still points to the Error-state instance. `process_prod()` detects this and refuses, but it should be `nullptr`.
- **Fix:** Add `_p_msg = nullptr;` to `_clean_cons_process()`.

#### 8. Potential race condition: `_thread_app_func` vs `_periodic_app_task` on `_p_msg`

- **File:line** — `ble_msg_manager.cpp:428-434` vs `app_manager.cpp:482-485`
- **Root cause:** `process_timeout()` (called from `_thread_app_func`) reads `_p_msg` without synchronization. `_periodic_app_task` may simultaneously modify `_p_msg` in `process_cons()`.
- **Runtime impact:** Timeout handler could call `process_finish(TIMEOUT)` on a message that `process_cons` is currently replacing with a different one.
- **Fix:** Protect `_p_msg` with `CriticalSectionLock`.

> **💡 fw-context insight — detecting cross-module races:**
> The review found the race condition by running `find_callers` on **both**
> functions. `find_callers("nexbox::BleMsgManager::process_timeout")`
> returned `_thread_app_func`. `find_callers("nexbox::BleMsgManager::process_cons")`
> returned `_periodic_app_task`. The two call chains originate from
> different thread contexts, which reveals the unsynchronized access.
> Without fw-context, you would need to trace each caller manually, and
> recognize the threading model — something easily missed when reading
> files in isolation.

#### 9. Conditional `set_slot` may lose snapshot on `lock_open` failure

- **File:line** — `src/inventory_writer.cpp:172-212` (`dispatch_open_slot`)
- **Root cause:** When `lock_open` fails, break exits the do-while, state remains `DISPATCH_LOADED`, and the condition `if (state != DISPATCH_LOADED && state != STOCK_DISPATCHED)` prevents calling `set_slot()`. The code sets a snapshot with `SLOT_OPEN_FAILED` in memory, but does not persist the snapshot.
- **Runtime impact:** Diagnostic data is missing, but this does not affect open functionality.
- **Fix:** Add explicit `set_slot` in the error path for the snapshot.

#### 10. SKEY critical level change — `NONE` → `BOTH`

- **File:line** — `src/nrtdata.cpp:45`
- **Root cause:** `CFG_INVALID_SKEY` previously `operation_typ = NONE`, now `BOTH` — an invalid SKEY blocks both IN and OUT operations.
- **Runtime impact:** On SKEY degradation during operation (expired provisioning key, corrupted configuration), the box stops working for customers.
- **Mitigation:** Intentional change for the V3 key system — provisioning flow must be verified.

#### 11. `BleToken::token_decrypt()` — incomplete array initialization

- **File:line** — `src/ble_token.cpp:32`
- **Root cause:** `Key keys[NUM_KEYS_FOR_ROTATION] = {0,0}` initializes only the first 2 elements. If `NUM_KEYS_FOR_ROTATION` were increased, elements 2+ would contain stack garbage.
- **Runtime impact:** Currently no impact (`NUM_KEYS_FOR_ROTATION = 2`). Fragile against future changes.
- **Fix:** Change to `Key keys[NUM_KEYS_FOR_ROTATION] = {}`.

#### 12. `DSP_COM` uses `SocketType::NORMAL` — potential blocking

- **File:line** — `src/modem_msg.cpp` (default `_is_socket_ok()`)
- **Root cause:** DSP_COM messages compete with normal messages (telemetry, journal) on the NORMAL socket. `ModemMsgReservation` by contrast uses the HIGH socket.
- **Runtime impact:** COMPLETE/CANCEL operations may wait if the NORMAL socket is processing another message.
- **Fix:** Consider using the HIGH socket for DSP_COM.

### 🔵 Info (7)

#### 13. `BleMsgState::process_prod_specialized()` — unreachable code

- **File:line** — `ble_msg.cpp:866-882`
- **Description:** `return BleMsg::ProdSpecializedStatus::ERROR;` on line 867 precedes CBOR encode code. `BleMsgState` is not yet implemented.

#### 14. Unused methods in header — `_process_prod()`, `_release_prod_buffer()`

- **File:line** — `ble_msg_manager.h:107,112`
- **Description:** The header declares these methods, but never defines them. `search_bodies` returned an empty result for them.

#### 15. `get_last_key()` — dead code after migration to `get_keys()`

- **File:line** — `src/ncfgdata_manager.cpp`
- **Description:** `find_callers("nexbox::NCfgDataManager::get_last_key")` → zero callers. Method should be removed.

> **💡 fw-context insight — dead code detection:**
> `find_callers` returned zero results for `get_last_key`. But a `grep -rn
> "get_last_key"` would have found the definition, the template
> instantiation, and potentially comments. Only fw-context can distinguish
> "defined" from "actually called", without reading every result manually.
> For whole-project dead code, `find_dead_code(project_only=true)` scans
> all 67 000 lines in seconds. A manual search like this would take hours.

#### 16. `cordio.preferred-tx-power=45` in debug build

- **File:line** — `mbed_app_dbg.json`
- **Description:** Value 45 is extreme (release build has 2). Likely a typo or different unit.

#### 17. `localtime()` instead of `gmtime()` for RTC

- **File:line** — `lib/modem/nmodem_driver_QUECTEL_EG9X.cpp:1942`
- **Description:** Pre-existing bug. After NTP synchronization, the code uses `localtime()` (conversion to local time) to set the external RTC. Should be `gmtime()` (UTC). Comment states "We work only with UTC".

#### 18. Unused variable `elapsed_time` in `lb_keyboard.cpp`

- **File:line** — `src/lb_keyboard.cpp:543`
- **Description:** `uint32_t elapsed_time = _nrtdata.get_uptime_ms();` — value never used.

#### 19. Inconsistent types in `ble_mail_rx_data_t`

- **File:line** — `src/nble.h:28-33`
- **Description:** `_rx_data` is `char[]`, but `ble_mail_tx_data_t._tx_data` is `uint8_t[]`. The reviewer recommends unifying on `uint8_t`.

---

## Assumptions and residual risks

| # | Risk | Assessment |
|---|--------|-----------|
| R1 | Key storage layout change — keys at indices 12–13 invisible after upgrade | **High** — server must send new keys |
| R2 | FRAM migration v1→v2 — `_num_shipments` not copied | **Low** — sentinel 0xFF is valid, overwritten on first change |
| R3 | `NUM_KEYS_FOR_ROTATION = 2` — verify with 2 slots | **Medium** — key rotation must work with only ACTIVE+GRACE |
| R4 | BLE memory — increased ACL/MTU buffers | **Medium** — must verify RAM for 24-cabinet configuration |
| R5 | `NTP_MAX_DIFF_SEC = 10` — too strict | **Medium** — may block sync on NTP server desynchronization |
| R6 | CI: STG and PGW builds use DEV signing key | **Medium** — possible security risk |
| R7 | `SKEY_INIT_BASE64` — mandatory CI macro, build fails without it | **Low** — CI is configured |
| R8 | `BleMsgToken` is the only active handler, `BleMsgState` WIP | **Low** — expected by design |
| R9 | Single-thread model BLE event queue — no explicit synchronization | **Low** — currently safe, fragile against future changes |

---

## Structural impact summary

| Change | Impact | Safety |
|-------|-------|-----------|
| CH_ECB/SIM800 removal | 7 deleted files, 16 cleaned up, clean build | ✅ |
| New BLE messaging subsystem | 8 new files, parallel to ModemMsgManager | ✅ (with reservations) |
| SDKv3 BLE characteristic + chunked TX | NBLE +363 lines, MTU adaptation, new UUID | ✅ (2 critical bugs) |
| Keyv3 protocol (key rotation) | New endpoints, 2-slot model, commit flow | ⚠️ Breaking storage change |
| DSP_COM endpoint | New communication channel for slot operations | ⚠️ Response ignored |
| FRAM migration v1→v2 | New `_num_shipments` field, CircBuffer migration | ✅ (with reservation) |
| Inventory snapshot logic | Conditional set_slot, snapshot only for first operation | ⚠️ Snapshot lost on error |
| BLE ACL/MTU increase | 9× larger ATT MTU, 2M PHY | ⚠️ RAM consumption |
| NTP dual-pool | 2 independent NTP sources, cross-validation | ✅ |

---

## Summary by severity

| Severity | Count | Key issues |
|----------|-------|-----------------|
| 🔴 Critical | 2 | Memory leak RX mailbox, variable `error` vs `err` |
| 🟡 Warning | 10 | Key storage breaking change, ignored DSP_COM response, debug restrict_size, missing frame_length validation, race condition, lost snapshot, SKEY BOTH |
| 🔵 Info | 7 | Dead code, unused variables, inconsistent types, pre-existing bugs |

**Overall impression:** The changes represent a significant architectural shift — transition to SDKv3 BLE stack, new key management (V3), removal of CH_ECB target. The code is predominantly well-structured and follows existing conventions. **2 critical bugs must be fixed before release.** 10 warnings require attention — especially the breaking change in key storage layout and the ignored DSP_COM response.
