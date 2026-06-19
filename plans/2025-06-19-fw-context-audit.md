# Plán: Audit a opravy fw-context na reálných projektech

**Status:** Probíhá — Fáze 1: chybové hlášky ✅ hotovo
**Datum:** 2025-06-19
**Projekty:** zbox-ecb-fw (Mbed OS, 52k symbolů), HA_Boiler (PlatformIO, 15k symbolů)

## 0. Revize

| Datum | Revize |
|-------|--------|
| 2025-06-19 | **Investigace BUG-1/2/3:** `AppManager::run` v kódu vůbec neexistuje (projekt používá `_thread_app_func`). `uart_send` také neexistuje. Všechny tři byly falešné poplachy způsobené chybnými testovacími dotazy. |
| 2025-06-19 | **BUG-5 rozšířen:** Není to jen `find_callers` + proměnné — 6 call-graph nástrojů (`find_callers`, `find_references`, `find_all_callers_recursive`, `find_callees_recursive`, `find_call_path`, `find_wrapper_callers`) nerozlišovalo "symbol not found" vs "symbol found but empty results". |
| 2025-06-19 | **Oprava chybových hlášek dokončena:** Všech 6 nástrojů nyní používá `_lookup_definition()` guard — při nenalezení vrací `{"error": "Symbol not found: {name}"}`, při nalezení bez výsledků `{"info": "No X found for '{name}'."}`. Testy: 175/175 pass, přímé Python ověření OK. |

---

## 1. Cíl

Na základě systematického auditu na dvou reálných embedded projektech identifikovat
a postupně opravit chyby, degradace a úzká místa v MCP nástrojích fw-context.

## 2. Testovací prostředí

| Vlastnost | Hodnota |
|-----------|---------|
| Ollama URL | `http://localhost:11434` |
| Chat model | `qwen2.5-coder:14b` |
| Embedding model | `mxbai-embed-large` |
| Python | 3.11.8 (pyenv) |
| fw-context verze | aktuální `main` |
| Datum testu | 2025-06-19 |

### Projekty

| Vlastnost | zbox-ecb-fw | HA_Boiler |
|-----------|-------------|-----------|
| Cesta | `/home/turbyho/dev/sw/work/packeta/zbox-ecb-fw` | `/home/turbyho/dev/sw/work/privat/HA_Boiler` |
| Build systém | Mbed OS | PlatformIO (Arduino/ESP32) |
| Symbolů | 52 406 | 14 977 |
| Souborů | 1 693 | 430 |
| Referencí | 1 285 048 | 108 924 |
| Stale | ne | ne |

---

## 3. Testovací scénáře a výsledky

### 3.1 Základní nástroje (lookup, source, map, context)

#### T1 — `lookup_symbol` exact match

| Projekt | Dotaz | Výsledek | Stav |
|---------|-------|----------|:----:|
| zbox | `uart_init` exact | Nenalezeno. `_did_you_mean`: `port_init`, `nrfx_uarte_init`, `_init`, `uart_get`, `irq_init` | ⚠️ |
| zbox | `neexistujici_funkce_xyz` | `[]` | ✅ |
| HA | `setup` exact | `setup()` v `src/main.cpp:38` + `i2c_clk_cal_t::setup` field | ✅ |
| HA | `neexistuje_vubec_nic_xyz` | `[]` | ✅ |

#### T2 — `lookup_symbol` prefix match

| Projekt | Dotaz | Výsledek | Stav |
|---------|-------|----------|:----:|
| zbox | `modem` (prefix) | 50 výsledků: `ModemMsgManager`, `ModemMode`, `MODEM` enum constanty, `ModemMsg*` třídy | ✅ |
| zbox | `SPI` (prefix) | 9 výsledků: `spi_init_direct`, `SPI` konstruktory, `spi_t`, `SPIName` | ✅ |
| HA | `boiler` (prefix) | 6 výsledků: `boiler_control`, `batFull`, `boiler1_pwr`, `boiler2_pwr` atd. | ✅ |

#### T3 — `get_source`

