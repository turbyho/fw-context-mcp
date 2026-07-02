"""Anotované dotazy a symbol pool pro evaluaci RRF fusion.

Každý dotaz definuje:
- query: string dotazu
- category: exact / concept / mixed / edge
- relevant: set of (name, file_path) — ground truth (co by se mělo najít)
- fts5_ranked: list of names — co by FTS5 vrátilo (simulované pořadí)
- vec_ranked: list of names — co by vektorový retrieval vrátil (simulované)

Symbol pool definuje všechny symboly, na které se ranked listy odkazují.
"""

from __future__ import annotations

# ── Symbol pool ───────────────────────────────────────────────────────────
# Každý symbol má name, file_path, kind, is_definition a další metadata.
# Používáno k převodu jmen v ranked listech na plné záznamy.

SYMBOL_POOL: dict[str, dict] = {
    # === Projektový kód (src/) ===
    "uart_init": {
        "name": "uart_init",
        "file_path": "src/drivers/uart.cpp",
        "kind": "function",
        "is_definition": True,
        "signature": "void uart_init(UART_DRIVER *uart, int baudrate)",
    },
    "uart_hw_init": {
        "name": "uart_hw_init",
        "file_path": "src/drivers/uart_hw.cpp",
        "kind": "function",
        "is_definition": True,
        "signature": "static void uart_hw_init(int port)",
    },
    "uart_read": {
        "name": "uart_read",
        "file_path": "src/drivers/uart.cpp",
        "kind": "function",
        "is_definition": True,
        "signature": "int uart_read(UART_DRIVER *uart, uint8_t *buf, int len)",
    },
    "uart_write": {
        "name": "uart_write",
        "file_path": "src/drivers/uart.cpp",
        "kind": "function",
        "is_definition": True,
        "signature": "int uart_write(UART_DRIVER *uart, const uint8_t *data, int len)",
    },
    "uart_irq_handler": {
        "name": "uart_irq_handler",
        "file_path": "src/drivers/uart.cpp",
        "kind": "function",
        "is_definition": False,
        "signature": "void uart_irq_handler(void)",
    },
    "modem_send_at": {
        "name": "modem_send_at",
        "file_path": "src/modem/at_driver.cpp",
        "kind": "function",
        "is_definition": True,
        "signature": "int modem_send_at(const char *cmd)",
    },
    "modem_data_tx": {
        "name": "modem_data_tx",
        "file_path": "src/modem/data.cpp",
        "kind": "method",
        "is_definition": True,
        "signature": "int ModemDriver::data_tx(const uint8_t *data, int len)",
    },
    "modem_connect": {
        "name": "modem_connect",
        "file_path": "src/modem/connect.cpp",
        "kind": "function",
        "is_definition": True,
        "signature": "int modem_connect(const char *apn)",
    },
    "modem_init": {
        "name": "modem_init",
        "file_path": "src/modem/init.cpp",
        "kind": "function",
        "is_definition": True,
        "signature": "void modem_init(ModemConfig *cfg)",
    },
    "send_packet": {
        "name": "send_packet",
        "file_path": "src/network/packet.cpp",
        "kind": "function",
        "is_definition": True,
        "signature": "int send_packet(Socket *sock, Packet *pkt)",
    },
    "ble_connection_handler": {
        "name": "ble_connection_handler",
        "file_path": "src/ble/conn.cpp",
        "kind": "function",
        "is_definition": True,
        "signature": "void ble_connection_handler(uint16_t handle)",
    },
    "on_ble_connect": {
        "name": "on_ble_connect",
        "file_path": "src/ble/events.cpp",
        "kind": "method",
        "is_definition": True,
        "signature": "void BLEManager::on_ble_connect(const BLEConnection *conn)",
    },
    "ble_gap_init": {
        "name": "ble_gap_init",
        "file_path": "src/ble/gap.cpp",
        "kind": "function",
        "is_definition": True,
        "signature": "int ble_gap_init(GAPConfig *cfg)",
    },
    "ble_disconnect": {
        "name": "ble_disconnect",
        "file_path": "src/ble/conn.cpp",
        "kind": "function",
        "is_definition": True,
        "signature": "int ble_disconnect(uint16_t handle)",
    },
    "wdt_refresh": {
        "name": "wdt_refresh",
        "file_path": "src/hal/wdt.cpp",
        "kind": "function",
        "is_definition": True,
        "signature": "void wdt_refresh(void)",
    },
    "wdt_init": {
        "name": "wdt_init",
        "file_path": "src/hal/wdt.cpp",
        "kind": "function",
        "is_definition": True,
        "signature": "void wdt_init(int timeout_ms)",
    },
    "flash_write": {
        "name": "flash_write",
        "file_path": "src/drivers/flash.cpp",
        "kind": "function",
        "is_definition": True,
        "signature": "int flash_write(uint32_t addr, const uint8_t *data, int len)",
    },
    "flash_read": {
        "name": "flash_read",
        "file_path": "src/drivers/flash.cpp",
        "kind": "function",
        "is_definition": True,
        "signature": "int flash_read(uint32_t addr, uint8_t *buf, int len)",
    },
    "flash_erase": {
        "name": "flash_erase",
        "file_path": "src/drivers/flash.cpp",
        "kind": "function",
        "is_definition": True,
        "signature": "int flash_erase(uint32_t addr, int sectors)",
    },
    "timer_init": {
        "name": "timer_init",
        "file_path": "src/hal/timer.cpp",
        "kind": "function",
        "is_definition": True,
        "signature": "void timer_init(void)",
    },
    "led_toggle": {
        "name": "led_toggle",
        "file_path": "src/drivers/led.cpp",
        "kind": "function",
        "is_definition": True,
        "signature": "void led_toggle(int led_id)",
    },
    "i2c_read": {
        "name": "i2c_read",
        "file_path": "src/drivers/i2c.cpp",
        "kind": "function",
        "is_definition": True,
        "signature": "int i2c_read(uint8_t addr, uint8_t *buf, int len)",
    },
    "i2c_write": {
        "name": "i2c_write",
        "file_path": "src/drivers/i2c.cpp",
        "kind": "function",
        "is_definition": True,
        "signature": "int i2c_write(uint8_t addr, const uint8_t *data, int len)",
    },
    # === SDK/vendor kód (mbed-os/) ===
    "serial_init": {
        "name": "serial_init",
        "file_path": "mbed-os/drivers/Serial.cpp",
        "kind": "method",
        "is_definition": True,
        "signature": "void Serial::init(PinName tx, PinName rx)",
    },
    "I2C_write": {
        "name": "I2C_write",
        "file_path": "mbed-os/drivers/I2C.cpp",
        "kind": "method",
        "is_definition": True,
        "signature": "int I2C::write(int addr, const char *data, int length)",
    },
    "I2C_read": {
        "name": "I2C_read",
        "file_path": "mbed-os/drivers/I2C.cpp",
        "kind": "method",
        "is_definition": True,
        "signature": "int I2C::read(int addr, char *data, int length)",
    },
    "Ticker_attach": {
        "name": "Ticker_attach",
        "file_path": "mbed-os/hal/Ticker.cpp",
        "kind": "method",
        "is_definition": True,
        "signature": "void Ticker::attach(Callback<void()> func, float t)",
    },
}

