from __future__ import annotations

"""Built-in dispatch bridges: callee QN → dispatch entry point QN.

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

Type-erased ISR functions
--------------------------

Some ISR registration APIs type-erase the handler argument
(e.g. ``NVIC_SetVector(IRQn, (uint32_t)handler)``), making the function
pointer invisible to libclang.  ``_TYPE_ERASED_ISR_FUNCTIONS`` maps such
function names to the (0-based) argument index that carries the handler.
The source-line fallback in ``symbols.py`` uses this to extract the handler
name from the raw source line.
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

# Type-erased ISR registration functions: function name → handler arg index (0‑based).
# These APIs cast the handler to an integer type so libclang cannot see the
# function‑pointer assignment.  The source‑line fallback uses this map to
# extract the handler name from the raw source line and create an fp_assignment.
_TYPE_ERASED_ISR_FUNCTIONS: dict[str, int] = {
    # ── ARM CMSIS / Nordic / STM32 ──────────────────────────────────
    "NVIC_SetVector":      1,   # NVIC_SetVector(IRQn, (uint32_t)handler)
    "__NVIC_SetVector":    1,
}

# Unqualified dispatch method names — used for fast pre-filtering in
# source-line fallback (``symbols.py:_run_source_line_fallback``).
_DISPATCH_METHOD_NAMES: frozenset[str] = frozenset({
    qn.rsplit("::", 1)[-1] for qn in _DISPATCH_ENTRY_POINTS
})