| Projekt | Dotaz | Výsledek | Stav |
|---------|-------|----------|:----:|
| zbox | `main` | 151 řádků — RTC init, modem init, BLE start, WDT | ✅ |
| HA | `loop` | 64 řádků — WiFi reconnect, teplota, modbus poll, CSV log, HA update | ✅ |

#### T4 — `get_file_map`

| Projekt | Dotaz | Výsledek | Stav |
|---------|-------|----------|:----:|
| zbox | `src/main.cpp` | 28 symbolů: 6 funkcí (`main`, handlery), 22 proměnných | ✅ |
| HA | `src/main.cpp` | 12 symbolů: 4 funkce (`printLocalTime`, `timeavailable`, `setup`, `loop`), 8 proměnných | ✅ |

#### T5 — `get_symbol_context`

| Projekt | Dotaz | Výsledek | Stav |
|---------|-------|----------|:----:|
| zbox | `MbedCRC` | 48 callerů (všechny template instantiation konstruktoru), 0 callees, zdrojový kód | ⚠️ |
| HA | `setup` | 1 caller (`loopTask`), 31 callees (projektové i framework), zdrojový kód | ✅ |

---

### 3.2 Call-graph nástroje

#### T6 — `find_callers`

| Projekt | Dotaz | Výsledek | Stav |
|---------|-------|----------|:----:|
| zbox | `box_manager` (globální proměnná) | "No callers found for 'box_manager'. Check the name" — `box_manager` je proměnná, ne funkce. Lepší hláška: "Symbol is a variable, use find_references" | ⚠️ |

#### T7 — `find_hotspots`

| Projekt | Dotaz | Výsledek | Stav |
|---------|-------|----------|:----:|
| zbox | top 15 | `callback` 4283×, `assert_nrf_callback` 3137×, `zdebug` 2058×, `nrf_gpio_pin_port_decode` 2028×, `get_ctime` 2010× | ✅ |
| HA | top 15 | `String::String` 1495×, `String::concat` 1079×, `String::String(const char*)` 606×, `isSSO` 551×, `xPortSetInterruptMaskFromISR` 390× | ✅ |

#### T8 — `find_dead_code`

| Projekt | Dotaz | Výsledek | Stav |
|---------|-------|----------|:----:|
| zbox | top 15 | Samé Mbed OS/SDK konstruktory: `AT24Mac`, `ATCmdParser`, `AdvertisingDataBuilder`, `AnalogIn`, `AppManager` | ⚠️ |
| HA | top 10 | Samé knihovní konstruktory: `AsyncServer` (4×), `CoilData` (2×), `DallasTemperature`, `EthernetClient` | ⚠️ |

#### T9 — `find_call_path`

| Projekt | Dotaz | Výsledek | Stav |
|---------|-------|----------|:----:|
| zbox | `main` → `uart_send` | "No path found within depth 10" — cesta by měla existovat přes `zmodem.init()` | ❌ |
| zbox | `_process_service_cmd` → `_lock_open_cmd` | depth 2: `_process_service_cmd → lock_open → _lock_open_cmd` | ✅ |
| HA | `loop` → `modbus_poll` | depth 1: `loop → modbus_poll` | ✅ |

#### T10 — `find_wrapper_callers`

| Projekt | Dotaz | Výsledek | Stav |
|---------|-------|----------|:----:|
| zbox | `UART_DRIVER` | "No methods found" — třída v projektu neexistuje, Mbed používá jiné názvy | ✅ |

#### T11 — `find_all_callers_recursive`

| Projekt | Dotaz | Výsledek | Stav |
|---------|-------|----------|:----:|
| zbox | `zbox::LBPwr::_lock_open_cmd` (depth 3) | 13 callerů: `lock_open` (depth 1), `_process_service_cmd`/`dispatch_open_slot`/`kb_open_disp` atd. (depth 2), `_process_dispatch_cmd`/`_thread_lb_func` atd. (depth 3) | ✅ |
| HA | `boiler_control` (depth 3) | 4 volající: `ha_update` (depth 1), `loop` (depth 2), `HAMqtt::loop`/`loopTask` (depth 3) | ✅ |