# ── Anotované dotazy ──────────────────────────────────────────────────────

QUERY_CASES: list[dict] = [
    # ── EXACT queries ────────────────────────────────────────────────
    {
        "query": "uart_init",
        "category": "exact",
        "relevant": {("uart_init", "src/drivers/uart.cpp"), ("uart_hw_init", "src/drivers/uart_hw.cpp")},
        "fts5_ranked": ["uart_init", "uart_hw_init", "uart_read", "uart_write", "uart_irq_handler", "serial_init", "timer_init", "led_toggle"],
        "vec_ranked": ["uart_hw_init", "uart_read", "uart_init", "serial_init", "uart_irq_handler", "i2c_read", "I2C_write", "Ticker_attach"],
    },
    {
        "query": "flash_erase",
        "category": "exact",
        "relevant": {("flash_erase", "src/drivers/flash.cpp"), ("flash_write", "src/drivers/flash.cpp"), ("flash_read", "src/drivers/flash.cpp")},
        "fts5_ranked": ["flash_erase", "flash_write", "flash_read", "timer_init", "led_toggle", "wdt_refresh"],
        "vec_ranked": ["flash_write", "flash_erase", "serial_init", "flash_read", "i2c_read", "I2C_write"],
    },
    {
        "query": "wdt_refresh",
        "category": "exact",
        "relevant": {("wdt_refresh", "src/hal/wdt.cpp"), ("wdt_init", "src/hal/wdt.cpp")},
        "fts5_ranked": ["wdt_refresh", "wdt_init", "timer_init", "led_toggle", "flash_write"],
        "vec_ranked": ["timer_init", "wdt_refresh", "Ticker_attach", "wdt_init", "serial_init"],
    },
    # ── CONCEPT queries ──────────────────────────────────────────────
    {
        "query": "modem send data",
        "category": "concept",
        "relevant": {
            ("modem_send_at", "src/modem/at_driver.cpp"),
            ("modem_data_tx", "src/modem/data.cpp"),
            ("send_packet", "src/network/packet.cpp"),
        },
        "fts5_ranked": [
            "modem_send_at", "modem_connect", "send_packet", "modem_init",
            "serial_init", "uart_write", "modem_data_tx", "led_toggle",
        ],
        "vec_ranked": [
            "send_packet", "modem_data_tx", "modem_send_at", "serial_init",
            "uart_read", "i2c_read", "I2C_write", "modem_connect",
        ],
    },
    {
        "query": "BLE connection handler",
        "category": "concept",
        "relevant": {
            ("ble_connection_handler", "src/ble/conn.cpp"),
            ("on_ble_connect", "src/ble/events.cpp"),
            ("ble_gap_init", "src/ble/gap.cpp"),
            ("ble_disconnect", "src/ble/conn.cpp"),
        },
        "fts5_ranked": [
            "ble_connection_handler", "ble_disconnect", "on_ble_connect",
            "ble_gap_init", "uart_init", "serial_init", "led_toggle",
        ],
        "vec_ranked": [
            "on_ble_connect", "ble_gap_init", "ble_disconnect",
            "ble_connection_handler", "serial_init", "Ticker_attach", "timer_init",
        ],
    },
    {
        "query": "i2c communication",
        "category": "concept",
        "relevant": {
            ("i2c_read", "src/drivers/i2c.cpp"),
            ("i2c_write", "src/drivers/i2c.cpp"),
        },
        "fts5_ranked": [
            "i2c_read", "i2c_write", "I2C_write", "I2C_read", "uart_read", "uart_write", "serial_init", "led_toggle",
        ],
        "vec_ranked": [
            "I2C_write", "i2c_write", "I2C_read", "i2c_read", "serial_init", "uart_read", "Ticker_attach", "uart_write",
        ],
    },
    # ── MIXED queries (exact + concept) ──────────────────────────────
    {
        "query": "flash memory write read",
        "category": "mixed",
        "relevant": {
            ("flash_write", "src/drivers/flash.cpp"),
            ("flash_read", "src/drivers/flash.cpp"),
            ("flash_erase", "src/drivers/flash.cpp"),
        },
        "fts5_ranked": ["flash_write", "flash_read", "flash_erase", "uart_write", "uart_read", "led_toggle", "timer_init"],
        "vec_ranked": ["flash_write", "flash_erase", "flash_read", "uart_write", "i2c_write", "serial_init", "uart_read"],
    },
    {
        "query": "timer init ticker",
        "category": "mixed",
        "relevant": {
            ("timer_init", "src/hal/timer.cpp"),
            ("wdt_init", "src/hal/wdt.cpp"),
        },
        "fts5_ranked": ["timer_init", "wdt_init", "Ticker_attach", "wdt_refresh", "uart_init", "led_toggle", "flash_write"],
        "vec_ranked": ["Ticker_attach", "timer_init", "wdt_init", "serial_init", "wdt_refresh", "uart_init", "i2c_read"],
    },
    # ── EDGE cases ──────────────────────────────────────────────────
    {
        "query": "neexistujici_funkce_xyz",
        "category": "edge",
        "relevant": set(),
        "fts5_ranked": ["timer_init", "led_toggle", "wdt_refresh", "flash_write"],
        "vec_ranked": ["serial_init", "Ticker_attach", "I2C_write", "timer_init"],
    },
    {
        "query": "uart_read_write_init_irq",
        "category": "edge",
        "relevant": {
            ("uart_read", "src/drivers/uart.cpp"),
            ("uart_write", "src/drivers/uart.cpp"),
            ("uart_init", "src/drivers/uart.cpp"),
            ("uart_irq_handler", "src/drivers/uart.cpp"),
        },
        "fts5_ranked": ["uart_read", "uart_write", "uart_init", "uart_hw_init", "uart_irq_handler", "serial_init", "timer_init"],
        "vec_ranked": ["uart_hw_init", "uart_read", "uart_init", "uart_irq_handler", "uart_write", "serial_init", "i2c_read"],
    },
]

# ── Helpers ───────────────────────────────────────────────────────────────


def resolve_results(names: list[str]) -> list[dict]:
    """Convert a ranked list of symbol names to full dict records."""
    results = []
    for name in names:
        if name in SYMBOL_POOL:
            results.append(dict(SYMBOL_POOL[name]))
    return results


def relevant_set(case: dict) -> set[tuple[str, str]]:
    """Extract relevant (name, file_path) set from a query case."""
    return case["relevant"]
