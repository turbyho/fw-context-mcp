from __future__ import annotations

"""Built-in dispatch bridges: callee QN → dispatch entry point QN.

WHY dispatch bridges: libclang's call graph is static — it sees
``EventQueue::call_every(callback)`` but has no way to know that
``callback`` will be invoked by ``EventQueue::dispatch_forever`` during
the event loop.  These bridges inject synthetic edges that connect
dispatch-registration APIs to their event-loop entry points, making
the static call graph correctly reflect the runtime control flow.

When a function calls a dispatch-registration method
(e.g. ``EventQueue::call_every``), a synthetic ``ref_kind='dispatch'`` edge
is created from the dispatch entry point (e.g. ``EventQueue::dispatch_forever``)
to the callback target.  This lets the call graph traverse through event loops
and thread starts.

Used by ``symbols.py`` (detection phase — creates ``PendingDispatch`` records)
and ``_postprocess.py`` (resolution phase — resolves them into ``refs`` rows).

At resolution time, user-defined entries from ``[call_graph.dispatch_bridges]``
in ``.fw-context/config.toml`` are merged on top.  Entries whose entry point
symbol does not exist in the index are silently skipped — the map can safely
list all supported platforms in one place without causing errors for
non-matching projects.
"""

# Map: dispatch-registration API qualified name → dispatch entry point QN.
_DISPATCH_ENTRY_POINTS: dict[str, str] = {
    # ── mbed-os ─────────────────────────────────────────────────────
    "events::EventQueue::call":         "events::EventQueue::dispatch_forever",
    "events::EventQueue::call_every":   "events::EventQueue::dispatch_forever",
    "events::EventQueue::call_in":      "events::EventQueue::dispatch_forever",
    "rtos::Thread::start":              "rtos::Thread::_thread_start",
    "mbed::Timeout::attach":            "mbed::Timeout::_timeout_handler",
    "mbed::Ticker::attach":             "mbed::Ticker::_ticker_handler",
    # ── Zephyr ──────────────────────────────────────────────────────
    "k_work_submit":                    "z_work_q_main",
    "k_work_schedule":                  "z_work_q_main",
    "k_work_submit_to_queue":           "z_work_q_main",
    # ── FreeRTOS ────────────────────────────────────────────────────
    "xTimerStart":                      "prvTimerCallback",
    "xTimerStartFromISR":               "prvTimerCallback",
    # ── ESP-IDF ─────────────────────────────────────────────────────
    "esp_intr_alloc":                   "<esp_intr_dispatch>",
    "intr_matrix_set":                  "<esp_intr_dispatch>",
    # ── STM32 HAL ───────────────────────────────────────────────────
    "HAL_UART_RegisterCallback":        "HAL_UART_IRQHandler",
    "HAL_SPI_RegisterCallback":         "HAL_SPI_IRQHandler",
    "HAL_TIM_RegisterCallback":         "HAL_TIM_IRQHandler",
    "HAL_I2C_RegisterCallback":         "HAL_I2C_EV_IRQHandler",
    # ── FreeRTOS (extended) ─────────────────────────────────────────
    "xTaskCreate":                      "prvTaskExitError",
    "xTaskCreateStatic":                "prvTaskExitError",
}