#### T12 — `find_callees_recursive`

| Projekt | Dotaz | Výsledek | Stav |
|---------|-------|----------|:----:|
| zbox | `zbox::AppManager::run` (depth 3) | "No callees found" — **podezřelé**, `run()` by měl něco volat | ❌ |
| zbox | `zbox::LBPwr::lock_open` (depth 2) | 26 callees: `_lock_open_cmd`, `_get_door_status_slot`, `zdebug`, `RS485::send`, `read`, `write` atd. | ✅ |
| HA | `loop` (depth 2) | 21 callees: `decround`, `digitalWrite`, `getTemp`, `ha_update`, `modbus_poll`, `boiler_control`, `printf` atd. | ✅ |

#### T13 — `find_references`

| Projekt | Dotaz | Výsledek | Stav |
|---------|-------|----------|:----:|
| zbox | `zbox::AppManager::run` | "No references found" — **stejný problém jako T12**, `run` existuje a je volána | ❌ |
| HA | `rt_data` | 22 referencí napříč `control.cpp`, `ha_mqtt.cpp`, `main.cpp`, `modbus.cpp`, `sensors.cpp` | ✅ |

#### T14 — `trace_data_flow`

| Projekt | Dotaz | Výsledek | Stav |
|---------|-------|----------|:----:|
| zbox | `CommandDoorControl` → `zbox::LBPwr::_lock_open_cmd` | 0/1 source functions reach target. `_get_door_status_slot` nalezena jako source, ale `reachable: false` | ⚠️ |

---

### 3.3 Fulltextové vyhledávání (search_code)

#### T15 — `search_code` relevantní dotazy

| Projekt | Dotaz | Výsledek | Stav |
|---------|-------|----------|:----:|
| zbox | `uart init` | 10 výsledků: `nrfx_uarte_init`, `PalUartDeInit`, `WsfBufIoUartInit`, `PalUartInit`, `nrfx_uarte_tx`, `nrfx_uarte_rx` atd. | ✅ |
| zbox | `bluetooth advertising` | 10 výsledků: `advertising_filter_policy_t`, `adv_data_type_t`, `GapAdvertisingReportEvent`, `advertising_event_properties_t` atd. | ✅ |
| zbox | `crc check` (kind=function) | 1 výsledek: `fds_record_open` s CRC check v docstringu | ✅ |
| HA | `modbus temperature` | 10 výsledků přes fallback `name_tokens_like`: `modbus_setup`, `modbus_ip`, `lastUpdateTemperature`, `ModbusClientTCPasync` atd. | ⚠️ |
| HA | `wifi mqtt home assistant` | 1 výsledek: `HAMqtt::HAMqtt` konstruktor s plným docstringem | ✅ |

#### T16 — `search_code` nerelevantní / okrajové dotazy

| Projekt | Dotaz | Výsledek | Stav |
|---------|-------|----------|:----:|
| zbox | `python django rest` | 4 výsledky přes `individual_terms` fallback: `greentea_notify_hosttest`, `restart`, `map_restart`, `err_restore` | ✅ |
| zbox | `door lock` | 10 výsledků přes `name_tokens_like` fallback: `DOOR_CONTROL`, `DOOR_STATUS`, `MutexSectionLock`, `lock`, `unlock` | ⚠️ |
| HA | `rocket launch telemetry` | `[]` — správně prázdné | ✅ |

---

### 3.4 Sémantické vyhledávání (semantic_search)

#### T17 — `semantic_search` vysoce relevantní koncepty

| Projekt | Dotaz | Top skóre | Výsledky | Stav |
|---------|-------|:---------:|----------|:----:|
| zbox | `parcel locker door control state machine` | 0.76 | `get_door_state`, `set_door_state`, `_get_door_status_slot`, `check_door_state`, `process_door_status`, `_lock_open_cmd`, `dispatch_open_slot` | ✅ |
| zbox | `battery power management sleep modes` | 0.68 | `is_sleep`, `sleep`, `wakeup`, `periodicCheck`, `charger_periodic_process` | ✅ |
| HA | `heating boiler temperature control thermostat` | 0.75 | `boiler_control`, `setCurrentAuxState`, `setAuxState`, `setCurrentTemperature`, `onTargetTemperatureCommand` | ✅ |

