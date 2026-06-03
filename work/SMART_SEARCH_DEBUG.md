# smart_search Debug Plan

## Aktuální stav (commit b7b9cf6)

**Architektura:** three-phase
- **Phase 1a:** phrase search `"firstword otherword"` (limit 5/query)
- **Phase 1b:** individual word search (limit 8/word, max 20 uniq names, dedup by name, noise filter)
- **Phase 2:** Ollama s kontextem 15 reálných symbolů generuje 3-5 FTS5 termů
- **Phase 3:** finální search s refined/fallback termy

**Konfigurace:** model `qwen2.5-coder:14b`, num_ctx 8192, debug_log `~/.fw-context/llm-debug.jsonl`
**Editable install:** `~/.fw-context/.venv` → `~/.fw-context/src/src/` (primární dev)

---

## Iterační log

### V0 (original)
Single prompt bez kontextu.
```
["ble_conn*", "disconnect_handler", "callback_ble"]
```
- `callback_ble` — špatné pořadí slov

### V1 (two-phase)
Rough search + Ollama s reálnými symboly.
```
["ble_conn*", "connection_handle", "on_remote_connection_parameter"]
```
- Lepší prefixy, ale 2/3 bez wildcard

### V2 (dedup + noise filter)
Odstraněny duplicity (`ble` 3×) a `operator=`.
```
["connection_establish", "conn_disconnect*", "ble_connection_handler"]
["connection*", "ble*", "lctrConnEstablish*", "LL_ERROR_CODE*"]
```
- Střídavé výsledky, někdy moc široké termy

### V3 (vylepšený prompt)
"STRONGLY prefer stems with wildcard", "cover different patterns", 3-5 termů.
```
["ble_connect*", "disconnect*", "callback*", "connect_error*", "JRC_BLE*"]
```
- 4/5 s wildcard, ale `disconnect*` a `callback*` moc široké (chytají GPIO)

### V4 (phrase search)
Phase 1a s phrase search `"firstword otherword"`.
```
["connection_role*", "ble_connect*", "on_remote_connection_parameter*"]
```
- Všechny s wildcard, ale jen 3 termy, `on_remote_connection_parameter*` pořád moc specifické

**Trend:** zlepšuje se, ale `qwen2.5-coder:14b` je limitující faktor.

---

## Známé problémy

1. `qwen2.5-coder:14b` generuje jen 3 termy (ne 4-5) — zvážit silnější model (deepseek-coder, codestral)
2. Model generuje příliš specifické názvy i s wildcard (`on_remote_connection_parameter*` místo `on_connection*`)
3. Chybí disconnect-specific termy v generovaných výsledcích
4. Kontext v rough_samples obsahuje šum: generic `ble` field, `connection` field, GPIO symboly (`NRF_GPIO_PIN_INPUT_CONNECT`)
5. Phrase search funguje jen pro `firstword + otherword` — ne pro ostatní kombinace
6. Chybí relevance scoring rough výsledků (symbol s více query slovy by měl přednost)
7. FTS5 tokenizer nerozděluje camelCase — `onConnectionComplete` je jeden token, query `"on connect*"` ho nenajde

---

## Plán dalšího ladění

### A) Krátkodobě (bez změny modelu)
- Relevance scoring rough výsledků: symboly obsahující víc query slov ukázat první v kontextu
- Zvýšit rough_samples limit z 20 na 30 (víc diverzity)
- Přidat phrase search pro všechny dvojice content_words (ne jen firstword+other)
- Experimentovat s teplotou modelu (snížit pro konzistentnější výstup)
- Zkusit říct modelu přesně kolik termů chceme ("Generate exactly 4 terms")

### B) Střednědobě (změna modelu)
- Vyzkoušet deepseek-coder / codestral — pravděpodobně největší skok v kvalitě
- Zvětšit num_ctx (8192 → 16384) pro víc kontextu

### C) Dlouhodobě
- Vlastní relevance model pro rough výsledky (TF-IDF nad indexed files)
- Fuzzy matching pro camelCase → snake_case v query termech
- Cache LLM odpovědí pro opakované podobné dotazy