#### T18 — `semantic_search` zcela nerelevantní koncepty

| Projekt | Dotaz | Výsledek | Stav |
|---------|-------|----------|:----:|
| zbox | `machine learning neural network training` | `RunningMedian::predict` (0.56), `DWT_Type` (0.55) — nízké skóre, správně marginální | ✅ |
| zbox | `web browser html css rendering` | `WDT` (0.64), `swdt_check` (0.63) — WDT ≠ web, false positive nad threshold 0.55 | ⚠️ |
| HA | `blockchain cryptocurrency mining` | "No symbols matched with similarity > 0.55" — správně | ✅ |

---

### 3.5 Smart search a explain

#### T19 — `smart_search`

| Projekt | Dotaz | Výsledek | Stav |
|---------|-------|----------|:----:|
| zbox | `how does the modem connect and send data` | Generované FTS5: `modem_connect*`, `network_attach*`, `send_data*`, `data_send*`. Výsledky: BLE-related (`l2cCocSendData`, `lctrSendDataLengthReq`, `lctrSendDataLengthPdu`). Chybí `ZMODEM`, `ModemMsgManager` atd. | ❌ |

#### T20 — `explain_symbol`

| Projekt | Dotaz | Výsledek | Stav |
|---------|-------|----------|:----:|
| zbox | `zbox::LBPwr::_lock_open_cmd` | Přesné vysvětlení: RS485, checksum, otevírání zámku, error handling, zapojení do systému | ✅ |
| HA | `boiler_control` | Přesné vysvětlení: battery management, temperature control, relay logic, hysteresis, periodic execution | ✅ |

---

## 4. Souhrn chyb a degradací

### ❌ Chyby (2 potvrzené, 3 falešné poplachy)

| ID | Nástroj | Projekt | Problém | Závažnost | Status |
|----|---------|---------|---------|:---------:|:------:|
| ~~BUG-1~~ | ~~`find_references`~~ | zbox | ~~`AppManager::run` — "No references"~~ → **FALEŠNÝ POPLACH:** symbol neexistuje | — | ❌ Zamítnuto |
| ~~BUG-2~~ | ~~`find_callees_recursive`~~ | zbox | ~~`AppManager::run` — "No callees"~~ → **FALEŠNÝ POPLACH:** symbol neexistuje | — | ❌ Zamítnuto |
| ~~BUG-3~~ | ~~`find_call_path`~~ | zbox | ~~`main → uart_send`~~ → **FALEŠNÝ POPLACH:** `uart_send` neexistuje | — | ❌ Zamítnuto |
| **BUG-4** | `smart_search` | zbox | "how does modem connect" generuje FTS5 termy, které vedou k BLE místo modem výsledkům | 🟡 Střední | 🔲 Čeká |
| **BUG-5** | Chybové hlášky (6 nástrojů) | oba | `find_callers`, `find_references`, `find_all_callers_recursive`, `find_callees_recursive`, `find_call_path`, `find_wrapper_callers` nerozlišovaly "symbol not found" vs "symbol found but empty" | 🟢 Nízká → 🟡 | ✅ **Hotovo** |

### ⚠️ Degradace / úzká místa (8)

| ID | Nástroj | Projekt | Problém | Návrh | Priorita |
|----|---------|---------|---------|-------|:--------:|
| **DEG-1** | `get_symbol_context` | zbox | 48 template callerů u `MbedCRC` — zahlcující, všechny stejný konstruktor | Deduplikovat template instantiation v callerech | 2 |
| **DEG-2** | `find_dead_code` | oba | 100% false positives z SDK knihoven (`AT24Mac`, `AsyncServer`, …) | Výchozí `exclude_paths` filtr podle `source_roots` | 1 |
| **DEG-3** | `search_code` | oba | Některé relevantní dotazy padají do fallbacku místo přímé FTS5 (`door lock`, `modbus temperature`) | Prověřit FTS5 tokenizér — možná "door" a "lock" jako separátní tokeny nefungují | 2 |
| **DEG-4** | `semantic_search` | zbox | "web browser" → `WDT` (0.64) — irelevantní výsledek nad threshold 0.55 | Zvážit zvýšení výchozího thresholdu nebo source-aware penalizaci | 3 |
| **DEG-5** | `lookup_symbol` | zbox | `uart_init` exact selhal, `_did_you_mean` správně navrhlo `nrfx_uarte_init` | Auto-fallback na `_did_you_mean` top result | 4 |
| **DEG-6** | `trace_data_flow` | zbox | Nekonzistentní výstup, metadata `_summary` vs `_experimental` | Stabilizovat API nebo označit experimental v docstringu | 4 |
| **DEG-7** | `get_symbol_context` | oba | Framework/SDK callees tvoří většinu výstupu (např. `setup` → 20+ framework callees z 31) | Přidat `exclude_paths` parametr pro filtrování SDK | 3 |
| **DEG-8** | `find_hotspots` | oba | Top položky jsou výhradně framework (`String`, `callback`, `xPort*`) | Filtrovat podle `source_roots` nebo přidat parametr `project_only` | 3 |

---

## 5. Plán oprav — aktuální stav

### ✅ Fáze 1: Chybové hlášky — HOTOVO (2025-06-19)

**Implementováno v `src/fw_context_mcp/mcp/server.py`:**
- `_references_result()` (ř. ~1040): přidán `_lookup_definition()` guard, odebráno "Check the name"
- `find_all_callers_recursive()` (ř. ~1198): přidán guard
- `find_callees_recursive()` (ř. ~1232): přidán guard
- `find_call_path()` (ř. ~1162–1164): přidány guardy pro oba symboly
- `find_wrapper_callers()` (ř. ~1299): přidán guard pro class_name

Všech 6 nástrojů teď používá stejný pattern jako `get_source`/`explain_symbol`/`get_symbol_context`:
- Symbol nenalezen → `{"error": "Symbol not found: {name}"}`
- Symbol nalezen, bez výsledků → `{"info": "No X found for '{name}'."}`

### 🔲 Fáze 2: Zbývající problémy

| # | ID | Co | Priorita |
|---|----|----|:--------:|
| 1 | BUG-4 | `smart_search` — LLM generuje zavádějící FTS5 termy | 🟡 |
| 2 | DEG-2 | `find_dead_code` — 100% false positives z SDK bez `exclude_paths` | 🟡 |
| 3 | DEG-1 | `get_symbol_context` — template caller deduplikace | 🟢 |
| 4 | DEG-3 | `search_code` — FTS5 fallback kvalita ("door lock", "modbus temperature") | 🟢 |
| 5 | DEG-4 | `semantic_search` — false positive "web browser" → WDT (0.64) | 🟢 |
| 6 | DEG-5 | `lookup_symbol` — auto-fallback na `_did_you_mean` | 🟢 |
| 7 | DEG-6 | `trace_data_flow` — označit jako experimental | 🟢 |
| 8 | DEG-7 | `get_symbol_context` — filtrování SDK callees | 🟢 |
| 9 | DEG-8 | `find_hotspots` — filtrování framework položek | 🟢 |

---

## 6. Verifikace

Po každé fázi:
1. Spustit `ruff check src/ tests/` + `mypy src/`
2. Spustit `python3 -m pytest tests/ -x -q`
3. Ručně ověřit opravený scénář na obou projektech
4. Aktualizovat tento plán — označit vyřešené položky

---

## 7. Rizika

- **BUG-1/2** může vyžadovat změnu v libclang parsování referencí — možné regrese
- **DEG-2** změna výchozího chování `find_dead_code` může překvapit uživatele, kteří spoléhají na aktuální výstup
- Schema change v `refs` tabulce by vyžadoval reindexaci — nutno řešit migrací
