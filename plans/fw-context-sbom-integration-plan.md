# Implementační plán: SBOM a vulnerability analýza pro `fw-context`

**Projekt:** `turbyho/fw-context-mcp`
**Typ dokumentu:** implementační plán (ukotvený do reálné codebase)
**Stav:** všechny fáze detailně specifikovány; připraveno k implementaci Fáze A
 **Datum:** 2026-08-04 (revize 11 — revize 10 + korekce z hlubokého legislativního výzkumu: CRA Annex I Part II(1) SBOM je **explicitní povinnost**; CRA support period min. 5 let (Art. 13(8)); AI Act timeline po AI Omnibusu (2026/1744); Machinery Reg. tech. dokumentace Annex IV; ENISA SRP bez API (11. 9. 2026) + reporting template; EUVD; CRA produktová klasifikace (Reg. 2025/2392) + conformity route; CISA 2026 ME v2.1 pole; Battery Reg. DPP 18. 2. 2027; PLD 9. 12. 2026)
 **Východiskové dokumenty:**
 - `plans/embedded-sbom-cookbook.md` (normativní reference — framework recepty, formáty, release gates; tento plán jej neduplikuje)
 - `plans/fw-context-local-vendored-components-recommendation.md` (normativní reference pro vendored komponenty — citováno jako „doporučení §26.x"; ukotveno na Z-Box ECB: mbed-os 6.17.0 interní fork, zcbor/FatFS vendored bez provenance, bootloader blob)
 - **CRA Implementation Guidance — C(2026) 5252** (27. 7. 2026): Commission guidance on the application of the Cyber Resilience Act; 67 praktických příkladů, flowcharty, support period guidance. Není právně závazná, ale představuje aktuální výklad Komise.
 - **Legislative Validation Report** (`plans/legislative-validation-report-2026-08-04.md`): předchozí průzkum regulačních požadavků — ESPR/DPP, PSTI, GPSR, sektorové regulace.
 - **Legislative Validation Report v2** (`plans/legislative-validation-report-2026-08-04-v2.md`): hluboký výzkum (~30 živých zdrojů, 2026-08-04) — korekce faktických chyb rev. 10, mezery G1–G11, automatizační příležitosti (SRP draft, CRA klasifikace, digitální DoC). Normativní pro tuto revizi.

---

## 1. Rozsah a cíle

Rozšířit `fw-context` o build-aware SBOM:

1. identifikace komponent z **aktivního buildu** (ne scan zdrojového stromu);
2. export CycloneDX JSON navázaný na konkrétní ELF/BIN přes SHA-256;
3. linkerová evidence (`linked`/`retained`) z GNU ld map a ELF;
4. externí vulnerability scanner (Grype) jako volitelný subprocess;
5. build-aware evidence pro posouzení aplikovatelnosti CVE;
6. člověkem schvalovaný VEX;
7. framework adaptéry (Mbed OS, Zephyr, PlatformIO, ESP-IDF) a release provenance;
8. provenance lokálně vendored komponent (doporučení): duální interní/upstream identita, source-tree hash jako fallback identita, scanner projection oddělená od compliance SBOM.

Vendored realita (Z-Box ECB): mbed-os je vložen přímo do repa (6.17.0 + lokální commit „local mbed-os" — Případ B interní fork), `lib/zcbor`, `lib/fat/ChaN`, `lib/tdbstore` jsou vendored snapshoty bez manifestu a bez zachované upstream vazby (Případ C — neznámý původ). Bez explicitní provenance vrstvy je výstup scanneru pouze heuristický.

Klíčová diferenciace: `declared → present → compiled → linked → retained → deployed`.
`fw-context` zná `compile_commands.json`, aktivní TUs, makra, include graph a `config_hash` — SBOM je další vrstva nad tímto indexem, nikoli obecný directory scanner.

### 1a. Legislativní kontext a hranice nástroje

**Primární regulace:**
- **CRA (Reg. (EU) 2024/2847):** v platnosti od 10. 12. 2024; reportovací povinnosti od 11. 9. 2026; plná účinnost od 11. 12. 2027. Vyžaduje essential cybersecurity requirements (Annex I Part I), vulnerability handling (Annex I Part II), technical documentation (Annex VII), reporting aktivně zneužívaných zranitelností přes ENISA SRP (Art. 14), CE marking, EU declaration of conformity a explicitní support period (Art. 13(8), **min. 5 let**). **SBOM je explicitní povinnost:** Annex I Part II(1) vyžaduje „software bill of materials in a commonly used and machine-readable format covering at the very least the top-level dependencies"; Annex VII 2(b) ho vyžaduje v technické dokumentaci; Annex VII(8) na odůvodněnou žádost MSA; Annex II(9) informace o přístupu k SBOM. Produktová klasifikace dle **Implementing Reg. (EU) 2025/2392** (technické popisy important/critical kategorií Annex III/IV) určuje conformity assessment route (Art. 32) a pole SRP template. EUVD (European Vulnerability Database) je nový identifikátor pro findings.
- **NIS2 (Dir. (EU) 2022/2555):** supply chain security pro 18 kritických sektorů (vč. „critical product manufacturing" — výroba elektronických a optických produktů); nevyžaduje specifický SBOM formát. Transpozice neúplná (CJEU referrals 7/2026), simplification návrh 1/2026.
- **RED (2014/53/EU) + Del. Reg. 2022/30:** cybersecurity požadavky pro radio equipment aplikovatelné od 1. 8. 2025 (odklad o 1 rok přes (EU) 2023/2444); **Del. Reg. (EU) 2026/339 (16. 2. 2026) ruší (EU) 2022/30 s účinkem od 11. 12. 2027** (přechod na CRA je nyní právně konkrétní). Pro WiFi/BLE/LoRa zařízení platí překryv RED→CRA. Harmonizované normy EN 18031-1/2/3 (network, privacy, fraud) v OJEU od 30. 1. 2025 (Implementing Decision (EU) 2025/138), beze změn 2026; pozor na publikační Notices (restriktivní klauzule nedávají presumption of conformity). RED dokumentace: EU DoC (Art. 17/Annex VI), tech. dokumentace (Art. 21/Annex V), uživatelské instrukce, CE, uchování 10 let. RED **nemá** incident reporting (až CRA Art. 14).
- **US EO 14028 / CISA 2026 SBOM Minimum Elements:** **CISA 2026 Minimum Elements v2.1** (29. 7. 2026, CISA+NSA+FBI+17 mezinárodních partnerů) nahrazuje NTIA 2021 a 2025 draft. Nová pole: **SBOM Author Signature**, SBOM Tool Name/Version, SBOM Version (SemVer), Data Format Name/Version, Generation Context, Component Hash Value/Algorithm, Component License, **Component Producer** (nahrazuje Supplier). Machine-processable: SPDX + CycloneDX (CycloneDX nyní ECMA-424, PURL ECMA-427); doporučena VEX/CSAF korelace; „avoid deprecated SBOM versions". EO 14028 §4(e) implementováno přes OMB M-22-18 (žádný FAR SBOM clause).
- **EU Data Act (Reg. (EU) 2023/2854):** od 12. 9. 2025 obecně aplikovatelný; přímý přístup k datům (Ch. II — raw i pre-processed data z IoT), B2B sharing (Ch. III), unfair terms (Ch. IV), cloud switching poplatky odstraněny 12. 1. 2027 (Ch. VI). Smart contracts implementing act (Art. 30(5)) dosud nepřijat. Dokumentace je převážně kontraktuální/informační (data description, clausule), ne technický soubor.
- **EU AI Act (Reg. (EU) 2024/1689) + AI Omnibus (Reg. (EU) 2026/1744, v platnosti 27. 7. 2026):** prohibice + AI literacy od 2. 2. 2025; GPAI od 2. 8. 2025; transparentnost (Art. 50) + vynucování od 2. 8. 2026; **high-risk Annex III od 2. 12. 2027** (posunuto Omnibusem); **high-risk Annex I od 2. 8. 2028** (posunuto Omnibusem); prohibition 9 (AI nudification) prosinec 2026. Embedded/edge AI (TinyML) **není high-risk per se** — jen jako safety komponenta Annex I produktu nebo v Annex III use-case. High-risk dokumentace: Annex IV technical documentation, EU DoC (Art. 47), CE (Art. 48), EU database registrace, QMS (Art. 17), post-market monitoring (Art. 72).
- **EU Machinery Regulation (Reg. (EU) 2023/1230):** nahrazuje Machinery Directive 2006/42/EC; plná aplikovatelnost od 20. 1. 2027. Kyberbezpečnostní EHSR v **Annex III** (§1.1.9 ochrana proti korupci, §1.2.1 bezpečnost/robustnost řídicích systémů); **technická dokumentace = Annex IV** (Art. 21), uchování ≥ 10 let, zdrojový kód na odůvodněnou žádost. **EUCC presumption (Art. 29(9))**: stroj certifikovaný dle Reg. 2019/881 = shoda s §1.1.9/§1.2.1. Stroj se samovyvíjejícím ML bezpečnostním chováním = safety komponenta v Annex I → povinné third-party posouzení.
- **Standardy:** CycloneDX 1.7 = ECMA-424 (2025-12-10); PURL = ECMA-427; SPDX 3.1 (RC 1/2026, build/hardware/supply-chain profily) = ISO/IEC 5962:2021 — explicitní exporter ve Fázi F; OpenChain ISO 5230/18974 = best-practice, ne povinnost.
- **NIST SSDF (SP 800-218 v1.1):** PS.3.2 vyžaduje provenance data — provenance vrstva plánu to pokrývá. Žádná v2.0; SP 800-218A (GenAI profile) existuje.
- **CRA Implementation Guidance (C(2026) 5252, 27. 7. 2026):** poskytuje 67 příkladů, flowcharty; **SBOM je explicitní CRA požadavek (Annex I Part II(1)), guidance řeší jeho praktickou aplikaci** — due diligence (Art. 13(5)) a technickou dokumentaci (Annex VII) rozšiřuje.
- **EU Product Liability Directive (Dir. (EU) 2024/2853):** transpoziční lhůta i aplikace na produkty na trhu **od 9. 12. 2026**. Software/firmware = produkt; defektní může být i post-market update (produkt zůstává pod kontrolou výrobce, dokud dodává updates); kyberzranitelnost porušující safety-relevantní požadavky = faktor defectu; řetězec EU liable person (manufacturer → importer → authorized rep → fulfilment); evidence disclosure → presumption of defectiveness (Art. 9); 3/10/25 let limitation. SBOM + VEX + evidence report vytváří **liability trail** (verze/update historie, test logy) — primární obrana výrobce.
- **ESPR / Digital Product Passport (Reg. (EU) 2024/1781):** v platnosti od 18. 7. 2024. **DPP Registry v provozu od 20. 7. 2026** (IR 2026/1778, Decision 2026/1736 — 6/8 harmonizovaných norem JTC24). Working Plan 16. 4. 2025: energy-related products 2026–2029, **ICT/elektronika DPP 2029**. **Battery Regulation (Reg. (EU) 2023/1542): DPP povinnost od 18. 2. 2027** pro EV/LMT/home-storage/industriální baterie — **dříve než ESPR elektronika**; produkty s bateriemi jsou v rozsahu. SBOM data (komponenty, materiály, `recycled_content_pct`, `hazardous_substances`) napájejí DPP. Fáze G: DPP exportér — **priorita battery (18. 2. 2027), pak ESPR elektronika (2029)**.
- **UK PSTI Act 2022 + Regulations 2023 (SI 2023/1007):** účinné od 29. 4. 2024. 3 požadavky (unikátní hesla, vulnerability disclosure kontakt, **published defined support period** — bez statutárního 5letého minima; SoC se uchovává 10 let nebo po dobu support period, dle delšího). **SI 2025/1267 (4. 12. 2025): SG CLS (libovolná úroveň) a JP JC-STAR STAR-1 = deemed PSTI compliance** (mutual recognition). Vynucování OPSS: vzorek 82 zařízení → 75 % neshoda; pokuty až £10m / 4 % obratu. Založeno na ETSI EN 303 645. UKCA/CE jsou pro GB obě platné; UKCA label relief do 31. 12. 2027.
- **EU General Product Safety Regulation — GPSR (Reg. (EU) 2023/988):** plně aplikovatelná od 13. 12. 2024. Traceabilita (produkt/výrobce), **EU-based responsible economic operator**, informace o rizicích uchovávat 10 let / traceability 6 let, Safety Business Gateway, recall. Kyberzranitelnost = bezpečnostní aspekt (recitaly 25–26); podstatná digitální modifikace → nové posouzení.
- **EU Cybersecurity Act — EUCC certifikace:** EUCC (Common Criteria-based, Implementing Reg. (EU) 2024/482, aplikace od 2/2025, novela 2025/2462) umožňuje pro CRA Class II produkty nahradit third-party conformity assessment; **CRA–EUCC presumption delegated act očekáván Q4 2026**. fw-context SBOM + VEX výstupy = podklad pro EUCC security target.
- **CISA SBOM for AI — Minimum Elements (G7, 12. 5. 2026):** mezinárodní konsenzus G7 (CISA + BSI + ACN/ANSSI/CSE/NCSC-UK/NCO-JP) pro AI-model SBOM. **7 klastrů**: metadata, system-level properties, models, **datasets (provenance, sensitivity)**, infrastructure, **security properties (security controls, compliance)**, **KPI (performance metrics)**. fw-context AI metadata pokrývá část; rozšíření viz §4.1 (`dataset`, `ai_security_controls`, `ai_performance_metrics`).
- **CRA Art. 24 — open-source software stewardi:** light-touch režim pro OSS stewards (cybersecurity policy, spolupráce s MSA, reporting aktivně zneužívaných zranitelností/incidentů); bez CE, bez pokut (Art. 64(10)). fw-context (sám OSS) může sloužit i stewards compliance.

**Hranice nástroje — co fw-context NEdělá:**

| Odpovědnost | Nástroj | Člověk / proces |
|---|---|---|
| Generování SBOM + VEX + findings | ✅ `sbom generate`, `vuln scan`, `vex export` | — |
| ENISA SRP reporting (Art. 14) | ⚠️ `vuln report-srp` generuje **předvyplněný draft** (24h/72h/final template) z findings+VEX+SBOM — SRP **nemá API** (potvrzeno ENISA) | Člověk vloží draft do SRP (11. 9. 2026) |
| EUVD reference | ✅ `euvd_id` v findings (`vuln show`/`vuln report-srp`) | — |
| CRA produktová klasifikace + conformity route | ✅ `[product] cra_class/cra_category` → `sbom check` reportuje route (Module A / EU-type / full QA / EUCC) | Výrobce potvrdí klasifikaci |
| CE marking + EU declaration of conformity | ⚠️ `sbom export doc` (Fáze G) — předvyplněné podklady pro EU DoC | Výrobce podepíše DoC |
| Conformity assessment (Annex VIII) | ⚠️ `sbom check` reportuje požadovanou route | Výrobce / notified body |
| Technická dokumentace (Annex VII) | ⚠️ SBOM je povinná součást (Annex VII 2(b)), ne náhrada | Výrobce sestaví kompletní dokumentaci |
| Rozhodnutí o aplikovatelnosti CVE | ⚠️ fw-context navrhuje (D1) | Člověk schvaluje VEX assessment |
| Podepisování artefaktů + SBOM (CISA ME Author Signature) | ⚠️ SHA256SUMS generátor + signing hook | Externí nástroj (cosign/gpg) |
| Data Act — dokumentace datových toků | ⚠️ `sbom generate` eviduje data categories | Výrobce implementuje přístupové mechanismy |
| AI Act — klasifikace AI modelů | ⚠️ SBOM eviduje AI komponenty, rizikovou kategorii | Výrobce provádí conformity assessment |
| Machinery Reg. — cybersecurity dokumentace | ⚠️ SBOM je součást tech. dokumentace (Annex IV) | Výrobce sestaví kompletní dokumentaci |
| RED EN 18031 — deklarace norem | ⚠️ `applied_standards` v `[sbom.regulatory]` | Výrobce deklaruje aplikované normy |
| ESPR/Battery — Digital Product Passport | ⚠️ `[sbom.dpp]` config + DPP exportér (Fáze G) — **priorita battery 18. 2. 2027** | Výrobce spravuje DPP data v externím systému |
| UK PSTI Act — Statement of Compliance | ⚠️ `sbom check --release` reportuje PSTI readiness; šablona SoC jako `sbom export psti-soc` (Fáze G); `[product] psti_route` (direct/cls-label/jc-star) | Výrobce |
| GPSR — cybersecurity as safety | ⚠️ SBOM+VEX = důkaz due diligence; traceability/retence 10 let | Výrobce provádí risk assessment |
| EUCC — certifikační podklad | ⚠️ SBOM+VEX výstupy jako podklad pro security target | Notified body provádí evaluaci |
| PLD — liability trail | ⚠️ evidence report + `sbom diff` + VEX audit = verze/update historie | Výrobce uchovává dokumentaci |
| Sektorové regulace (IEC 62443, MDR, WP.29) | ❌ mimo scope MVP — roadmapa Fáze H | Výrobce / sektorový specialista |

### 1b. Sektorové regulace a mezinárodní režimy

Následující sektorové regulace vyžadují SBOM nebo ekvivalentní dokumentaci. Nejsou součástí MVP (general embedded), ale plánuje se je adresovat přes adaptérový model ve Fázi H:

| Sektor | Regulace | SBOM požadavek | Typická embedded zařízení |
|--------|----------|----------------|---------------------------|
| Průmyslová automatizace | **IEC 62443-4-1** (SDL) + **IEC 62443-4-2** (komponenty) | SBOM součástí secure development lifecycle; vulnerability handling | PLC, RTU, HMI, průmyslové gateway |
| Zdravotnické prostředky | **EU MDR 2017/745** + **FDA §524B** | SBOM + vulnerability management povinné pro premarket submission | Infuzní pumpy, pacientské monitory, diagnostika |
| Automobilový průmysl | **UNECE WP.29 R155** + **ISO/SAE 21434** | SBOM + TARA + continuous monitoring; cybersecurity management system | ECU, TCU, gateway, ADAS |
| Letectví | **DO-326A/DO-356A** | Airworthiness security process | Avionika |
| Kritická infrastruktura | **NIS2** + **NIST SP 800-161r1** | Supply chain risk management; C-SCRM | Energetika, vodárenství, doprava |

**Mezinárodní režimy mimo EU/USA/UK** — fw-context CycloneDX + VEX export lze použít jako podklad pro:
- **Singapore CLS** (Cybersecurity Labelling Scheme, spec CCC SP-151-2 v1.4, 4/2025) — založeno na ETSI EN 303 645; **úrovně L3/L4 vyžadují SBOM** (binary scan proti komponentám deklarovaným v SBOM, od 9/2023); **uznáno UK PSTI (SI 2025/1267)**; CLS(MD) pro zdravotnické prostředky
- **Japan JC-STAR** (METI/IoT policy 30. 8. 2024) — STAR-1 od 3/2025; **STAR-1 uznán UK PSTI (SI 2025/1267, 1. 1. 2026) a SG CLS (1. 6. 2026)**; METI SBOM guidelines (SBOM活用ガイドブック) směřují k povinnému SBOM
- **India TEC** — MTCTE povinná certifikace IoT zařízení (Telecom Act 2023, Rules 2025)
- **Germany IT-SiG 2.0 / IT-Sicherheitskennzeichen** — BSZ certifikace pro kritické komponenty; BSI TR-03183-2 (SBOM guideline)

**CRA reporting workflow (dokumentovaný, SRP je manuální kvůli chybějícímu API):** `vuln scan` → `VulnerabilityFinding` → `vuln analyze` (D1 applicability) → `vex propose` → člověk rozhodne → `vex approve` → **`vuln report-srp` vygeneruje předvyplněný draft** → člověk vloží do SRP. Celý řetězec od finding po draft je uvnitř fw-context; poslední krok (odeslání do SRP) je manuální (ENISA neposkytuje API).

---

## 2. Analýza realizovatelnosti vůči existující codebase

### 2.1 Co codebase již poskytuje (bez nové implementace)

| Schopnost | Zdroj v codebase | SBOM využití |
|---|---|---|
| Seznam aktivních TU (file, directory, compiler args, flags_hash, source_hash) | `manifest.json` → `manifest.load()` → `entries[]` | Fáze A collector — zdroj aktivních souborů |
| Include graf (headers per TU, z libclang token streamu) | `manifest.json` → `entries[].headers` | Header-only komponenty §4.3 |
| config_hash — deterministický fingerprint buildu | `manifest.json` → `config_hash` (SHA-256 nad normalizovaným compile_commands.json) | `sbom_input_hash` základ |
| Detekce vendor SDK | `sdk_detect.py`, `files.is_project` (0/1) | Rozlišení project vs vendor |
| Build system identifikace | `build.py:BuildConfig.system` ("mbed-os", "zephyr", "platformio", …) | `sbom-input.json` → `build_system` |
| Git metadata (commit, tag, dirty flag) | `indexer/git_context.py` | `local_revision`, verze z gitu |
| DB connection s WAL, integrity, caching | `mcp/shared/connection.py:_open_db_safe()` | `security.db` — identický pattern |
| TOML config loading (třívrstvý: global/user/project) | `config/settings.py:_apply_section()` | `components.toml`, `SbomConfig` |
| CLI subcommand registrace (argparse) | `cli/__init__.py:main()` — vzor `cmd_*(args) → int` | `cli/_sbom.py`, `_vuln.py`, `_vex.py` |
| Symbolový FTS5 index (jména, signatury, těla) | `symbols_fts`, `files_fts` | `applicability.py` (Fáze D1) — vyhledávání symbolů dle CVE |
| Call graph (přímá volání + function-pointer edges) | `refs`, `indirect_call_sites`, `fp_assignments` | `applicability.py` — posouzení reachability CVE |

### 2.2 Datový model `manifest.json` — přesný formát

```json
{
  "_format": "fw-context-manifest/1",
  "compile_commands_path": "/path/to/compile_commands.json",
  "project_root": "/path/to/project",
  "build_dir_patterns": ["BUILD/", ".pio/"],
  "config_hash": "abc123...",
  "entries": [
    {
      "file": "src/main.cpp",
      "directory": "/path/to/project",
      "arguments": ["gcc", "-c", "-Iinclude", "-DNDEBUG", "src/main.cpp"],
      "source_hash": "sha256hex",
      "flags_hash": "sha256hex",
      "headers": [
        {"path": "include/main.h", "line": 1, "generated": false},
        {"path": "mbed-os/mbed.h", "line": 2, "generated": false}
      ]
    }
  ]
}
```

**Klíčová pole pro SBOM:**
- `entries[].file` — aktivní zdrojový soubor (relativní k project_root)
- `entries[].headers[].path` — inkludované hlavičky (pro header-only detekci)
- `entries[].headers[].generated` — flag pro build-generované hlavičky (ignorovat)
- `entries[].source_hash` — SHA-256 zdrojového souboru (detekce změn)
- `entries[].arguments` — kompilátorové flagy (pro CVE applicability — např. `-DUSE_MBEDTLS`)

### 2.3 Korekce ideového plánu vůči reálné codebase

| Ideový plán | Reálný stav | Rozhodnutí |
|---|---|---|
| Fáze 0: „přesunout subcommands mimo rostoucí `cli.py`" | CLI je **již modularizované**: `cli/__init__.py:main()` registruje subcommandy | **Fáze 0 se ruší.** Nové příkazy = nové moduly `cli/_sbom.py`, `_vuln.py`, `_vex.py` |
| Rozšířit `BuildSystem` protokol o 3 SBOM metody | Protokol implementuje **11 builderů**; povinné metody by všechny rozbily | **Nový opt-in protokol** `sbom/adapters/base.py:SbomSupport` |
| Přidat `files.component_id` do index.db | Znamenalo by schema migraci + reindex | **Fáze A–B bez DB změn.** SBOM je čistá funkce vstupů; tabulky až `security.db` (Fáze C) |
| `BuildConfig` doplnit PlatformIO `environment` | `build.py:BuildConfig` (dataclass s 20 poli) — `environment` chybí | Fáze B: `environment: str \| None = None` (additive, neprolomí existující buildery) |
| Automatická detekce ELF/map artefaktů | Více image = nejednoznačnost (Mbed `BUILD/**`) | **Fáze A vyžaduje explicitní `[sbom.artifact]`**; auto-discovery přes adaptéry ve Fázi B |
| `sbom init` generuje registry z detekce | Git root + vendor paths známe | MVP: konzervativní šablona (projekt + vendor rooty z `index.vendor_paths`); framework návrhy až adaptéry |
| MCP nástroje | Další povrch k testování | **Fáze A–C CLI-only.** MCP read-only nástroje Fáze D |

### 2.4 Matice obtížnosti implementace

| Komponenta | Obtížnost | Závislosti | Rizika |
|---|---|---|---|
| `sbom/model.py` | **nízká** | stdlib | Čisté dataclasses |
| `sbom/registry.py` | **nízká** | `tomllib` | Prefix-glob matching musí být správně otestován |
| `sbom/validation.py` | **nízká** | `SbomGraph` | Kontroly nad hotovým grafem |
| `SbomConfig` | **nízká** | `_apply_section` | Stejný pattern jako `LLMConfig` |
| `cli/_sbom.py` | **nízká** | CLI pattern | Copy z `cli/_index.py` |
| `sbom/collector.py` | **střední** | `manifest.load()` | Header-only matching přes include graf — data existují |
| `sbom/resolver.py` | **střední** | `subprocess` (git), filesystem I/O | `source_tree_sha256` — streaming hash bezpečný |
| `sbom/report.py` | **střední** | `SbomGraph` | Čistá presentace |
| `sbom/generate.py` | **střední** | collector + resolver + exporter | Deterministické, testovatelné |
| `sbom/exporters/cyclonedx.py` | **střední** | stdlib `json` | Ruční serializace; validace `cyclonedx-cli` externě |
| `sbom/projection.py` | **střední** | `cyclonedx.py` | Druhý export s přepsanými identitami |
| `sbom/linker/gnu_map.py` | **střední** | stdlib | GNU ld formát nestrukturovaný; lld varianta se liší; testovat na FM |
| `vulnerabilities/db.py` | **střední** | sqlite3 | Nová DB — identický pattern jako `index.db` |
| `vulnerabilities/scanner.py` + `grype.py` | **střední-vyšší** | Grype binary (subprocess) | **Riziko:** Grype výstupní formát se mění mezi verzemi → **pin verze v `[vuln.scanner]`** |
| `vulnerabilities/osv.py` | **střední** | `urllib` (stdlib) | OSV `/v1/querybatch` HTTP API; rate limiting |
| `vex/` (model + store + export) | **střední** | `security.db` | VEX lifecycle, CDX VEX export |
| `sbom/linker/elf.py` | **střední** | `pyelftools` (opt-in extra) | Cross-check map↔ELF |
| `vulnerabilities/applicability.py` | **vyšší** | `index.db` callgraph | Symbol-level callgraph → ne pro makra/preprocessor podmínky; možné false negatives |
| `sbom/identify.py` | **vysoká** | OSV/ClearlyDefined externí data | **Heuristika pouze** — plán správně: návrh k manuálnímu schválení, nikdy automatický zápis |
| Adaptéry `suggest_component_rules()` | **střední** | Framework-specific | MVP mbed-os (známá mapa); PlatformIO/Zephyr variabilní |
| Multi-image (E2) | **střední-vyšší** | `SbomGraph` per image + agregace | Testování na bootloader+app projektech |

### 2.5 Mimo dosah nástroje (explicitně vyloučeno)

| Požadavek | Důvod |
|---|---|
| ENISA SRP reporting API | SRP v provozu od 11. 9. 2026; **ENISA potvrdil: API se v této fázi neposkytuje** (FAQ Q15). Odeslání je proto manuální (webový formulář). fw-context generuje **předvyplněný draft** (`vuln report-srp`); API integrace je future consideration, pokud ENISA API zpřístupní |
| ML model vulnerability scanning | Grype neumí AI/ML model CVE. Potřeba specializovaný scanner (modelcards, model signing) — mimo scope |
| Plná SPDX 3.1 kompatibilita | SPDX 3.1 v RC (1/2026), build/hardware/supply-chain profily se vyvíjejí. CDX 1.7 MVP; SPDX exporter Fáze F |
| Automatický zápis identity vendored komponent | `identify.py` vždy návrh → člověk potvrdí |
| DPP exportér (ESPR/Battery) | ESPR delegated acts pro elektroniku se připravují (2029). **Battery DPP (Reg. 2023/1542) je povinný od 18. 2. 2027** — Fáze G: nejprve battery, pak ESPR elektronika po zveřejnění product-specific požadavků |
| Sektorové adaptéry (IEC 62443, MDR, WP.29) | Mimo general-embedded scope. Roadmapa Fáze H — adaptérový model umožní komunitní rozšíření |

### 2.6 Co platí beze změny

- Evidence ladder a povinnost deklarovat neúplnost.
- Interní kanonický model ≠ CycloneDX (exporter je tenká vrstva).
- Offline-by-default; online enrichment pouze explicitně.
- Scanner jako volitelný executable, ne Python dependency.
- `not_affected` vždy vyžaduje člověka; `fw-context` navrhuje a dokládá.
- `security.db` odděleně od `index.db`.
- Root identity přes `sbom_input_hash`, ne jen `config_hash`.
- Duální identita komponenty: compliance SBOM popisuje skutečný interní fork (+ derived-from upstream), scanner projection pro vulnerability matching používá upstream `security_match_*` identitu (doporučení §26.4).
- Rekonstrukce provenance je vždy návrh k ručnímu schválení, nikdy automatický zápis identity.

---

## 3. Cílová struktura

```text
src/fw_context_mcp/
├── sbom/                      # Fáze A–B, E
│   ├── __init__.py
│   ├── model.py               # A1: kanonické dataclasses
│   ├── registry.py            # A1: components.toml load/validate + path matching
│   ├── collector.py           # A2: manifest.json → aktivní zdroje
│   ├── resolver.py            # A2: identity + verze komponent
│   ├── report.py              # A2: sbom check / explain výstup
│   ├── generate.py            # A3: orchestrace, sbom-input.json, SHA-256
│   ├── projection.py          # A3: scanner projection (doporučení §26.4)
│   ├── identify.py            # C3: rekonstrukce provenance vendored komponent (doporučení §26.7)
│   ├── validation.py          # A3: interní kontroly výstupu
│   ├── diff.py                # D3: porovnání dvou CDX dokumentů
│   ├── exporters/
│   │   ├── __init__.py
│   │   ├── cyclonedx.py       # A3: CDX 1.6/1.7
│   │   └── spdx.py            # Fáze F: SPDX 3.1 (ISO/IEC 5962:2021) — build/hardware/supply-chain profily
│   ├── linker/                # Fáze B
│   │   ├── __init__.py
│   │   ├── base.py            # B1: LinkedInput, MapParser protokol
│   │   ├── gnu_map.py         # B1: GNU ld / lld map parser
│   │   └── elf.py             # B3: pyelftools wrapper (opt-in extra)
│   └── adapters/              # Fáze B (generic, mbed_os), Fáze E (zephyr, platformio, esp_idf)
│       ├── __init__.py
│       ├── base.py            # B2: SbomSupport protokol, ReleaseArtifact
│       ├── generic.py         # B2
│       ├── mbed_os.py         # B2
│       ├── zephyr.py          # E1
│       ├── platformio.py      # E1
│       └── esp_idf.py         # E1
├── vulnerabilities/           # Fáze C–D
│   ├── __init__.py
│   ├── scanner.py             # C1: VulnerabilityScanner protokol
│   ├── grype.py               # C2: Grype adapter
│   ├── osv.py                 # C3: OSV HTTP API klient (commit matching přes /v1/querybatch, doporučení §26.5)
│   ├── normalize.py           # C1: VulnerabilityFinding, match confidence
│   ├── db.py                  # C1: security.db schema + CRUD
│   └── applicability.py       # D1: build-aware CVE evidence
├── vex/                       # Fáze D
│   ├── __init__.py
│   ├── model.py               # D2: stavy + lifecycle
│   ├── store.py               # D2: assessments v security.db
│   └── export.py              # D2: CycloneDX VEX + YAML
├── mcp/handlers/
│   └── sbom.py                # D3: read-only MCP nástroje
└── cli/
    ├── _sbom.py               # Fáze A (+B, D3 diff)
    ├── _vuln.py               # Fáze C (+D1 analyze)
    └── _vex.py                # Fáze D
```

Design pravidla (dědí projektové konvence):

- `sbom/`, `vulnerabilities/`, `vex/` nesmí importovat z `mcp/` ani `search/`; směr závislostí `cli → {sbom, vulnerabilities, vex} → indexer (jen manifest/config_hash/ops typy)`.
- `vulnerabilities/applicability.py` smí číst `index.db` (přes `indexer/db` a `mcp/shared` logiku callgraph) — je to jediná SBOM-doménová část závislá na symbolovém indexu.
- **Evidence store = soubory, ne tabulky.** Cookbook §13.11 navrhuje `sbom_components`/`sbom_file_components` tabulky; jejich roli plní verzovaný evidence report JSON z `generate` (deterministický, diffable v gitu, čitelný bez DB). Multi-image (E2) generuje jeden report per image; MCP (D3) čte reporty + `security.db`. Tabulky se zavedou, jen pokud se file-based přístup ukáže jako nedostatečný — rozhodnutí se přehodnotí v E2.
- Exportéři čtou pouze kanonický model — nikdy SQLite, nikdy linker mapu přímo.
- Prefix `_` = privátní modul domény.
- `security.db` se otevírá přes stejné sqlite konvence jako `index.db` (pysqlite3 redirect v `__init__.py` běží první; WAL; `foreign_keys=ON`).

---

## 4. Fáze A — build-aware component inventory + CycloneDX MVP

**Cíl:** z `manifest.json` + `components.toml` + explicitního artefaktu vygenerovat validní CycloneDX s evidence level `compiled` a poctivým `composition: incomplete`. Plně offline, bez DB změn, bez nových povinných závislostí (stdlib: `tomllib`, `json`, `hashlib`, `pathlib`, `re`, `subprocess` pro git).

### 4.1 Kanonický model — `sbom/model.py` (PR A1)

```python
@dataclass(frozen=True, slots=True)
class HashValue:
    alg: str            # "SHA-256"
    content: str

@dataclass(frozen=True, slots=True)
class ComponentIdentity:
    component_id: str           # stabilní ID z registry ("mbedtls")
    name: str
    version: str                # resolved; fallback viz 4.4
    revision: str | None        # plný git SHA, pokud známý
    supplier: str | None
    producer: str | None = None    # CISA 2026 ME v2.1 "Component Producer" (nahrazuje Supplier; výrobce komponenty)
    component_type: str         # library|framework|operating-system|application|firmware|ai-model|dataset|data-processor
    purl: str | None
    cpe: str | None
    source_url: str | None
    licenses: tuple[str, ...]
    parent: str | None          # component_id nadřazené komponenty
    version_source: str         # "git"|"header"|"manual"|"hash-fallback"
    warnings: tuple[str, ...]
    support_status: str = "unknown"        # "supported"|"eol"|"internal-fork"|"unknown" (cookbook §6)
    support_end_date: str | None = None    # ISO 8601 date (YYYY-MM-DD) — povinné dle CRA Art. 13(8) pro supported/eol
    owner: str | None = None               # interní vlastník komponenty
    internal_version: str | None = None    # interní fork "2.28.8+company.3" (cookbook §16.5)
    # ── Data Act (Reg. 2023/2854): data categories ──
    data_categories: tuple[str, ...] = ()  # např. ("personal", "telemetry", "location", "health")
    data_access_url: str | None = None     # URL/endpoint pro přístup k datům dle Data Act
    # ── AI Act (Reg. 2024/1689): AI model metadata ──
    ai_risk_category: str | None = None    # "unacceptable"|"high"|"limited"|"minimal"|"gpa" — jen pro ai-model
    ai_training_data_summary: str | None = None  # shrnutí trénovacích dat (vyžadováno AI Act)
    ai_model_version: str | None = None    # verze modelu (může být odlišná od verze knihovny)
    ai_framework: str | None = None        # "tensorflow-lite"|"onnx"|"pytorch-mobile"|...
    ai_intended_use: str | None = None     # určené použití modelu (CISA SBOM for AI, G7 2026)
    ai_architecture: str | None = None     # architektura modelu (např. "MobileNetV2", "RandomForest") — CISA SBOM for AI
    # ── CISA SBOM for AI (G7, 12.5.2026) — rozšířené klastry (volitelné) ──
    ai_model_input_output_properties: str | None = None   # vstupy/výstupy modelu (SLP cluster)
    ai_training_properties: str | None = None             # trénovací vlastnosti modelu (models cluster)
    ai_security_controls: tuple[str, ...] = ()            # security controls (security-properties cluster)
    ai_compliance: tuple[str, ...] = ()                   # security compliance refs (security-properties cluster)
    ai_performance_metrics: tuple[str, ...] = ()          # KPI cluster (operational performance metrics)
    # dataset komponenty (component_type = "dataset"): trénovací/data sady
    dataset_provenance: str | None = None      # původ datasetu
    dataset_sensitivity: str | None = None     # "personal"|"non-personal"|...
    dataset_statistical_properties: str | None = None
    # ── ESPR / Digital Product Passport (Reg. 2024/1781) ──
    recycled_content_pct: float | None = None  # % recyklovaného obsahu (DPP materials)
    repairability_score: str | None = None     # skóre opravitelnosti (např. "A"–"E" dle FR index)
    hazardous_substances: tuple[str, ...] = () # seznam nebezpečných látek (REACH/SCIP kompatibilní)
    # ── Upstream bezpečnostní identita (doporučení §26.3) ──
    upstream_name: str | None = None
    upstream_version: str | None = None
    upstream_repository: str | None = None
    upstream_revision: str | None = None   # commit/tag upstreamu, ze kterého fork vychází
    # ── Lokální provenance (doporučení §26.3) ──
    local_revision: str | None = None      # commit projektového gitu, který komponentu naposledy změnil
    source_tree_sha256: str | None = None  # hash celého source tree komponenty (vč. nekompilovaných souborů)
    patchset_sha256: str | None = None     # hash souborů v patch_dir vůči upstreamu
    # ── Scanner projekce (doporučení §26.3–26.4) ──
    security_match_name: str | None = None     # default: upstream_name
    security_match_version: str | None = None  # default: upstream_version
    security_match_mode: str = "exact"         # "exact"|"upstream-ancestor"|"heuristic"
    identity_confidence: str = "high"          # "high"|"medium"|"low"

@dataclass(frozen=True, slots=True)
class ComponentEvidence:
    level: str                  # "compiled"|"linked"|"retained"|"deployed"
    source_paths: tuple[str, ...]   # relativní k project_root, setříděné
    object_paths: tuple[str, ...] = ()       # Fáze B
    archive_members: tuple[str, ...] = ()    # Fáze B
    retained_bytes: int | None = None        # Fáze B
    evidence_refs: tuple[EvidenceRef, ...] = ()

@dataclass(frozen=True, slots=True)
class EvidenceRef:
    kind: str                   # "compile-command"|"linker-map"|"registry"|"git"|"version-header"|"blob"|"source-tree"|"patchset"
    detail: str                 # např. "manifest.json entry #128", "firmware.map line 8123"

@dataclass(frozen=True, slots=True)
class FirmwareArtifact:
    path: str                   # relativní k project_root
    sha256: str
    artifact_type: str          # "elf"|"bin"|"hex"
    role: str                   # "application" (Fáze A); Fáze E: "bootloader"|"network-core"|…
    signed: bool = False        # podepsaný/šifrovaný image (Fáze E, cookbook §16.7)
    derived_from: str | None = None  # path předka v řetězci elf→bin→signed→encrypted (cookbook §16.7)

@dataclass(frozen=True, slots=True)
class SbomGraph:
    root: ComponentIdentity
    components: tuple[ComponentIdentity, ...]
    evidence: Mapping[str, ComponentEvidence]   # component_id → evidence
    artifacts: tuple[FirmwareArtifact, ...]
    completeness: str           # "complete"|"incomplete"|"unknown"
    unmapped_sources: tuple[str, ...]
    linker_inputs_unmapped: tuple[str, ...] = ()  # Fáze B
    # ── Regulatory metadata ──
    applied_standards: tuple[str, ...] = ()      # harmonizované normy (např. "EN 18031-1", "ETSI EN 303 645")
    machinery_category: str | None = None         # kategorie dle Machinery Reg. Annex I
    product_data_categories: tuple[str, ...] = () # Data Act: kategorie dat produkovaných produktem
    # ── CRA produktová klasifikace (Reg. (EU) 2025/2392) ──
    cra_class: str = "default"                    # "default"|"important-i"|"important-ii"|"critical" (Annex III/IV)
    cra_category: str | None = None               # kategorie dle Annex III/IV (např. "Annex III Class I #11 Operating systems")
    cra_conformity_route: str | None = None       # odvozeno: "module-a"|"eu-type"|"full-quality-assurance"|"eucc-certificate" (Art. 32)
    security_contact: str | None = None           # kontakt pro hlášení zranitelností (CRA Annex VII 2(b), PSTI, GPSR)
    eu_responsible_operator: str | None = None    # EU-based operator (GPSR Art. 16, CRA Art. 22, PLD Art. 8)
    psti_route: str | None = None                 # "direct"|"cls-label"|"jc-star" (UK PSTI SI 2025/1267)
    # ── CISA 2026 ME v2.1 SBOM metadata ──
    sbom_version: str = "1.0.0"                   # SemVer SBOM verze (CISA ME v2.1 "SBOM Version")
    sbom_tool_name: str = "fw-context"            # CISA ME v2.1 "SBOM Tool Name"
    sbom_tool_version: str | None = None          # CISA ME v2.1 "SBOM Tool Version"
    sbom_author: str | None = None                # CISA ME v2.1 "SBOM Author"
    sbom_signature: str | None = None             # CISA ME v2.1 "SBOM Author Signature" (nastaví signing hook E3)
```

Pole pro Fázi B se zavedou hned (defaulty), aby Fáze B neměnila public dataclasses — pouze je začala plnit.

Invarianty vynucené builderem:

- `component_id` unikátní; `root.component_id` ∉ `components`;
- každý `parent` ukazuje na existující komponentu (jinak FAIL);
- `evidence` klíče ⊆ `components ∪ {root}`;
- `unmapped_sources` ∩ (všechny `source_paths`) = ∅.

### 4.2 Registry — `sbom/registry.py` (PR A1)

Soubor `.fw-context/components.toml` (samostatně od `config.toml` — reviewovatelné, verzovatelné s projektem). Schema `1`:

```toml
schema = 1

[product]
name = "example-locker-controller"
supplier = "Example s.r.o."
producer = "Example s.r.o."          # CISA 2026 ME v2.1: Component Producer (root komponenta)
type = "firmware"
data_categories = ["telemetry", "location"]   # Data Act: kategorie dat produkovaných produktem
security_contact = "security@example.com"     # CRA Annex VII 2(b) + PSTI + GPSR: hlášení zranitelností
eu_responsible_operator = "Example s.r.o., Prague, CZ"  # GPSR Art. 16 + CRA Art. 22 + PLD Art. 8
cra_class = "default"               # "default"|"important-i"|"important-ii"|"critical" (Reg. (EU) 2025/2392)
cra_category = ""                   # kategorie dle Annex III/IV (např. "Annex III Class I #11 Operating systems")
psti_route = "direct"               # UK PSTI: "direct"|"cls-label"|"jc-star" (SI 2025/1267)

[[components]]
id = "app"
type = "application"
paths = ["src/**", "include/**"]
version_source = "git"
git_root = "."

# Případ B (interní fork, např. mbedtls v Z-Box ECB):
[[components]]
id = "mbedtls"
name = "Mbed TLS"
type = "library"
supplier = "TrustedFirmware.org"
parent = "mbed-os"
paths = ["mbed-os/connectivity/mbedtls/**"]
version_source = "manual"        # internal-fork: interní verze (§4.4), ne header
version = "2.28.8+company.4"     # uložena do internal_version; compliance SBOM ji nese v `version`
purl_template = "pkg:generic/mbedtls@{version}"

[components.security]               # váže se k předchozí [[components]] (mbedtls)
owner = "embedded-security"
support_status = "internal-fork"
support_end_date = "2029-12-31"    # CRA Art. 13(8): povinné pro supported/eol; internal-fork = volitelné
monitoring = ["NVD", "OSV", "upstream advisories"]
remediation = "backport-or-replace"

[components.upstream]               # bezpečnostní (scanner) identita — doporučení §26.3
name = "mbedtls"
version = "2.28.8"
repository = "https://github.com/Mbed-TLS/mbedtls"
revision = "abcdef0123456789"
cpe = "cpe:2.3:a:arm:mbed_tls:2.28.8:*:*:*:*:*:*:*"   # explicitní CPE — povinné pro Grype matching (F1)
security_match_mode = "upstream-ancestor"   # interní fork: match proti upstream verzi
patch_dir = "patches/mbedtls"               # volitelné; obsah → patchset_sha256

# Verze z headeru (alternativa k manual u ne-fork komponent):
[[components]]
id = "littlefs"
name = "littlefs"
type = "library"
parent = "mbed-os"
paths = ["mbed-os/storage/filesystem/littlefs/**"]
version_source = "header"
version_file = "mbed-os/storage/filesystem/littlefs/lfs.h"
version_pattern = 'LFS_VERSION_STRING\s+"([^"]+)"'

# Případ C (neznámý původ, např. lib/zcbor v Z-Box ECB) — minimální evidence:
[[components]]
id = "zcbor"
name = "zcbor"
type = "library"
paths = ["lib/zcbor/**"]
version_source = "hash-fallback"     # source_tree_sha256 = jediná stabilní identita
identity_confidence = "low"
security_match_mode = "heuristic"    # dokud identify-component nenavrhne upstream

[[components]]
id = "modem-fw"
type = "firmware"
version_source = "manual"
version = "EG91EFBR06A08M4G"
deployed_always = true
blob_path = "blobs/eg91.bin"        # Fáze A: blob se hashuje (SHA-256), evidence "deployed"

# AI Act: AI/ML model komponenta (edge ML, TinyML)
[[components]]
id = "anomaly-detector"
name = "Anomaly Detector Model"
type = "ai-model"
paths = ["ml-models/anomaly-detector/**"]
version_source = "manual"
version = "v3.2.1"
data_categories = ["telemetry"]         # Data Act: model zpracovává telemetrická data
ai_risk_category = "limited"            # AI Act klasifikace
ai_training_data_summary = "Syntetická data + historická telemetrie 2023-2025"
ai_framework = "tensorflow-lite"

[components.upstream]
name = "anomaly-detector"
version = "v3.2.1"
security_match_mode = "exact"
```

Pravidla (unit-testované):

1. **Validace při load:** duplicitní `id` → error; `parent` neexistuje → error; `version_source` ∈ {git, header, manual, hash-fallback}; `version_file` vyžadován pro `header`; `version` vyžadován pro `manual`; neznámé klíče → warning (forward-compat); neznámé `schema` > 1 → error. `[product]` validace: `cra_class` ∈ {default, important-i, important-ii, critical} (jinak warning `unknown-cra-class`); `cra_category` volitelné, ale při `cra_class ≠ default` v `--release` vyžadováno (conformity route nelze určit bez kategorie); `security_contact` v `--release` vyžadováno (CRA Annex VII 2(b) + PSTI requirement 2 + GPSR); `eu_responsible_operator` v `--release` vyžadováno pro produkty na EU trhu (GPSR Art. 16 / CRA Art. 22); `psti_route` ∈ {direct, cls-label, jc-star} nebo warning. `[components.security]` validace: `support_status` ∈ {supported, eol, internal-fork, unknown}; `support_end_date` musí být ISO 8601 (YYYY-MM-DD) pokud je uvedeno; `support_status = "supported"` bez `support_end_date` → warning `missing-support-end-date` v `--release` (CRA Art. 13(8)); **CRA Art. 13(8) minimum: `support_end_date` produktu i komponent musí pokrývat ≥ 5 let od placing on market** — kratší → warning `short-support-period` v `--release` FAIL s možností explicitního odůvodnění `support_shorter_than_5y_reason` (produkt očekáván v užití < 5 let, zapsáno do evidence, Annex VII(4)); `support_status = "eol"` s `support_end_date` v budoucnosti → warning `eol-date-in-future`; `owner`/`monitoring`/`remediation`/`migration_target`/`support_end_date` jinak volitelné. `[components.upstream]` validace: `security_match_mode` ∈ {exact, upstream-ancestor, heuristic}; `upstream-ancestor` vyžaduje `version` (jinak není proti čemu matchovat); `heuristic` vynutí `identity_confidence` ≤ medium; `security_match_name/version` defaultují na `name`/`version` upstream sekce; `patch_dir` musí existovat, pokud je uveden; `cpe` je volitelné, ale komponenta určená k CVE matchingu (`security_match_mode` ∈ {exact, upstream-ancestor}) bez `cpe` a s `pkg:generic` purl → warning `projection-unmatched-risk` (Grype `pkg:generic` bez CPE nematchuje), v `--release` FAIL (viz §4.4 release policy). `[components.ai]` validace: pouze pro `type = "ai-model"`; `ai_risk_category` ∈ {unacceptable, high, limited, minimal, gpa}; `ai_risk_category ∈ {high, limited}` bez `ai_training_data_summary` → warning `ai-missing-training-data` (AI Act transparency); `ai_framework` ∈ známé hodnoty nebo warning `ai-unknown-framework`. `dataset` komponenty (`type = "dataset"`): `dataset_provenance` v `--release` vyžadováno (G7 SBOM for AI dataset cluster). `data_categories` validace: prázdné povoleno; non-empty → každá hodnota ∈ {personal, telemetry, location, health, financial, biometric, environmental, operational} nebo warning `unknown-data-category`.
2. **Path matching — prefix-glob, ne `PurePath.match`:** `PurePath.match` je right-anchored (`PurePath("app/src/main.c").match("src/**")` → `True` → falešné vlastnictví). Glob se proto překládá na regex ukotvený na **začátek** relativní cesty (`**` = libovolná hloubka, `*` = jedna úroveň) a matchuje celou cestu zleva. **Nejdelší (nejspecifičtější) matching prefix vyhrává** — vnořená komponenta má přednost; konflikt dvou pravidel stejné specificity → error s oběma pravidly.
3. Jeden soubor = jeden primární vlastník.
4. `deployed_always = true` → komponenta v SBOM i bez aktivních zdrojů, evidence `deployed`. `blob_path` → SHA-256 blobu se počítá **již ve Fázi A** (streaming) a zapisuje se do evidence reportu s `EvidenceRef(kind="blob")`; blob chybí na disku → FAIL v `--release`, jinak warning (cookbook §15.2 + release gate §18.1: „všechny deployed bloby evidovány").

API:

```python
def load_registry(path: Path) -> ComponentRegistry          # raises RegistryError
def match_owner(registry, rel_path: str) -> ComponentRule | None
def iter_rules(registry) -> Iterable[ComponentRule]
```

### 4.3 Collector — `sbom/collector.py` (PR A2)

Vstup: `manifest.load(db_dir)` (existující `indexer/manifest.py:load()`).

- Aktivní zdroje = `entries[].file` (relativní, již normalizované generátorem).
- **Header-only komponenty:** `entries[].headers` (manifest je sbírá z libclang token streamu) se použijí pro komponenty, jejichž `paths` nematchují žádnou aktivní TU, ale matchují hlavičky inkludované aktivními TU → evidence `compiled` (cookbook §16.2). Bez tohoto by header-only knihovny ze SBOM úplně vypadly.
- Detaily hlaviček se do exportu **nezahrnují** (`include_headers = false` default, §4.9) — include graph slouží pouze k detekci přítomnosti.
- Collector nesahá na disk kromě manifestu — deterministický, testovatelný.

### 4.4 Resolver — `sbom/resolver.py` (PR A2)

Mapování: aktivní zdroj → `match_owner()` → komponenta (agregace `source_paths`).

Verze — pořadí autority (první úspěch vyhrává):

1. `manual` — literál z registry.
2. `header` — regex `version_pattern` (default `VERSION_STRING\s+"([^"]+)"`) nad `version_file`; max 1 MB; neshoda → FAIL s hláškou.
3. `git` — `git -C <git_root> rev-parse HEAD` (subprocess, timeout 10 s, bez shellu). Dirty strom (`git status --porcelain`) → warning `dirty-worktree`. Verze: `git describe --tags --always` když tag, jinak `0+git.<short-sha>`.
4. `hash-fallback` — verze odvozená ze `source_tree_sha256` (provenance níže; jediná stabilní identita Případu C): `legacy+sha256.<prefix16>`, kde prefix16 = prvních 16 znaků `source_tree_sha256`. Verze a source-tree hash jsou tak **jedna identita** — žádná divergence mezi hashem z manifestu (jen kompilované TUs) a hashem celého stromu (cookbook §6.2).

Komponenta z registry **bez aktivních zdrojů** a bez `deployed_always` → není v grafu; v reportu jako `declared-not-compiled` (info).

**Interní fork, EOL a support period (cookbook §10.6–10.7, §16.5; CRA Art. 13(8)):**

- `support_status = "internal-fork"` → komponenta musí mít `version_source = "manual"` s interní verzí (`+company.N`, uloženou do `internal_version`) nebo `hash-fallback`; čistá upstream verze → **FAIL** („patchovaná komponenta používá pouze upstream verzi"). `support_end_date` je pro internal-fork volitelné.
- `support_status = "supported"` → `support_end_date` povinné v `--release` (CRA Art. 13(8): výrobce musí specifikovat konec podpory při nákupu); chybějící → warning `missing-support-end-date`, v `--release` FAIL.
- **CRA Art. 13(8) minimum — 5 let:** `support_end_date` produktu a podporovaných komponent musí pokrývat ≥ 5 let od placing on market (product `placed_on_market_date` z `[product]` nebo `[sbom.product]`). Kratší → warning `short-support-period`; v `--release` FAIL s výjimkou explicitního `support_shorter_than_5y_reason` (produkt očekáván v užití < 5 let — zapsáno do evidence reportu, Annex VII(4) vyžaduje informace o určení support period v tech. dokumentaci).
- `support_status = "eol"` → warning `eol-component` v reportu. V `--release` je tento warning **tolerován pouze při kompletních EOL metadatech** (`remediation` + `migration_target` + `support_end_date` vyplněny — cookbook §10.6 považuje evidované EOL za legitimní stav); EOL bez těchto metadat → FAIL. Pokud `support_end_date` je v budoucnosti a stav je `eol` → warning `eol-date-in-future` (datum v rozporu se stavem).
- `support_status`, `support_end_date` a `owner` se exportují jako CDX properties (`fw-context:support-status`, `fw-context:support-end-date`, `fw-context:owner`).

**CRA produktová klasifikace a conformity route (Reg. (EU) 2025/2392, Art. 32):**

Resolver naplní `SbomGraph.cra_class`/`cra_category` z `[product]` a odvodí `cra_conformity_route`:
- `default` → **Module A** (internal control, Annex VIII Part I) — bez notified body;
- `important-i` (Annex III Class I) → **EU-type examination + conformity to type** (Annex VIII Parts II+III), pokud nejsou aplikovatelné harmonizované normy;
- `important-ii` (Annex III Class II) a `critical` bez certifikátu → **full quality assurance** (Annex VIII Part IV);
- `critical` (Annex IV) → **EUCC certifikát** assurance level ≥ substantial (Art. 32(4)).
- Kategorie relevantní pro embedded (Annex III Class I): boot managery/bootloadery (#8), síťová rozhraní Wi-Fi/BLE/Zigbee (#10), operační systémy vč. RTOS (#11), routery/modermy (#12), MCU/MPU/ASIC/FPGA se security funkcemi (#13–15). **Core-functionality test:** produkt integrující important komponentu není sám important (Implementing Reg. 2025/2392, recitaly 2–5) — klasifikace je lidské rozhodnutí potvrzené v `[product]`, ne automatická derivace.
- Route se reportuje v `sbom check --release` a exportuje jako CDX property `fw-context:cra-class`, `fw-context:cra-category`, `fw-context:cra-conformity-route`.

**Provenance evidence (doporučení §26.3, §26.8):**

- `source_tree_sha256` — SHA-256 nad setříděným seznamem `(rel_path, file_sha256)` všech souborů matchujících `paths` (streaming z disku, deterministické pořadí, binární režim; symbolické linky mimo strom → FAIL). Počítá se **vždy**, když `paths` existují — je to jediná stabilní identita pro Případ C (neznámý původ) a kontrolní kotva pro Případ B (fork drift). EvidenceRef `kind="source-tree"`.
- `local_revision` — `git log -1 --format=%H -- <paths>` (subprocess, timeout; bez gitu → `None`, ne chyba).
- `patchset_sha256` — SHA-256 nad setříděným obsahem souborů v `patch_dir`; evidence `kind="patchset"`.
- `support_status = "internal-fork"` navíc vyžaduje `[components.upstream]` (jinak není oddělena interní a security identita) — bez ní FAIL v `--release`, jinak warning `fork-without-upstream`.

**Release policy pro lokální komponenty (doporučení §26.8):**

- Vývojový režim povolí `unknown upstream`, chybějící PURL, `heuristic` identitu a `composition: incomplete` — vždy s warningem.
- Release režim (`--release`) vyžaduje pro každou komponentu alespoň jedno z: `upstream version + revision`, nebo `internal_version + source_tree_sha256`. Pro komponenty určené k CVE matchingu navíc `security_match_name + security_match_version`, `identity_confidence` medium/high a **CPE** (bez CPE a s `pkg:generic` purl Grype nematchuje — komponenta by byla pro scanner neviditelná).
- Blokující chyby (FAIL vždy — i mimo `--release`/`--strict`): komponenta bez stabilní identity; komponenta bez verze, revize i source-tree hashe; scanner projection v konfliktu s compliance identitou; `security_match_mode = "exact"` při neznámé upstream verzi.

Root komponenta: `[product]` z registry + verze z `--release-version` nebo `[sbom.product] version_command` (subprocess `shlex.split`, timeout; failure → FAIL v `--release`, jinak warning + `0.0.0+unknown`).

### 4.5 Report — `sbom/report.py` (PR A2)

`sbom check` (text; `--json` pro CI):

```text
Build system:        mbed-os
Translation units:   318
Mapped:              318   Unmapped: 0
Components:          12 (1 nested under mbed-os, 1 deployed-only)
Without version:     0
Artifact (elf):      found, SHA-256 computed
Evidence level:      compiled → composition: incomplete (linker evidence není implementována)
Warnings:            dirty-worktree (app), generated-purl (mbedtls)
Exit code:           0 OK | 1 warnings v --strict | 2 errors (fail_unmapped v --release/--strict, validační chyby)
```

`sbom explain <id>` — identity source, version source, počet zdrojů/objektů, ukázka evidence refs, warnings.

**Regulatory documentation readiness** — `sbom check --release` rozšiřuje report o pokrytí napříč všemi relevantními regulacemi:

| Regulace / Požadavek | Pokryto fw-context | Poznámka |
|---|---|---|
| **CRA Annex I Part II** (vulnerability handling — **esenciální požadavek**) | | |
| **SBOM v machine-readable formátu ≥ top-level dependencies** (bod 1) | ✅ `sbom generate` (CDX 1.7) | **Explicitní povinnost** CRA — sbom check ověří, že každá komponenta má identitu a dependencies jsou kompletní |
| Identifikace a dokumentace zranitelností | ✅ `vuln scan` + `vuln analyze` (D1) | |
| Klasifikace dle závažnosti | ✅ CVSS z Grype + override v `vex propose` | |
| Oprava a reporting (bez zbytečného odkladu) | ⚠️ VEX workflow dokumentuje rozhodnutí; SRP odeslání manuální (bez API) | `vuln report-srp` generuje draft |
| **CRA Art. 7/8/32 — produktová klasifikace a conformity route** | ✅ `[product] cra_class/cra_category` → `cra_conformity_route` (Module A / EU-type / full QA / EUCC) | Reg. (EU) 2025/2392; core-functionality test je lidské rozhodnutí |
| **CRA Annex VII** | | |
| Popis produktu + určené použití | ✅ `[product]` z registry | |
| Cybersecurity risk assessment | ⚠️ SBOM dokládá komponenty — risk assessment je samostatný dokument | `sbom check` reportuje „covered: component inventory; uncovered: risk assessment" |
| Seznam harmonizovaných standardů | ✅ `[sbom.regulatory].applied_standards` | Výrobce deklaruje v configu (např. ETSI EN 303 645, EN 18031) |
| Design + development info | ✅ `config_hash`, `sbom_input_hash`, `formulation` (Fáze E) | |
| **SBOM v tech. dokumentaci (Annex VII 2(b))** | ✅ `sbom generate` + evidence report | Povinná součást, ne náhrada |
| **Vulnerability disclosure contact (Annex VII 2(b))** | ✅ `[product].security_contact` | Společné s PSTI requirement 2 + GPSR |
| Due diligence third-party komponent | ✅ `version_source`, `upstream_*`, `source_tree_sha256`, scanner projection | Art. 13(5) |
| EU declaration of conformity | ⚠️ `sbom export doc` (Fáze G) — podklady | Výrobce podepíše |
| **Support period (Art. 13(8), min. 5 let)** | ✅ `support_end_date` ≥ 5 let od placing on market | Kratší jen s odůvodněním |
| **CRA Art. 14 — SRP reporting** | ⚠️ `vuln report-srp` draft (24h/72h/final) + `euvd_id` | SRP bez API; odeslání manuální |
| **EU Data Act (2023/2854)** | | |
| Data categories + access mechanisms | ✅ `[product].data_categories`, `ComponentIdentity.data_categories` | Výrobce implementuje přístup |
| Data-flow documentation | ✅ SBOM eviduje data-producing komponenty | |
| **EU AI Act (2024/1689) + AI Omnibus (2026/1744)** | | |
| AI model classification (risk category) | ✅ `component_type = "ai-model"`, `ComponentIdentity.ai_risk_category` | high-risk Annex III od 2. 12. 2027, Annex I od 2. 8. 2028 |
| Training data provenance | ✅ `ComponentIdentity.ai_training_data_summary`, `ai_model_version` | |
| Model intended use + architecture | ✅ `ai_intended_use`, `ai_architecture` | CISA SBOM for AI (G7 2026) |
| **EU Machinery Reg. (2023/1230)** | | |
| Cybersecurity pro strojní zařízení | ✅ `[sbom.regulatory].machinery_category` | SBOM součást tech. dokumentace **Annex IV** (Art. 21); EHSR Annex III §1.1.9/§1.2.1; EUCC presumption Art. 29(9) |
| **RED / EN 18031** | | |
| Harmonizované normy pro radio equipment | ✅ `[sbom.regulatory].applied_standards` (EN 18031-1/2/3) | Pro WiFi/BLE/LoRa; (EU) 2022/30 zrušen (EU) 2026/339 od 11. 12. 2027; RED DoC/tech. file 10 let |
| **ESPR / Battery / Digital Product Passport** | | |
| Materiálové složení a recyklovatelnost | ⚠️ `ComponentIdentity.recycled_content_pct`, `repairability_score`, `hazardous_substances` | Fáze G: DPP exportér — **battery (18. 2. 2027) před ESPR elektronikou (2029)** |
| **UK PSTI Act 2022** | | |
| Unique passwords + vulnerability disclosure + update period | ✅ `support_end_date` + VEX workflow + `[product].security_contact`; `psti_route` (direct/cls-label/jc-star) | SoC šablona Fáze G; SoC retence 10 let / support period; CLS/JC-STAR deemed compliance (SI 2025/1267) |
| **GPSR (2023/988)** | | |
| Kyberbezpečnost jako součást bezpečnosti produktu | ⚠️ SBOM+VEX = důkaz due diligence; risk assessment samostatně | + traceability/retence 10 let, `[product].eu_responsible_operator` |
| **PLD (Dir. 2024/2853, aplikace 9. 12. 2026)** | | |
| Liability trail (verze/update historie) | ✅ evidence report + `sbom diff` + VEX audit | Kyberzranitelnost = faktor defectu; software = produkt |
| **US / CISA** | | |
| SBOM Minimum Elements 2026 (v2.1) | ✅ CDX 1.7 export + VEX integrace + `sbom_version`/`sbom_author`/`producer`/hashe/licence | Signatura SBOM (E3) |
| SBOM for AI (G7 2026) | ✅ `ai_*` + `dataset_*` + `ai_security_controls`/`ai_performance_metrics` metadata v `ComponentIdentity` | 7 klastrů |
| Federal procurement (EO 14028) | ✅ kompatibilní s CISA minimem | |
| **SG CLS / JP JC-STAR** | | |
| SBOM na L3/L4 (SG CLS) / STAR-1 (JC-STAR) | ✅ CDX export + scanner projection | Binary scan proti SBOM deklarovaným komponentám |

`--release` → `sbom check` vypíše tuto matici s pokrytím; chybějící `support_end_date` u `supported` nebo support period < 5 let → FAIL; chybějící `security_contact`/`eu_responsible_operator` → FAIL. Reportuje také PSTI readiness (pro UK trh), DPP readiness status (ESPR/Battery) a CRA conformity route.

### 4.6 Generate — `sbom/generate.py` (PR A3)

1. `Config` + `manifest.load(db_dir)` — chybí index → „nejprve `fw-context index`".
2. `load_registry()` → collector → resolver → `SbomGraph`.
3. Artefakt: `[sbom.artifact] elf/bin/map` (explicitní; Fáze A bez auto-discovery). Musí existovat → SHA-256 streaming.
4. `sbom-input.json` vedle výstupu:

```json
{
  "_format": "fw-context-sbom-input/1",
  "config_hash": "…",
  "manifest_sha256": "…",
  "component_registry_sha256": "…",
  "artifact_sha256": "…",
  "component_source_trees_sha256": "…",   // agregát: SHA-256 nad setříděnými páry (component_id, source_tree_sha256)
  "build_system": "mbed-os",
  "build_target": "CUSTOM_TARGET",
  "build_profile": "release",
  "sbom_input_hash": "SHA256(config_hash+manifest+registry+artifact+component_source_trees)"
}
```

   `build_system`/`build_target`/`build_profile` se plní z `cfg.build.*` (Config), ne z manifestu. `component_source_trees_sha256` je v input hashi proto, že změna nekompilovaného souboru vendored komponenty mění `source_tree_sha256`, ale ne manifest — bez něj by dva SBOMy měly stejný input hash při různé identitě komponenty.

   (`linker_map_sha256` a `parser_version` přibydou ve Fázi B → `_format` se bumpne na `fw-context-sbom-input/2`; pole jsou výhradně přidáváná, reader musí ignorovat neznámá.)
5. Export → `--output`.
6. Interní kontroly (`validation.py`): unikátní bom-refs, dependency refs existují, root má hash, žádná komponenta bez `version`, **secrets denylist** nad serializovaným výstupem (privátní klíče, tokeny, hesla — cookbook §8.12); `--release` ⇒ `fail_unmapped` + clean worktree + žádný warning mimo tolerovaný `eol-component` s kompletními EOL metadaty (§4.4).

### 4.7 CycloneDX exporter — `sbom/exporters/cyclonedx.py` (PR A3)

- `spec_version` `"1.6"`/`"1.7"`; **default 1.7**; 1.6 = tenký downgrade (1.7-only pole se vynechají). **Zastaralé verze nepoužívat** (CISA 2026 ME: „avoid accepting SBOMs in deprecated versions").
- **CISA 2026 ME v2.1 metadata:** `metadata.timestamp` (RFC 9557), `metadata.tools` (name=`fw-context`, version z `sbom_tool_version`), `metadata.component` nese `producer`/`supplier`, SBOM version (SemVer, `sbom_version`) v `metadata` + property; hash algoritmy IANA/NIST-approved; `sbom_author`/`sbom_signature` → signing hook (E3, gpg/cosign) naplní CISA „SBOM Author Signature".
- `metadata.component` = root (`type: firmware`, `hashes[SHA-256]`, properties `fw-context:config-hash`, `fw-context:evidence-level`, `fw-context:board/target`, `fw-context:generation-context`).
- `components[]`; `bom-ref = "urn:fw-context:<project_id>:<component_id>"` (stabilní přes releasy → diff funguje). Každá komponenta nese hash (`source_tree_sha256`) a SPDX license ID (CISA ME: Component Hash Value/Algorithm + Component License).
- `dependencies[]`: root → top-level; parent → nested.
- `compositions`: jediná s `aggregate: "incomplete"` (Fáze A hardcoded); `coverage`/unknown komponenty explicitně označeny (CISA ME „Explicitly Identifying Unknown Information").
- Properties komponent: `fw-context:evidence`, `fw-context:version-source`, `fw-context:revision`, `fw-context:support-status`, `fw-context:support-end-date`, `fw-context:owner`, `fw-context:producer`, `fw-context:identity-confidence`, `fw-context:source-tree-sha256`, `fw-context:upstream-name`, `fw-context:upstream-version` (poslední dvě jen při vyplněné `[components.upstream]`), `fw-context:data-categories` (jen neprázdné), `fw-context:ai-risk-category`, `fw-context:ai-framework`, `fw-context:dataset-provenance` (jen pro dataset).
- Metadata properties: `fw-context:applied-standards` (z `[sbom.regulatory].applied_standards`), `fw-context:machinery-category`, `fw-context:product-data-categories`, `fw-context:data-access-url`, `fw-context:cra-class`, `fw-context:cra-category`, `fw-context:cra-conformity-route`, `fw-context:security-contact`, `fw-context:eu-responsible-operator`, `fw-context:psti-route`, `fw-context:sbom-version`.
- Interní fork: `version` nese interní verzi (`2.28.8+company.4`), upstream základ je v properties — compliance SBOM popisuje skutečný fork, nikdy netvrdí neupravený upstream.
- `purl_template` render: s opt-in `packageurl-python` (extra `sbom`, §5.4) se výsledek validuje jako PURL; bez ní jen konzervativní string render + warning `purl-unvalidated`. **CPE politika:** compliance SBOM nese CPE pouze explicitní z registry (cookbook §2.4 — žádné hádané CPE); scanner projection nese CPE povinně pro komponenty určené k CVE matchingu (rovněž explicitní z `[components.upstream] cpe` — explicitní záznam není „hádaný", viz projection níže).
- Serial `urn:uuid:<uuid4>`, timestamp UTC. **Testy normalizují** (golden jen stabilní podmnožina).
- JSON schema validace jen s extra `sbom-validation` (`jsonschema`); jinak přeskočena s info. CI validuje externě `cyclonedx-cli`.
- `source_paths` neexportovat (`include_source_paths = false`); jdou do interního `--evidence-output` JSON.

**Scanner projection — `sbom/projection.py` (doporučení §26.4):**

Druhý export nad stejným `SbomGraph`, určený výhradně jako vstup scanneru:

- `name`/`version`/`purl`/`cpe` komponenty pochází z `security_match_*` (upstream identity) — **nikdy** neobsahuje interní version suffix (`+company.N`), aby scanner matchnul veřejné version ranges.
- **CPE je v projekci povinné pro komponenty určené k CVE matchingu** (`security_match_mode` ∈ {exact, upstream-ancestor}): Grype matchuje `pkg:generic` purl pouze přes CPE metadata — komponenta bez CPE je pro Grype neviditelná. CPE se přebírá z explicitního `[components.upstream] cpe` (registry je autorita; žádná automatická derivace — CPE naming je nekonzistentní). Chybějící CPE → warning `projection-unmatched-risk`, v `--release` FAIL (§4.2, §4.4).
- Property `fw-context:component-id` nese stabilní interní ID → doplňkový signál pro zpětné mapování finding → `component_id` v `security.db` (§6.3); primární mapování je purl/CPE/name+version, protože scanner CDX properties do výstupu typicky nepropaguje.
- Komponenty s `security_match_mode = "heuristic"` se do projekce nezahrnují (scanner by produkoval low-confidence šum); jsou vypsány v reportu jako `not-projected`.
- Projection je validní CDX (projde `cyclonedx-cli validate`), ale není určen k publikaci — do `metadata` se zapíše property `fw-context:projection-of: <artifact_sha256>`.
- Konflikt projection ↔ compliance identita (např. `security_match_version` == interní verze s suffixem) → FAIL validace §4.6.

### 4.8 CLI — `cli/_sbom.py` (PR A2+A3)

Vzor `cli/_index.py`: `cmd_sbom(args) → int`, registrace v `cli/__init__.py:main()`:

```text
fw-context sbom init      # A2: components.toml šablona (product + git root + vendor rooty);
                          #     existující soubor nikdy nepřepíše
fw-context sbom check     # A2 [--json] [--strict]
fw-context sbom explain <id>   # A2
fw-context sbom generate  # A3 [--output PATH] [--release-version V] [--release] [--evidence-output PATH]
fw-context sbom scanner-projection  # A3 [--scanner grype] [--output PATH] — projekce pro scanner (§26.4);
                                    #     implicitně ji vytváří i `vuln scan` (Fáze C)
fw-context sbom identify-component <path>  # C3: rekonstrukce provenance vendored komponenty (§26.7);
                                           #     vypíše návrh registry položky, nikdy nezapisuje automaticky
fw-context sbom validate <file>  # A3: interní kontroly §4.6 nad existujícím CDX (bez generate);
                                 #     JSON schema validace s extra sbom-validation, jinak přeskočena
fw-context sbom export doc       # G: předvyplněné podklady pro EU DoC (RED Art. 17, CRA Annex V, EMC/LVD,
                                 #     Machinery Annex IV) — komponenty, applied_standards, support period,
                                 #     výrobce/AR kontakt; digitální-first (NLF omnibus COM(2025) 503)
```

### 4.9 Konfigurace — `config/settings.py`

Nový dataclass `SbomConfig` + sekce v `Config`, naplněná stávajícím `_apply_section` mechanismem:

```toml
[sbom]
component_registry = ".fw-context/components.toml"
spec_version = "1.7"
output = "release/firmware.cdx.json"
fail_unmapped = false   # default: unmapped = warning; --release/--strict vynutí error (exit 2), viz §9
include_source_paths = false
include_headers = false   # detail hlaviček v exportu; detekce header-only komponent běží vždy

[sbom.product]
version_command = "git describe --tags --always --dirty"
support_end_date = "2029-12-31"  # CRA Art. 13(8): konec podpory produktu; povinné pro --release
placed_on_market_date = "2025-06-01"   # pro výpočet support period ≥ 5 let (CRA Art. 13(8))
# support_shorter_than_5y_reason = "produkt očekáván v užití 3 roky (baterie)"  # výjimka, zapsáno do evidence (Annex VII(4))
security_contact = "security@example.com"  # CRA Annex VII 2(b) + PSTI + GPSR
eu_responsible_operator = "Example s.r.o., Prague, CZ"  # GPSR Art. 16 / CRA Art. 22 / PLD Art. 8

[sbom.artifact]
elf = "BUILD/CUSTOM_TARGET/GCC_ARM/firmware.elf"
binary = "BUILD/CUSTOM_TARGET/GCC_ARM/firmware.bin"
map = "BUILD/CUSTOM_TARGET/GCC_ARM/firmware.map"
role = "application"

[sbom.regulatory]
# Multi-regulation compliance metadata
applied_standards = ["EN 18031-1", "EN 18031-2", "ETSI EN 303 645"]
#   harmonizované normy aplikované na produkt (CRA Annex VII + RED)
machinery_category = ""              # kategorie stroje dle Machinery Reg. Annex I (seznam vysoce rizikových strojů);
                                     #   technická dokumentace je Annex IV (Art. 21); prázdné = produkt není strojní zařízení
product_data_categories = ["telemetry", "location"]
#   Data Act: kategorie dat produkovaných produktem
data_access_url = ""                 # Data Act: URL/endpoint pro přístup k datům
cra_class = "default"                # Reg. (EU) 2025/2392: "default"|"important-i"|"important-ii"|"critical"
cra_category = ""                    # kategorie Annex III/IV (core-functionality test je lidské rozhodnutí)
psti_route = "direct"                # UK PSTI: "direct"|"cls-label"|"jc-star" (SI 2025/1267)
```

`[sbom.artifact]` povinná pro `generate` v Fázi A (jinak FAIL s návodem).

### 4.10 Testy — `tests/test_sbom/`

```text
tests/test_sbom/
├── fixtures/
│   ├── registry_basic.toml / registry_conflict.toml / registry_nested.toml
│   ├── registry_upstream.toml     # duální identita: interní fork + upstream sekce + patch_dir
│   ├── manifest_basic.json        # 6 TUs napříč 3 komponentami
│   ├── version_header.h
│   ├── artifact.bin               # pevný obsah → pevný SHA-256
│   ├── patches/                   # 2 patch soubory → patchset_sha256
│   ├── vendored-exact-upstream/   # doporučení §26.9 — Případ A: přesná kopie, security_match_mode exact
│   ├── vendored-patched-fork/     # Případ B: interní verze + upstream ancestor + patch_dir
│   ├── vendored-unknown-origin/   # Případ C: hash-fallback, identity_confidence low
│   ├── vendored-backported-cve-fix/  # Případ B + patch obsahující backport fix (pro D1)
│   ├── vendored-version-suffix/   # interní suffix „+company.N" → projection bez suffixu
│   ├── registry_ai_model.toml     # AI Act: ai-model komponenta s rizikovou kategorií + training data
│   └── registry_data_act.toml     # Data Act: product.data_categories + komponenty s data_categories
├── test_model.py / test_registry.py / test_collector.py / test_resolver.py
├── test_report.py / test_generate.py / test_cyclonedx.py / test_cli_sbom.py
```

Povinné scénáře: nested precedence; konflikt stejné specificity; prefix-glob negativ (`app/src/main.c` nesmí matchnout `src/**`); chybějící verze → FAIL; hash-fallback bez gitu; unmapped → exit 2 při `fail_unmapped`; změna artefaktu → změna `sbom_input_hash`; deterministický export (2× generate → stejné `components`+`dependencies`); blob hash + chybějící blob → FAIL v `--release`; header-only komponenta doložená include graphem; `[components.security]` validace; internal-fork s čistou upstream verzí → FAIL; internal-fork bez `[components.upstream]` → FAIL v `--release`; secrets denylist → FAIL. Vendored scénáře (doporučení §26.9): projection nemá interní version suffix a nese `fw-context:component-id`; `heuristic` komponenta není v projekci (`not-projected`); unknown-origin má `source_tree_sha256` + `identity_confidence low`; release policy §26.8 → FAIL bez stabilní identity; konflikt projection ↔ compliance → FAIL; `patchset_sha256` deterministický a mění se s obsahem `patch_dir`. AI Act scénáře: ai-model bez `ai_risk_category` → warning; `ai_risk_category ∈ {high, limited}` bez `ai_training_data_summary` → warning; neznámý `ai_framework` → warning; Data Act scénáře: produktová `data_categories` se propagují do CDX properties; komponenta s `data_categories` nese CDX property `fw-context:data-categories`. **CRA/regulatorní scénáře (rev. 11):** `support_end_date < placed_on_market_date + 5 let` → warning `short-support-period`, v `--release` FAIL bez `support_shorter_than_5y_reason`; chybějící `security_contact`/`eu_responsible_operator` → FAIL v `--release`; `cra_class = "important-i"` bez `cra_category` → FAIL v `--release`; neznámý `cra_class` → warning; `cra_conformity_route` derivace (default→module-a, important-i→eu-type, important-ii→full-quality-assurance, critical→eucc-certificate); CDX export nese `fw-context:cra-class`/`cra-category`/`cra-conformity-route`; CISA 2026 ME: `sbom_version`/`sbom_author`/`producer` v exportu, SPDX license ID, komponentní hash; dataset komponenta bez `dataset_provenance` → FAIL v `--release`. Git testy přes `git init` v `tmp_path`.

### 4.11 PR rozpad Fáze A

| PR | Obsah | Závislost |
|---|---|---|
| **A1** | `sbom/model.py` (vč. upstream/provenance/scanner-match + AI Act/Data Act polí), `sbom/registry.py` (vč. `[components.upstream]`, `[components.ai]`, `data_categories` validace) + testy. Žádné CLI. | — |
| **A2** | `collector.py`, `resolver.py` (vč. `source_tree_sha256`/`local_revision`/`patchset_sha256` + release policy §26.8), `report.py`, `SbomConfig`, `cli/_sbom.py` (`init`, `check`, `explain`) + testy | A1 |
| **A3** | `exporters/cyclonedx.py`, `projection.py`, `generate.py`, `validation.py`, `sbom generate` + `sbom scanner-projection` + testy, docs | A2 |

### 4.12 Acceptance criteria Fáze A

1. Každá aktivní TU má vlastníka nebo je reportována (exit 2 v release režimu).
2. Verze ze 4 zdrojů + zaznamenaný `version_source`.
3. CDX 1.7 i 1.6 projdou `cyclonedx-cli validate`.
4. Root nese SHA-256 artefaktu; `sbom_input_hash` v `sbom-input.json`.
5. `composition: incomplete` — žádné falešné `complete`.
6. Celý pipeline offline.
7. Každý `deployed_always` blob má SHA-256 nebo FAIL v `--release` (cookbook §15.2, §18.1).
8. Header-only komponenta doložená include graphem je v SBOM s evidence `compiled`.
9. Interní fork s čistou upstream verzí → FAIL; EOL komponenta → warning.
10. Export prošel secrets denylist kontrolou.
11. Žádná změna DB schématu; index a MCP netknuté (`make test` zelené).
12. Ověřeno na **HA_Boiler** (ESP32, WiFi — radio equipment, v režimu RED→CRA překryvu) a **FM** (STM32, GNU ld) (read-only).
13. Scanner projection: žádný interní version suffix, `fw-context:component-id` přítomen, `heuristic` komponenty vynechány, konflikt s compliance identitou → FAIL; komponenty určené k CVE matchingu nesou CPE (bez CPE → FAIL v `--release`).
14. Komponenta neznámého původu (Případ C) nese `source_tree_sha256` jako stabilní identitu a `identity_confidence: low`; v `--release` FAIL dle release policy §26.8.
15. `[sbom.regulatory].applied_standards` se exportují jako CDX `metadata.properties` (`fw-context:applied-standards`).
16. AI-model komponenta validuje `ai_risk_category` a `ai_training_data_summary` dle AI Act; chybějící metadata → warning/FAIL dle rizikové kategorie.
17. `[sbom.regulatory].product_data_categories` a `ComponentIdentity.data_categories` se exportují jako CDX properties (`fw-context:data-categories`).
18. **CRA Annex I Part II(1):** export obsahuje všechny komponenty (dependencies kompletní, machine-readable CDX) — SBOM jako explicitní povinnost.
19. **CRA Art. 13(8):** `support_end_date` produktu ≥ 5 let od `placed_on_market_date`; kratší → FAIL v `--release` bez odůvodnění (jeden řádek v evidence reportu jako Annex VII(4)).
20. **Reg. (EU) 2025/2392:** `cra_class`/`cra_category` se exportují; `cra_conformity_route` se odvodí a reportuje.
21. **CISA 2026 ME v2.1:** export nese `sbom_version`, tool name/version, `producer`, komponentní hashe, SPDX licence; `sbom check --release` reportuje ME compliance.
22. **PSTI/GPSR:** `security_contact` + `eu_responsible_operator` povinné v `--release`; `psti_route` validováno.

---

## 5. Fáze B — linker evidence (`linked`/`retained`) + artefakt discovery

**Cíl:** povýšit evidence z `compiled` na `linked`/`retained`, doplnit `linker_map_sha256` do identity a umožnit `composition: complete` při 100 % mapování. Vedle toho opt-in auto-discovery artefaktů přes adaptéry, aby `[sbom.artifact]` nemusela být povinná u jednoznačných projektů.

### 5.1 GNU ld map parser — `sbom/linker/gnu_map.py` (PR B1)

Rozparsované sekce map souboru (GNU ld i lld — lld je téměř kompatibilní, odlišnosti pokryjí fixtures):

| Map sekce | Význam pro SBOM |
|---|---|
| `Archive member included to satisfy reference by file (symbol)` | které členy `.a` byly skutečně vytaženy → `linked` |
| `Discarded input sections` | vstupy vyhozené `--gc-sections` → **ne** `retained` |
| `Linker script and memory map` | output sekce + řádky `.text.foo 0xADDR SIZE obj.o` / `archive.a(member.o)` → `retained` + velikost |
| `Memory Configuration` | jen diagnostika (regiony FLASH/RAM) |
| `LOAD path/to/lib.a` / `START GROUP` | load-only metadata → ignorovat, nepočítat jako objekty |

Výstupní model (`sbom/linker/base.py`):

```python
@dataclass(frozen=True, slots=True)
class LinkedInput:
    object_path: str            # normalizovaná cesta (relativní k project_root pokud možno)
    archive_path: str | None
    archive_member: str | None
    output_sections: tuple[str, ...]  # alokované output sekce, do kterých přispěl
    retained_bytes: int
    discarded: bool             # všechny jeho sekce skončily v Discarded
    generated: bool             # linker stubs / <internal> / LTO plugin objekty

class MapParser(Protocol):
    PARSER_VERSION: str
    def parse(self, map_path: Path) -> MapParseResult: ...
```

`MapParseResult` = `inputs: list[LinkedInput]`, `warnings: list[str]`, `format_detected: str`.

Pravidla parseru:

- **Cesty:** map obsahuje cesty relativní k link-time cwd → resolver zkusí `project_root`, pak build dir (z `build_dir_patterns`), pak označí `unresolved` (→ unmapped linker input, ne tiché vynechání).
- **LTO:** objekty `*.ltrans*.o`, `cc*.o` v tmp, `<artificial>` → `generated=True`, warning `lto-limited-provenance`; evidence pro dotčené komponenty se degraduje: `retained` s confidence `low` (zůstane `compiled`-level provenance ze symbolů).
- **COMMON / `*fill*`** → ignorovat pro vlastnictví.
- **Neznámý formát/řádek:** konzervativní — zaznamenat warning, nepřerušit parse; ale pokud se nesparsuje >50 % vstupních řádků output sekcí → celý parse = `failed` → evidence zůstane `compiled`, composition `incomplete`. **Nikdy falešné `retained`.**
- `PARSER_VERSION` se zapíše do evidence reportu a `sbom-input.json` (změna parseru mění reprodukovatelnost).

### 5.2 Integrace evidence — `resolver.py` + `model.py` (PR B2)

Post-processing po Fázi A mappingu:

1. `LinkedInput.object_path` → původní zdroj: mapování objekt→zdroj přes manifest (`entries[].file` ↔ objekt cesta: stejný relativní kmen, přípona `.o/.obj`; kolize → warning, preferovat přesnou shodu z linker command line pokud adapter dodá).
2. Vlastník zdroje = vlastník objektu → evidence:
   - objekt v map, `discarded=False`, přispěl do **runtime sekce** ≥1 B → komponenta `retained`;
   - objekt v map, ale jen discarded → komponenta max `linked`;
   - zdroj kompilován, objekt v map chybí → komponenta `compiled` (typické u header-only/LTO).
3. Runtime sekce allowlist:

```toml
[sbom.linker]
# cesta k mapě je výhradně [sbom.artifact].map (§4.9) — žádný druhý zdroj
runtime_sections = [".text*", ".rodata*", ".data*", ".bss*", ".noinit*",
                    ".ramfunc*", ".vectors*", ".init_array*", ".fini_array*"]
```

   Debug sekce (`.debug_*`, `.comment`, `.ARM.attributes`) se do runtime presence nepočítají.
4. `linker_inputs_unmapped` — linker vstupy bez vlastníka (toolchain runtime `libgcc`, `crt0` → výchozí allowlist v `[sbom.linker] ignore_inputs = ["*/libgcc/*", "*/crt*.o", "*/libstdc++/*"]`, reviewovatelný). Cokoli jiného unmapped → FAIL při `--release`, jinak warning.
5. `composition`:
   - `complete` ⇔ 100 % linker vstupů namapováno (po allowlistu) **a** všechny komponenty mají verzi;
   - `incomplete` ⇔ map parsována, mezery existují;
   - `unknown` ⇔ map chybí/neparsovatelná.
6. `sbom-input.json` += `linker_map_sha256`, `parser_version`; `sbom_input_hash` přepočet dle §4.6 rozšířeného vzorce.

### 5.3 Artefakt discovery + adaptéry — `sbom/adapters/` (PR B2)

```python
class SbomSupport(Protocol):
    """Opt-in SBOM capability — buildery se nemodifikují."""

    name: str

    @classmethod
    def detect(cls, project_root: Path, cfg: Config) -> bool: ...

    def discover_release_artifacts(self, project_root: Path, cfg: Config) -> list[ReleaseArtifact]: ...

    def suggest_component_rules(self, project_root: Path, cfg: Config) -> list[ComponentRule]: ...

@dataclass(frozen=True, slots=True)
class ReleaseArtifact:
    role: str
    elf: Path | None
    binary: Path | None
    hex: Path | None
    map_file: Path | None
    environment: str | None     # PlatformIO env (Fáze E)
    board: str | None
```

- `generic.py`: glob `*.elf`/`*.map` v adresářích z `get_build_dir_patterns()` aktivního builderu; 1 nalezený ELF → auto; >1 → FAIL se seznamem kandidátů a návodem k `[sbom.artifact]`.
- `mbed_os.py`: `BUILD/<target>/<toolchain>/*.elf` s target/toolchain z aktivního buildu (`.mbed`/`mbed_app.json`); `suggest_component_rules()` navrhne nested pravidla pro `mbed-os/connectivity/mbedtls/**`, `lwipstack/**`, `littlefs/**`, `cmsis/**` do `sbom init`.
- Priorita: explicitní `[sbom.artifact]` > adapter discovery. Adapter nikdy nepřepíše config.
- `BuildConfig.environment: str | None = None` (additive; využije se ve Fázi E, přidáno zde protože `ReleaseArtifact.environment` na něj navazuje).

### 5.4 ELF — `sbom/linker/elf.py` (PR B3)

Opt-in extra `sbom = ["pyelftools>=0.31", "packageurl-python>=0.16"]`:

- ověření, že ELF SHA-256 odpovídá artefaktu (sanity);
- čtení section headers → cross-check `retained_bytes` z mapy (tolerance, jinak warning `map-elf-mismatch`);
- detekce stripped debug info (info hláška; bez vlivu na evidence).
- Bez pyelftools: pouze hash, žádný FAIL.

### 5.5 Testy Fáze B

```text
tests/test_sbom/fixtures/
├── gnu-map-basic.map         # standalone .o + output sekce
├── gnu-map-archive.map       # archive.a(member.o) vytažení
├── gnu-map-gc-sections.map   # discarded sekce
├── gnu-map-lto.map           # ltrans objekty
├── gnu-map-lld.map           # lld varianty
└── gnu-map-common.map        # COMMON, fill
```

Scénáře: archive member mapping; discarded ≠ retained; LTO → warning + confidence low; unresolved path → unmapped linker input; runtime allowlist respektován; 100 % mapování → `complete`; parsovací selhání >50 % → fallback `compiled`+`unknown`; `sbom_input_hash` se mění s mapou; generic adapter single/multi ELF; mbed adapter nested suggestions.

### 5.6 PR rozpad Fáze B

| PR | Obsah | Závislost |
|---|---|---|
| **B1** | `linker/base.py`, `linker/gnu_map.py` + fixtures + testy parseru (čistý, bez integrace) | A3 |
| **B2** | evidence upgrade v resolveru, `[sbom.linker]` config, `sbom-input.json` rozšíření, adaptéry base+generic+mbed_os, `BuildConfig.environment` | B1 |
| **B3** | `linker/elf.py` (opt-in), map↔ELF cross-check, completeness v `sbom check` reportu | B2 |

### 5.7 Acceptance criteria Fáze B

1. Komponenta s objekty v runtime sekci má evidence `retained` + `retained_bytes`.
2. Discarded-only komponenta má max `linked`; nikdy `retained`.
3. `composition: complete` jen při 100 % mapování; jinak poctivě `incomplete`/`unknown`.
4. LTO build → viditelný warning, žádné falešné `retained`.
5. `[sbom.artifact]` nepovinná u single-image projektů (adapter), povinná/nejednoznačná u multi-image.
6. Ověřeno na **FM** (STM32, GNU ld map) a **HA_Boiler**.

---

## 6. Fáze C — vulnerability scan (Grype) + `security.db`

**Cíl:** nad vygenerovaným SBOM spustit externí scanner, archivovat raw výstupy a normalizovat findings s match confidence. Scanner je vyměnitelný, offline `sbom generate` zůstává nedotčen.

**Omezení scanneru:**
- Grype detekuje pouze softwarové CVE (knihovny, frameworky) — **nedetekuje AI/ML model zranitelnosti** (modelcards, model signing, adversarial robustness). Pro ai-model komponenty nutný specializovaný nástroj (mimo scope).
- Grype výstupní formát se může měnit mezi verzemi → **verze Grype musí být pinnutá** v `[vuln.scanner]` configu (`grype_version = "0.80.0"`). Při změně verze se invalidují předchozí scan výsledky.
- `pkg:generic` PURL bez CPE není Grype schopen matchovat → komponenty určené k CVE matchingu musí mít explicitní CPE v `[components.upstream]` (již v release policy §4.4).

### 6.1 `security.db` — `vulnerabilities/db.py` (PR C1)

Umístění: `.fw-context/<project-id>/security.db` (vedle `index.db`). Vlastní schema verze v `schema_meta` (nezávislá na index schema version). Konvence: WAL, `foreign_keys=ON`, parametrované dotazy.

```sql
CREATE TABLE schema_meta (version INTEGER NOT NULL);

CREATE TABLE release_artifacts (
    artifact_sha256   TEXT PRIMARY KEY,
    sbom_input_hash   TEXT NOT NULL,
    path              TEXT NOT NULL,
    role              TEXT NOT NULL DEFAULT 'application',
    sbom_path         TEXT NOT NULL,
    created_at        TEXT NOT NULL           -- ISO-8601 UTC
);

CREATE TABLE vulnerability_scans (
    scan_id           INTEGER PRIMARY KEY,
    scanner           TEXT NOT NULL,          -- "grype" | "osv"
    scanner_version   TEXT NOT NULL,
    db_timestamp      TEXT,                   -- stáří vulnerability DB scanneru
    artifact_sha256   TEXT NOT NULL REFERENCES release_artifacts(artifact_sha256),
    raw_report_path   TEXT NOT NULL,
    config_json       TEXT NOT NULL,          -- scanner flags, policy
    created_at        TEXT NOT NULL
);

CREATE TABLE vulnerability_findings (
    scan_id           INTEGER NOT NULL REFERENCES vulnerability_scans(scan_id),
    vulnerability_id  TEXT NOT NULL,          -- CVE-…, GHSA-…
    euvd_id           TEXT,                   -- EUVD ID (European Vulnerability Database, CRA Art. 14/16) — SRP template pole v14
    component_id      TEXT NOT NULL,          -- bom-ref z našeho SBOM
    component_name    TEXT NOT NULL,
    component_version TEXT NOT NULL,
    purl              TEXT,
    cpe               TEXT,
    match_type        TEXT NOT NULL,          -- viz 6.3
    severity          TEXT,
    cvss              REAL,
    fixed_versions    TEXT NOT NULL DEFAULT '[]',  -- JSON array
    raw_json          TEXT NOT NULL,
    PRIMARY KEY (scan_id, vulnerability_id, component_id)
);

CREATE TABLE vulnerability_assessments (   -- Fáze D, DDL zavedeno zde
    assessment_id           INTEGER PRIMARY KEY,
    artifact_sha256         TEXT NOT NULL REFERENCES release_artifacts(artifact_sha256),
    vulnerability_id        TEXT NOT NULL,
    component_id            TEXT NOT NULL,
    state                   TEXT NOT NULL,          -- affected|not_affected|fixed|under_investigation
    lifecycle               TEXT NOT NULL,          -- unreviewed|proposed|approved|rejected|superseded
    justification           TEXT,
    detail                  TEXT,
    evidence_json           TEXT NOT NULL DEFAULT '{}',
    evidence_bundle_sha256  TEXT,                    -- SHA-256 evidence bundlu z vuln analyze
    proposed_by             TEXT,                    -- kdo navrhl (--by u propose)
    proposed_at             TEXT,                    -- timestamp návrhu
    approver                TEXT,
    approved_at             TEXT,                    -- timestamp schválení
    sla_due_date            TEXT,                    -- ISO 8601 — deadline pro rozhodnutí
    superseded_from         INTEGER REFERENCES vulnerability_assessments(assessment_id),
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

CREATE INDEX idx_findings_cve ON vulnerability_findings(vulnerability_id);
CREATE INDEX idx_findings_component ON vulnerability_findings(component_id);
CREATE INDEX idx_assessments_cve ON vulnerability_assessments(vulnerability_id, artifact_sha256);
```

Migrace: `schema_meta.version` + additive `ALTER TABLE` skripty (stejná disciplína jako `indexer/db/_schema.py`).

### 6.2 Scanner protokol + Grype adapter (PR C2)

`vulnerabilities/scanner.py`:

```python
class VulnerabilityScanner(Protocol):
    scanner_id: str
    def probe(self) -> ScannerInfo | None: ...  # executable, verze, DB timestamp; nenalezen → None
    def scan(self, sbom_path: Path, output_path: Path, timeout_seconds: int) -> ScanExecution: ...
    def parse(self, output_path: Path) -> list[VulnerabilityFinding]: ...
```

`vulnerabilities/grype.py`:

- `shutil.which("grype")`; chybí → srozumitelná chyba s instalačním návodem (nikdy auto-install).
- `grype version -o json` → `scanner_version`; `grype db status -o json` → `db_timestamp`; stáří > `[vuln] max_db_age_days` (default 7) → warning.
- Scan: `grype sbom:<path> -o json --file <out>` (syntax přesně dle detekované major verze; při neznámé verzi FAIL s hláškou, ne hádat flagy). Subprocess: list argv, bez shellu, timeout (default 600 s), env passthrough minimal (`HOME`, `PATH`, `GRYPE_DB_*`).
- **Vstup scanu je scanner projection** (§4.7), ne compliance SBOM: `vuln scan` si projekci vygeneruje interně (dočasný soubor), pokud projekt obsahuje komponenty s `[components.upstream]` nebo `internal_version`; jinak použije compliance SBOM přímo. Findings se mapují zpět na interní komponenty přes **purl → CPE → name+security_match_version** proti projekci (§6.3); property `fw-context:component-id` je jen doplňkový signál — Grype CDX properties do výstupu standardně nepropaguje, funkčnost se ověří v C2 PoC (§6.7).
- `ScanExecution` zaznamená: argv, exit code, duration, stderr tail (do logu, ne do DB).

**OSV HTTP API — `vulnerabilities/osv.py` (PR C3, doporučení §26.5):**

Doplňkový scanner pro komponenty se známým `upstream.repository + upstream.revision` (vendored snapshoty a forky — přesnější než CPE match). **Nepoužívá osv-scanner CLI** — `osv-scanner scan source` nepodporuje manifest párů (repo, commit); commit matching jde přímo přes OSV HTTP API:

- Pro každou komponentu s `upstream_revision` jeden dotaz: `POST https://api.osv.dev/v1/querybatch` s `{"queries": [{"commit": "<upstream_revision>"}]}` (commit query podporováno, `package` optional, `version` se nezadává). Batch minimalizuje počet requestů; timeout + retry konvence jako u Grype subprocessu. Scan fáze je inherentně online (stejně jako Grype DB) — zdokumentovaná výjimka z offline-by-default, offline režim = OSV scan přeskočen s info hláškou.
- Adapter implementuje `VulnerabilityScanner` protokol; `scan()` nad interním commit manifestem z `SbomGraph` (ne nad SBOM souborem — protokol rozšířen o `input_kind: "sbom" | "commits"`).
- Findings se ukládají do stejných tabulek (`scanner = "osv"`), `match_type = "exact-commit"` — vyšší confidence než `exact-purl`.
- Komponenty bez `upstream_revision` se OSV scanu neúčastní (vypsány jako `skipped`).

### 6.3 Normalizace — `vulnerabilities/normalize.py` (PR C1)

```python
@dataclass(frozen=True, slots=True)
class VulnerabilityFinding:
    scanner: str
    vulnerability_id: str       # CVE-…, GHSA-…
    euvd_id: str | None = None  # EUVD ID (European Vulnerability Database) — SRP template pole v14
    namespace: str | None       # "nvd:cpe", "github:language", …
    component_id: str           # bom-ref (spárováno přes purl/name+version zpět na náš SBOM)
    component_name: str
    component_version: str
    purl: str | None
    cpe: str | None
    match_type: str             # viz níže
    severity: str | None
    cvss: float | None
    fixed_versions: tuple[str, ...]
    raw_record: Mapping[str, Any]
```

`match_type` (odvozen z Grype `matchDetails`/`matcher`):

```text
exact-commit > exact-purl > exact-cpe > generated-cpe > name-version > fuzzy > unknown
```

Pravidla:

- `generated-cpe` a níže → finding v reportech viditelně označen `[LOW CONFIDENCE]`.
- Finding, jehož komponentu nelze spárovat zpět na bom-ref z našeho SBOM → `component_id = ""` + warning `unmatched-finding` (nikdy zahodit — může jít o naši chybu v SBOM). **Primární mapování:** purl findingu proti purl projekce → CPE proti CPE → `name + security_match_version`; property `fw-context:component-id` je best-effort signál navíc (scanner CDX properties typicky nepropaguje do výstupu — ověří C2 PoC, §6.7). Shoda přes `security_match_*` se zaznamená do match provenance (finding raw_json + `match_type`).
- Stejné CVE na stejné komponentě z více matcherů v rámci jednoho scanu (např. CPE + purl současně) → **upsert merge** (PK `(scan_id, vulnerability_id, component_id)` by jinak kolidoval): vyhraje záznam s vyšším `match_type`, `fixed_versions` se sjednotí, `raw_json` nese pole všech sloučených záznamů.
- Duplicitní CVE napříč namespacy → sloučit, namespace zaznamenat.
- Stejné CVE nalezené Grypem i OSV → dva findings (různý `scan_id`), report je seskupuje; shoda napříč scannery zvyšuje confidence, je označena v `vuln show`.

### 6.4 CLI — `cli/_vuln.py` (PR C2)

```text
fw-context vuln scan [--scanner grype|osv] [--sbom PATH] [--timeout N]
    # default SBOM = [sbom].output; u upstream/internal identity se použije interně
    # vygenerovaná scanner projection (§6.2); po scanu: release_artifacts + scans + findings insert
fw-context vuln list [--component ID] [--severity LEVEL+] [--artifact-sha PREFIX]
fw-context vuln show <CVE-ID>            # normalizovaný detail + raw JSON (--raw); zobrazí i EUVD ID
fw-context vuln report-srp <CVE-ID> [--stage early-warning|notification|final] [--artifact-sha PREFIX]
    # Fáze D+: předvyplněný draft CRA Art. 14 notifikace (SRP template: 24h/72h/final pole)
    # z findings + VEX assessmentu + SBOM (product, CRA class/category, CVE/EUVD ID, severity,
    # corrective measures, Member States). SRP nemá API (potvrzeno ENISA) — výstup je JSON/formulář
    # k manuálnímu vložení. Reporting zůstává lidským krokem.
fw-context vuln gate [--release]         # CI/release gate (cookbook §17.2), exit 0/2:
                                         #   FAIL scanner DB starší než max_db_age_days
                                         #   FAIL finding severity >= min_severity bez assessmentu
                                         #   FAIL unmatched-finding (komponenta nespárována na SBOM)
                                         #   Fáze D rozšíří o: kritické CVE bez rozhodnutí/vlastníka,
                                         #   překročený termín under_investigation
```

`vuln list` defaultně seskupí podle komponenty, seřadí podle severity, `[LOW CONFIDENCE]` označí.

Config:

```toml
[vuln]
scanner = "grype"
grype_version = "0.80.0"                  # pinnutá verze — změna invaliduje předchozí scany
max_db_age_days = 7
archive_dir = "release/vulnerabilities"   # raw reporty
timeout_seconds = 600

[vuln.gate]
min_severity = "high"                     # findings >= této severity vyžadují assessment
max_under_investigation_days = 30         # Fáze D: termín pro under_investigation
```

**Omezení Grype:**
- Grype **nedetekuje AI/ML model zranitelnosti** — ai-model komponenty nelze skenovat. Pro modely nutný specializovaný nástroj (mimo scope).
- Grype matchuje `pkg:generic` PURL pouze přes CPE — komponenty k CVE matchingu musí mít explicitní CPE (již v release policy §4.4).
- Grype podporuje pouze software CVEs, nikoli hardware/firmware CVEs ovlivňující MCU, bootloader nebo radio firmware.

Součástí Fáze C je i **`fw-context sbom enrich --online`** (rozšíření `cli/_sbom.py`): explicitní opt-in online krok, který pro komponenty bez PURL/CPE navrhne identifikátory z veřejných zdrojů a vypíše je jako návrhy — nikdy je nezapíše do registry automaticky (cookbook §13.16). Default zůstává plně offline.

**`fw-context sbom identify-component <path>` — `sbom/identify.py` (PR C3, doporučení §26.7):**

Rekonstrukce provenance pro vendored komponenty neznámého původu (Případ C — typicky `lib/zcbor`, `lib/fat/ChaN` v Z-Box ECB). **Čistě heuristická — vždy návrh k manuálnímu schválení, nikdy automatický zápis identity.**

1. hledání version headerů a copyright/project markerů;
2. porovnání známých file names a adresářové struktury;
3. porovnání source tree proti známým upstream tagům (lokální mirror/klon upstreamu zadaný parametrem — nástroj sám nic neklonuje bez `--online`);
4. určení nejbližšího upstream commitu/verze + diff přehled (files compared / exact matches / modified / unknown);
5. `source_tree_sha256` aktuálního stromu;
6. **návrh** registry položky (TOML na stdout) vč. `[components.upstream]` a `identity_confidence`.

Identita se nikdy nepotvrdí automaticky — výstup končí `Manual review required: yes`. CVE Binary Tool smí být použit jen jako doplňková heuristika identifikace (evidence source `binary heuristic`, confidence low/medium), nikdy jako autoritativní zdroj identity (doporučení §26.5, §26.7).

### 6.5 Testy Fáze C

- `test_normalize.py`: fixture `grype-report.json` (reálný zmenšený výstup) → findings; match_type odvození; unmatched finding → warning; deduplikace namespaců.
- `test_db.py`: migrace z prázdné DB; CRUD; cizí klíče; dva scany nad stejným artefaktem.
- `test_grype.py`: parse fáze offline z fixture; probe/scan za `pytest.mark.skipif(shutil.which("grype") is None)` (stejný vzor jako existující skipy ollama/libclang).
- `test_osv.py`: fixture `osv-querybatch-response.json` → findings s `match_type = "exact-commit"`; komponenta bez `upstream_revision` → `skipped`; offline režim → skip s info; live API test za `pytest.mark.skipif` dle env flagu (network, defaultně přeskočen — vzor ollama skipů).
- `test_identify.py`: fixture vendored stromy (§4.10) → návrh upstream identity + confidence; nic nezapisuje; bez upstream mirroru → pouze `source_tree_sha256` + marker-based návrh s confidence low.
- `test_cli_vuln.py`: exit kódy, defaulty z configu; scan nad projekcí mapuje findings přes purl/CPE/name+version (§6.3).

### 6.6 PR rozpad Fáze C

| PR | Obsah | Závislost |
|---|---|---|
| **C1** | `vulnerabilities/db.py` (schema+migrace+CRUD), `normalize.py`, fixtures, testy | B1 (libovolný bod po A3) |
| **C2** | `scanner.py`, `grype.py`, `cli/_vuln.py`, `[vuln]` config, testy, docs | C1 |
| **C3** | `vulnerabilities/osv.py`, `sbom/identify.py`, `sbom identify-component`, testy | C2 |

### 6.7 Acceptance criteria Fáze C

1. `vuln scan` funguje nad naším CDX 1.7 i 1.6 výstupem; raw report archivován.
2. Každý finding má `match_type`; low-confidence viditelně označen.
3. Findings navázány na `artifact_sha256` (ne `config_hash`).
4. Stará scanner DB → warning; chybějící grype → čistá chyba bez tracebacku.
5. `security.db` nezávislá na `index.db` — reindex nemění vulnerability data.
6. `vuln gate` vrací exit 2 při porušení gate pravidel z §6.4; pravidla odpovídají cookbook §17.2.
7. Scan nad scanner projection: findings se mapují zpět na interní `component_id` přes purl/CPE/name+version; interní version suffix nezpůsobí miss. **C2 obsahuje PoC**: reálný Grype nad vygenerovanou projekcí — ověří, že CPE-bearing komponenty matchují (jinak by projekce byla pro Grype neviditelná) a zda Grype propíše `fw-context:component-id` property do výstupu (dle výsledku se upraví primární mapping v §6.3).
8. OSV `exact-commit` match (HTTP API) pro komponentu s `upstream_revision` má vyšší confidence než Grype CPE match nad stejnou komponentou.
9. `identify-component` nad vendored fixture `unknown-origin` navrhne upstream + `identity_confidence`, ale nic nezapíše; výstup vyžaduje ruční review.

---

## 7. Fáze D — applicability evidence + VEX + MCP

**Cíl:** největší přidaná hodnota fw-context — pro konkrétní CVE připravit build-aware důkazy (kompilováno? linkováno? symbol přítomen? makro aktivní? call path? patch?) a workflow pro člověkem schválený VEX.

### 7.1 Applicability — `vulnerabilities/applicability.py` (PR D1)

**Omezení:** Analýza spoléhá na symbolový callgraph z `index.db`. Callgraph pokrývá C/C++ funkce a function-pointer edges — **nepokrývá**: makra expandovaná preprocesorem, `#ifdef`-podmíněný kód mimo aktivní konfiguraci, inline assembly bez symbolů, dynamicky generovaný kód. Výsledkem jsou možné false negatives (CVE označena jako `not_affected`, ale ve skutečnosti je kód přítomen přes makro). V takových případech nástroj vrátí `unknown` s vysvětlením.

Vstupy pro analýzu CVE:

- finding z `security.db` (komponenta, verze, fixed_versions);
- **hints** — dotčené soubory/symboly/makra. Zdroj: CLI flagy + perzistentní sekce v `components.toml`:

```toml
[[components.cve_hints]]
cve = "CVE-2024-28956"
files = ["library/ssl_srv.c"]
symbols = ["mbedtls_ssl_parse_certificate"]
macros = ["MBEDTLS_SSL_SRV_C"]
```

  (Automatické parsování advisories je mimo scope — hints zadává člověk z CVE textu; jsou reviewovatelné v gitu.)

Kontroly (každá vrací `yes/no/unknown` + evidence ref):

| Kontrola | Zdroj pravdy |
|---|---|
| Komponenta retained/linked/compiled | `SbomGraph` evidence (regenerovaná z posledního generate nebo `--sbom` evidence report) |
| Dotčený soubor kompilován | `manifest.json` entries |
| Dotčený objekt linkován/retained | linker evidence (Fáze B; bez ní `unknown`) |
| Symbol přítomen v indexu | `indexer/db` symbol lookup (sdílená logika s MCP `lookup_symbol`) |
| Požadované makro aktivní | `manifest.json["macros"]` |
| Call path z entrypointu | callgraph tabulky (sdílená logika s `find_call_path`); hloubka configurovatelná |
| Lokální patch dotčené funkce | patch-aware analýza (doporučení §26.6): `patch_dir` soubory se parsují (unified diff) a porovnají s hints soubory/symboly — patch dotýkající se dotčeného souboru/symbolu → kandidát na ekvivalent upstream fix commitu; obsahová shoda hunku → `fix confidence: medium`; bez `patch_dir`: detekce `dirty-worktree`/`internal-fork` → `unknown` s návodem |
| Verze vs. fixed_versions | string/SemVer-ish porovnání; nejednoznačné → `unknown`; u `upstream-ancestor` se porovnává `security_match_version`, ne interní verze |

Výstup: strukturovaný evidence bundle (JSON) + **návrh** stavu:

```text
CVE-2024-28956 @ mbedtls 2.28.8 (artifact a1b2c3…)
  component retained:            YES (12 objektů, 42 816 B)
  affected file compiled:        NO  (ssl_srv.c není v compile_commands)
  required macro MBEDTLS_SSL_SRV_C: NO (není v aktivních makrech)
  symbol present:                unknown (soubor nekompilován)
  call path:                     n/a
  fixed in:                      2.28.9, 3.6.0
  → NÁVRH: not_affected (důvod: vulnerable-code-not-present)
  ⚠ Návrh je podklad pro člověka, nikoli rozhodnutí.
```

Návrhová heuristika je **konzervativní**: návrh `not_affected` jen když všechny kontroly kódu jsou `NO` (žádné `unknown`); jinak `under_investigation`. `affected` se nikdy nenavrhne automaticky. Návrh `fixed` jen když patch-aware kontrola najde ekvivalent upstream fix commitu s `fix confidence ≥ medium` a verze komponenty je pod fixed_versions — i tak vyžaduje lidský approve (doporučení §26.6).

CLI: `fw-context vuln analyze <CVE> [--files …] [--symbols …] [--macros …] [--json]` → vypíše bundle; `--save-hints` doplní hints do `components.toml` (jediný write do registry mimo `sbom init`, potvrzeno uživatelem).

**CRA Art. 13(6) — reportování upstreamu:** když je zranitelnost v komponentě (vč. open-source), výrobce ji musí nahlásit osobě/entitě, která komponentu vyvíjí/spravuje, a adresovat/opravit ji; při vytvoření modifikace musí sdílet kód/dokumentaci (kde vhodné v machine-readable formátu). `vuln analyze` pro komponenty s `[components.upstream] repository` vygeneruje součást bundle: upravený draft upstream reportu (CVE, dotčené soubory/symboly, lokální patch `patchset_sha256`, navržená oprava) — hlášení zůstává lidským krokem.

### 7.2 VEX — `vex/` (PR D2)

#### 7.2.0 Proces schvalování — role, workflow, SLA

VEX approval je **vícestupňový proces s lidským rozhodnutím v každém kroku**. fw-context automatizuje evidenci, návrhy a audit trail — rozhodnutí zůstává na člověku.

**Role:**

| Role | Odpovědnost | Typická osoba |
|---|---|---|
| **Analyst** | Spouští `vuln scan`, kontroluje findings, spouští `vuln analyze` pro build-aware evidenci | Security engineer, embedded developer |
| **Reviewer** | Navrhuje VEX stav (`vex propose`) na základě evidence bundlu | Security lead, tech lead |
| **Approver** | Finálně schvaluje (`vex approve`) — nese právní odpovědnost za VEX výrok | CISO, product owner, compliance officer |
| **Reporter** | Reportuje schválený VEX do ENISA SRP (mimo fw-context, manuální krok) | Compliance officer |

Role nejsou vynuceny nástrojem (žádné RBAC) — jsou konvencí podpořenou `--by` polem. Organizace si definuje vlastní policy.

**Workflow (end-to-end):**

```
┌─ Fáze C (automatická) ──────────────────────────────────────────┐
│                                                                    │
│  1. fw-context vuln scan                                         │
│     → Grype/OSV scan → raw report archivován                     │
│     → findings normalizovány → security.db                       │
│                                                                    │
│  2. fw-context vuln list --severity critical,high                │
│     → Analyst kontroluje nové findings                           │
│     → Triáž: false positive? low confidence? relevant?           │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
                              ↓
┌─ Fáze D (analytik + reviewer) ────────────────────────────────────┐
│                                                                    │
│  3. fw-context vuln analyze CVE-2024-28956                       │
│     → build-aware evidence bundle (kompilováno? linkováno?        │
│       symbol? makro? call path? patch? verze vs fixed_versions)   │
│     → NÁVRH stavu (konzervativní):                                │
│       • všechny kontroly NO → navrhuje not_affected               │
│       • jakékoli unknown → navrhuje under_investigation           │
│       • affected se nikdy nenavrhuje automaticky                  │
│                                                                    │
│  4. Reviewer posoudí evidence bundle:                             │
│     ├─ Přijme návrh? → vex propose                                │
│     ├─ Nesouhlasí? → upraví state + justification, pak propose    │
│     └─ Nedostatek dat? → ponechá under_investigation              │
│                                                                    │
│  5. fw-context vex propose CVE-2024-28956 \                      │
│       --state not_affected \                                      │
│       --justification vulnerable_code_not_present \               │
│       --detail "ssl_srv.c není v buildu, MBEDTLS_SSL_SRV_C=0" \  │
│       --evidence evidence-CVE-2024-28956.json                     │
│     → lifecycle: unreviewed → proposed                            │
│     → zapsáno do security.db (append-only)                       │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
                              ↓
┌─ Schvalování (approver) ──────────────────────────────────────────┐
│                                                                    │
│  6. Approver kontroluje:                                          │
│     • justification je dostatečně konkrétní                       │
│     • evidence bundle je přiložen                                 │
│     • artifact_sha256 odpovídá release artefaktu                  │
│     • SLA: CRA vyžaduje reporting do 14 dnů od zjištění fixu      │
│                                                                    │
│  7a. fw-context vex approve CVE-2024-28956 --by "j.novak"        │
│      → proposed → approved                                        │
│      → timestamp schválení, approver, audit záznam                │
│                                                                    │
│  7b. NEBO fw-context vex reject CVE-2024-28956 \                 │
│        --by "j.novak" --reason "nedostatečná evidence"            │
│      → proposed → rejected (vrací se k analýze)                  │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
                              ↓
┌─ Export + reporting (výstup fw-context → SRP je manuální) ──────────┐
│                                                                    │
│  8. fw-context vex export --output release/firmware.vex.cdx.json │
│     → CycloneDX VEX dokument s approved assessments               │
│     → self-contained, bom-refs sedí na SBOM                       │
│                                                                    │
│  9. fw-context vuln report-srp CVE-2024-28956 \                   │
│       --stage early-warning|notification|final                     │
│     → předvyplněný draft SRP notifikace (24h/72h/final template):  │
│       product + CRA class/category, CVE ID, EUVD ID, severity,     │
│       corrective measures, Member States                           │
│                                                                    │
│ 10. Reporter (člověk):                                             │
│     • VEX dokument + SBOM → podklad pro technickou dokumentaci    │
│     • Pro actively exploited vulnerabilites (24h/72h/14d):         │
│       → vloží draft z krok 9 do ENISA SRP (11. 9. 2026 v provozu)  │
│     • ENISA SRP neposkytuje API (potvrzeno) — webové rozhraní      │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

**Batch operace:**

```text
fw-context vex propose-batch --from-vuln-gate
    # Pro všechny CVEs, které prošly vuln gate s návrhem not_affected,
    # vytvoří hromadný proposed záznam. Vyžaduje --by a global justification.
    # Použití: release pipeline — schválit všechny "bezpečné" CVEs najednou.

fw-context vex approve-batch --all-proposed --by "j.novak"
    # Schválí všechny proposed assessments pro aktuální artifact.
    # Vyžaduje explicitní potvrzení (--confirm).
```

**SLA tracking — `vex status` rozšíření:**

```text
fw-context vex status --sla
    CVE                     State        SLA due        Overdue
    CVE-2024-28956         approved      2026-09-15     -
    CVE-2024-49138         proposed      2026-09-20     -
    CVE-2024-12345         under_inv.    2026-08-10     25d ⚠
```

SLA se počítá od data first scan findingu (pro `under_investigation`) nebo od data `vex propose` (pro `proposed`). Lhůty:
- `under_investigation` → max 30 dní (config `max_under_investigation_days`)
- `proposed` → max 14 dní (CRA Annex I Part II — reasonable time)
- Překročení → warning v `vex status`, FAIL v `--release`/`vuln gate`

**Audit trail:**

Každá změna stavu je append-only záznam v `security.db`:

```sql
-- Každý VEX assessment nese kompletní historii
assessment_id | artifact_sha256 | cve | component_id | state | lifecycle |
justification | detail | evidence_bundle_sha256 | proposed_by | proposed_at |
approved_by | approved_at | superseded_from | sla_due_date
```

Historie je plně rekonstruovatelná — pro jakýkoli artifact lze zpětně dohledat, kdo co kdy schválil a na základě čeho. To je klíčové pro CRA audit (Annex VII vyžaduje dokumentaci vulnerability handling procesu).

#### 7.2.1 Datový model a CLI

`vex/model.py` — stavy a lifecycle (přechody vynucené v kódu, unit-testy). **Interní slovník je NTIA/OpenVEX** (stabilní napříč exporty); CDX export z něj mapuje:

```text
lifecycle: unreviewed → proposed → approved | rejected → (nový artifact) superseded
state (interní):     affected | not_affected | fixed | under_investigation

Mapování state → CDX vulnerabilities[].analysis.state (enum impactAnalysisState):
  affected            → exploitable
  not_affected        → not_affected
  fixed               → resolved
  under_investigation → in_triage

Mapování justification → CDX analysis.justification (enum impactAnalysisJustification):
  vulnerable_code_not_present          → code_not_present
  vulnerable_code_not_in_execute_path  → code_not_reachable
  requires_configuration               → requires_configuration   (shodné)
  (ostatní CDX hodnoty: requires_dependency, requires_environment,
   protected_by_compiler, protected_at_runtime, protected_at_perimeter,
   protected_by_mitigating_control — interně se užívají přímo CDX názvy)
```

Interní hodnoty se ukládají do `security.db`; mapování probíhá výhradně v `vex/export.py` — jinak by export produkoval CDX neprocházející schema validací.

`vex/store.py` — CRUD nad `vulnerability_assessments` (DDL z §6.1). Pravidla:

- `approved` vyžaduje: `artifact_sha256` (musí existovat v `release_artifacts`), `justification` (pro not_affected povinná — interní slovník dle mapovací tabulky výše; export mapuje na CDX justification enum), `detail`, `approver`, `approved_at`.
- `proposed` zapisuje: `proposed_by`, `proposed_at`, `evidence_bundle_sha256` (odkaz na `vuln analyze` výstup).
- `sla_due_date` se automaticky inicializuje: pro `under_investigation` = `created_at + max_under_investigation_days`; pro `proposed` = `proposed_at + 14 dní` (CRA Annex I Part II). Překročení SLA → warning v `vex status --sla`.
- Nový `artifact_sha256` → dosavadní assessments zůstávají, ale `vex status` je ukáže jako `stale` (out-of-scope artifact) — nikdy se tiché nepřenášejí; přenos = explicitní `vex copy --from <sha>` s označením `proposed` a novým `sla_due_date`.
- PK je autoincrement `assessment_id`; každý update vloží nový řádek se stejným (artifact, CVE, component) klíčem, předchozí přepne do `superseded` s `superseded_from` odkazem — historie je append-only, plně auditovatelná.

CLI — `cli/_vex.py`:

```text
fw-context vex propose <CVE> --state not_affected --justification vulnerable_code_not_present \
     --detail "…" --evidence evidence.json [--artifact-sha PREFIX] [--by "…"]
     # zapíše lifecycle=proposed, proposed_by, proposed_at, evidence_bundle_sha256;
     # artifact: default z posledního scanu daného CVE,
     # více kandidátů → FAIL se seznamem (vazba na artifact je povinná, §6.1 FK)
fw-context vex propose-batch --from-vuln-gate --by "…"
     # hromadný propose pro všechny CVEs s návrhem not_affected z vuln gate
fw-context vex approve <CVE> --by "j.novak"      # proposed → approved (výzva k potvrzení artifact sha)
fw-context vex approve-batch --all-proposed --by "j.novak" [--confirm]
     # schválí všechny proposed pro aktuální artifact
fw-context vex reject <CVE> --by "…" --reason "…"
fw-context vex status [--artifact-sha PREFIX] [--sla]
     # matice CVE × lifecycle, stale označeny; --sla zobrazí termíny a překročení
fw-context vex export --output release/firmware.vex.cdx.json
fw-context vex export-yaml --output assessments.yaml   # reviewovatelný snapshot vč. SLA
```

`vex/export.py` — CycloneDX VEX: `vulnerabilities[]` s `id`, `ratings` (z findingu), `affects` = bom-ref komponenty, `analysis.state/justification/detail`, `analysis.responses`. Dokument je self-contained (nevstřebává celý SBOM), `bomFormat: CycloneDX`, spec dle configu.

### 7.3 MCP nástroje + `sbom diff` (PR D3)

Nový handler `mcp/handlers/sbom.py` dle vzoru existujících handlerů (`BaseHandler`, `shared/context.py`; read-only, stejný stale-recovery mechanismus). Nástroje:

```text
get_sbom_summary            # root, počty komponent, completeness, artifact sha, poslední scan
list_firmware_components    # id, name, version, evidence level, parent
get_component_evidence      # detailní evidence jedné komponenty
find_component_files        # component_id → source/object paths
list_unmapped_build_inputs  # unmapped sources + linker inputs
compare_sbom_releases       # wrapper nad sbom/diff.py
list_vulnerabilities        # filtry: severity, komponenta, low-confidence
get_vulnerability           # finding + poslední assessment
analyze_vulnerability_context  # D1 bundle (read-only, hints jen čte)
get_vex_assessment          # stav + historie
```

Zdroje dat: poslední `generate` artefakty (evidence report JSON) + `security.db`. MCP **nikdy** nespouští scan ani generate. Žádný write nástroj v D (ani `propose_vex_assessment` — odloženo, viz §9).

`sbom/diff.py` + `fw-context sbom diff old.cdx.json new.cdx.json`: added/removed components, version changes, evidence level changes, artifact sha change, config_hash change; text + `--json`.

### 7.4 Testy Fáze D

- `test_applicability.py`: všechny kontroly proti fixture index.db + manifest; konzervativita návrhu (unknown → under_investigation); hints z TOML.
- `test_vex.py`: stavový automat (zakázané přechody), approve bez justification → error, stale při novém artefaktu, `vex copy` → proposed.
- `test_vex_export.py`: CDX VEX struktura, affects bom-ref shoda se SBOM.
- `test_diff.py`: added/removed/version/evidence změny.
- MCP handler: existující testovací vzor handlerů (mock DB) — `test_handler_sbom.py`.

### 7.5 PR rozpad Fáze D

| PR | Obsah | Závislost |
|---|---|---|
| **D1** | `applicability.py`, hints v registry, `vuln analyze` + testy | C2 (+B pro linker kontroly; bez B → `unknown`) |
| **D2** | `vex/` (model, store, export), `cli/_vex.py` + testy | C1 (tabulky), D1 |
| **D3** | `mcp/handlers/sbom.py`, `sbom/diff.py`, `sbom diff` + testy, docs | D2 |
| **D4** | `vuln report-srp` (SRP draft 24h/72h/final z findings+VEX+SBOM; `euvd_id`), Art. 13(6) upstream report draft, testy | D2 (VEX), C1 (`euvd_id`) |

### 7.6 Acceptance criteria Fáze D

1. `vuln analyze` doloží každou kontrolu evidence refem; `unknown` je explicitní, ne tiché.
2. Žádná cesta v kódu nevede k `approved` bez `--by` a justification.
3. VEX export je validní CycloneDX a `affects` sedí na bom-refs SBOM.
4. Nový release artifact → staré approvals viditelně `stale`, nikdy tiché přenesené.
5. `vuln gate` (rozšíření z Fáze C) selže, když kritické CVE nemá rozhodnutí a vlastníka, nebo je překročen termín `under_investigation` (cookbook §17.2).
6. MCP nástroje read-only; index použitý pro applicability je stejný jako pro ostatní fw-context dotazy.
7. `vex status --sla` zobrazí termíny a překročení; `vex propose` automaticky nastaví `sla_due_date`.
8. `vex propose-batch` a `vex approve-batch` fungují s potvrzením; bez `--confirm` → dry-run výpis.
9. Každá změna stavu vytvoří nový append-only záznam; `superseded_from` odkazuje na předchozí; historie je plně rekonstruovatelná.
10. `vuln report-srp` (D4) generuje draft 24h/72h/final s poli SRP template (product, CRA class/category, CVE ID, EUVD ID, severity, corrective measures); `euvd_id` je uložen ve findings a zobrazen v `vuln show`.
11. Art. 13(6): `vuln analyze` u komponenty s `[components.upstream] repository` zahrne draft upstream reportu (CVE, dotčené soubory, `patchset_sha256`); nic se neodesílá automaticky.

---

## 8. Fáze E — framework adaptéry, multi-image, provenance

**Cíl:** plná hodnota pro reálné produkty — více image (bootloader, network core), framework metadata (Zephyr/PIO/ESP-IDF), binární bloby, build provenance a podepisování.

### 8.1 Adaptéry (PR E1)

Všechny implementují `SbomSupport` (§5.3) + rozšíření o `collect_package_metadata()`:

```python
def collect_package_metadata(self, project_root: Path, cfg: Config) -> list[PackageCandidate]: ...
# PackageCandidate: name, version, revision, source_url, purl_hint, paths, origin ("west"|"pio-pkg"|"idf")
```

**`zephyr.py`:**

- `west list -f '{name} {revision} {url}'` (subprocess, stabilní formátovaný výstup — žádné parsování YAML, žádná nová závislost) → moduly + revize → `PackageCandidate`s. Frozen manifest se archivuje jako soubor, neparše se.
- `.config` (z build dir): `CONFIG_MBEDTLS=y` apod. → aktivace/deaktivace navržených komponentních pravidel.
- `suggest_component_rules()`: zephyr modules s vlastním upstreamem (mbedtls, littlefs, mcuboot, tinycrypt…).
- `west spdx` output (`build/*/spdx/*.jsonld`), pokud existuje → **evidence input**: file-level potvrzení `compiled` pro moduly; identity zůstává na fw-context registry (west SPDX je provenance, ne autorita verzí).

**`platformio.py`:**

- vyžaduje `BuildConfig.environment` (§5.3); více env bez výběru → FAIL se seznamem.
- `pio pkg list -e <env> --json` → packages (framework, toolchain, libdeps) s verzemi; `.pio/libdeps/<env>/*/library.json` → `source_url`, verze.
- artefakty: `.pio/build/<env>/firmware.{elf,bin}` + map.
- `suggest_component_rules()`: framework dekompozice (framework-arduinocespressif32 → FreeRTOS/lwIP/mbedtls podle přítomnosti v compile_commands).

**`esp_idf.py`:**

- `project_description.json` z build dir (komponenty, cesty, verze IDF); `idf_component.yml` manažer závislosti; `sdkconfig` pro aktivní moduly.

Společné: adaptéry jen **navrhují** pravidla/metadata; finální autorita = `components.toml` (`sbom init` návrhy zapíše jako komentované sekce k odkomentování, `sbom check` hlásí `suggested-not-confirmed`).

### 8.2 Multi-image + bloby (PR E2)

- `SbomGraph` rozšíření: root = **product**; každý image = komponenta `type: firmware` s vlastním `FirmwareArtifact` a vlastním podstromem komponent. Evidence se počítá per-image (vlastní manifest/map/ELF).
- Zephyr sysbuild: `discover_release_artifacts()` vrátí N artefaktů (app, mcuboot, network-core) → `fw-context sbom generate --all-images` vygeneruje jeden product CDX (nebo N souborů + product BOM, dle `[sbom] multi_image_output = "merged"|"split"`, default merged).
- Bloby: registry `blob_path` (§4.2) → SHA-256 blobu, evidence `deployed`, EvidenceRef `kind: "blob"`. Blob bez souboru na disku → FAIL v `--release`.
- Mbed multi-image: není sysbuild; více ELF = uživatel vybere roli per `[sbom.artifact]` nebo opakovaný generate.

### 8.3 Provenance + signing (PR E3)

- CDX 1.7 `formulation`: build commands digest, toolchain (name+version z `[sbom.toolchain]` nebo auto `arm-none-eabi-gcc -dumpversion`), `config_hash`, `sbom_input_hash`.
- `SHA256SUMS` generátor: `fw-context sbom checksums --output release/SHA256SUMS` nad [sbom].output + artefakty + evidence report.
- Signing **externě orchestrávané** (cosign/gpg) — fw-context nepodepisuje; `sbom check --release` jen ověří přítomnost `*.sig`/`*.att` pokud `[sbom.release] require_signature = true`. Derivační řetězec unsigned → signed → encrypted se modeluje přes `FirmwareArtifact.signed`/`derived_from` (§4.1, cookbook §16.7); gate ověří, že podepsané/šifrované artefakty mají vyplněného předka a hash.
- in-toto/SLSA: export `release-attestation.json` šablony (subject = SHA256SUMS položky) k naplnění CI; žádná kryptografie uvnitř fw-context.

### 8.4 Testy Fáze E

- Fixtures: `west-list.txt`, `.config` výstřižek, `pio-pkg-list.json`, `project_description.json`, `spdx/*.jsonld` zmenšeniny.
- Adaptéry: detekce, návrhy pravidel, více env → FAIL; west spdx jako evidence (ne identita).
- Multi-image: merged CDX má product root + 2 firmware subřady; per-image evidence.
- Blob: chybějící blob soubor → FAIL v release; hash v evidence reportu.
- Checksums: deterministický obsah, všechny cesty existují.

### 8.5 PR rozpad Fáze E

| PR | Obsah | Závislost |
|---|---|---|
| **E1** | `adapters/{zephyr,platformio,esp_idf}.py` + `collect_package_metadata` + testy | B2 |
| **E2** | multi-image `SbomGraph`, sysbuild discovery, bloby + testy | E1 (zephyr) |
| **E3** | formulation, SHA256SUMS, attestation šablona, release signature gate + testy | E2 |
| **E4** | SPDX exporter (`sbom/exporters/spdx.py`, future consideration — ISO 5962:2021; tenká vrstva nad kanonickým modelem; nutno vyhodnotit mapping SbomGraph→SPDX 3.x profily) | E3 |

### 8.6 Acceptance criteria Fáze E

1. Zephyr projekt: SBOM obsahuje moduly z frozen manifestu se správnými revizemi; sysbuild → více image v jednom product BOM.
2. PlatformIO: bez `environment` u multi-env projektu FAIL; s env kompletní package metadata.
3. Blob má SHA-256 a evidence `deployed`.
4. `SHA256SUMS` + CDX formulation umožní CI attestaci bez dalších změn fw-context.
5. Ověřeno na **Z-Box ECB** (Mbed, nested komponenty, blob modem firmware) — pozor: indexing trvá hodiny, test pouze sbom vrstvy nad existujícím indexem. Z-Box je zároveň referenční projekt pro vendored provenance: mbed-os 6.17.0 vendored s lokálním commitem → Případ B (`internal-fork` + `[components.upstream]`); `lib/zcbor`, `lib/fat/ChaN`, `lib/tdbstore` vendored bez manifestu → Případ C (`hash-fallback` + `identity_confidence: low` + kandidát pro `identify-component`); `bootloader/P_ECB_BOARD_BL.bin` → `deployed_always` blob; `keys/` obsahuje RSA privátní klíče v repu → secrets denylist (§4.6) musí export zachytit.
6. SPDX exporter (E4, future consideration): mapping `SbomGraph`→SPDX 3.x zdokumentován; minimálně SBOM core + security profily; výstup validován `spdx-java-tools`.

---

## 9. Uzavřená rozhodnutí

| Otázka | Rozhodnutí |
|---|---|
| security.db vs index.db | samostatně (Fáze C); Fáze A–B bez DB |
| CDX verze | 1.7 default + 1.6 export |
| components.toml vs config sekce | samostatný soubor (reviewovatelnost) |
| `sbom generate` spouští build? | **Nikdy.** Čte existující index + artefakt |
| `fail_unmapped` default | `check`/`generate` = warning; `--release`/`--strict` = error (exit 2) |
| Source paths veřejně? | Ne — jen interní evidence report |
| VEX assessment DB vs YAML | DB je zdroj pravdy + YAML export pro review |
| LTO | conservative: confidence ↓, composition max `incomplete` |
| `west spdx` | evidence input, ne autorita |
| Vendored bez gitu | `hash-fallback` verze |
| Artefakt discovery | Fáze A explicitní config; Fáze B+ adaptéry |
| MCP write (propose VEX) | **Odloženo mimo plán** — VEX mutace pouze přes CLI |
| CVE hints z advisories | člověk do `components.toml`; žádné auto-parsování |
| Podepisování SBOM | externí nástroje; fw-context jen gate + checksums |
| Header-only komponenty | evidence `compiled` z include graphu (cookbook §16.2); `include_headers=false` řídí jen detail exportu |
| EOL/patch metadata | `[components.security]` v registry; internal-fork s čistou upstream verzí → FAIL (cookbook §10.7) |
| SPDX export | mimo scope — jediný export je CycloneDX; `west spdx` slouží jen jako evidence input (Zephyr) |
| Online enrichment | `sbom enrich --online` (Fáze C), opt-in; default offline |
| Pořadí fází | cookbook §13.18 vuln-scan fázi nemá (scan je concern §14) a framework adaptéry řadí do své fáze C; plán adaptéry přesouvá do E a vkládá novou fázi C (vuln scan) — scanner přináší hodnotu dřív a nezávisí na framework metadatech |
| Vulnerability gate | `vuln gate` (C: scanner metadata + confidence; D: assessment pravidla) dle cookbook §17.2 |
| CPE politika | compliance SBOM = pouze explicitní CPE z registry (cookbook §2.4); scanner projection = CPE povinné pro CVE-matchované komponenty z `[components.upstream] cpe` (Grype `pkg:generic` bez CPE nematchuje); žádná automatická derivace CPE |
| Duální identita (interní vs upstream) | compliance SBOM = skutečný interní fork + derived-from properties; scanner projection = upstream `security_match_*` bez interního suffixu (doporučení §26.4) |
| Komponenta neznámého původu (Případ C) | minimální identita = `source_tree_sha256` + `identity_confidence: low` + `security_match_mode: heuristic`; release policy §26.8 |
| Rekonstrukce provenance | `sbom identify-component` (C3) — vždy jen návrh, ruční schválení; nikdy auto-write do registry |
| OSV API | doplňkový scanner (C3) pro commit matching u vendored snapshotů/forků — přímo přes OSV HTTP API `/v1/querybatch` (osv-scanner CLI manifest (repo, commit) nepodporuje); Grype zůstává primární |
| CVE Binary Tool | jen heuristická pomůcka pro `identify-component`; nikdy autoritativní zdroj identity (doporučení §26.5) |
| Interní fork bez upstream identity | FAIL v `--release` (`fork-without-upstream`) — bez `[components.upstream]` není oddělena compliance a security identita |
| ENISA SRP reporting | SRP v provozu od 11. 9. 2026, **bez API** (potvrzeno ENISA FAQ Q15). Odeslání je manuální; **fw-context generuje předvyplněný draft** (`vuln report-srp`, 24h/72h/final template vč. CVE/EUVD ID, CRA class/category). API integrace = future consideration, pokud ENISA API zpřístupní |
| EUVD ID | `vulnerability_findings.euvd_id` (additive migrace, Fáze C) — European Vulnerability Database, SRP template pole v14 |
| SPDX export | future consideration (Fáze E) — aktuálně jen CycloneDX; SPDX ISO 5962:2021 může být vyžadován pro některé sektory (automotive, medical); exporter je tenká vrstva nad kanonickým modelem, nevyžaduje změnu architektury; `west spdx` slouží jako evidence input (Zephyr) |
| CRA produktová klasifikace | `[product] cra_class/cra_category` (Reg. 2025/2392) — core-functionality test je **lidské rozhodnutí**, nikoli automatická derivace; conformity route derivuje resolver |
| CRA support period min. 5 let | `support_end_date ≥ placed_on_market_date + 5 let`; kratší jen s `support_shorter_than_5y_reason` (Annex VII(4)) — Art. 13(8) |
| SBOM jako povinnost | CRA Annex I Part II(1) **explicitně vyžaduje** SBOM v machine-readable formátu ≥ top-level dependencies — plán z toho vychází jako z povinného výstupu, ne best-practice |
| CISA 2026 ME v2.1 | SBOM Author Signature, SBOM Version (SemVer), tool name/version, generation context, komponentní hashe, licence, `producer` — exportér + `sbom check --release` ME check |
| PLD (Dir. 2024/2853) | aplikace od 9. 12. 2026; evidence report + `sbom diff` + VEX audit = liability trail (verze/update historie) |
| Battery DPP | priorita Fáze G: **18. 2. 2027** (Battery Reg. 2023/1542) před ESPR elektronikou (2029) |
| RED→CRA přechod | pro radio-enabled zařízení (WiFi/BLE/LoRa) platí překryv RED Art. 3(3)(d-f)→CRA; **Del. Reg. (EU) 2026/339 ruší (EU) 2022/30 od 11. 12. 2027**; fw-context SBOM kompatibilní s oběma režimy — žádná změna architektury; testovací projekty HA_Boiler (ESP32/WiFi), FM (STM32), Z-Box (nRF52/BLE) jsou radio equipment |

---

## 10. Rizika (nejvyšší → mitigace)

1. **Nesprávná identita komponenty** → explicitní registry jako autorita, `version_source` v exportu, CPE pouze explicitní z registry (compliance i projection — žádná derivace), PURL jen přes `purl_template`.
2. **Falešné `complete`** → Fáze A hardcoded `incomplete`; `complete` jen při 100 % mapování linker vstupů (Fáze B).
3. **Variabilita linker map** → fixture corpus, `PARSER_VERSION`, konzervativní fallback, >50 % parse failure → celý fallback.
4. **False positives scanneru** → `match_type` confidence, low-confidence označení, applicability evidence, VEX.
5. **Automatické `not_affected`** → návrh je konzervativní heuristika; approve vyžaduje člověka + artifact scope + audit trail.
6. **Scope creep** → PR rozpad; každá fáze má vlastní acceptance criteria a neblokuje předchozí.
7. **Rozbití stávajícího indexu** → Fáze A–B nesahají na `indexer/` mimo čtení manifestu; regrese hlídaná `make test`.
8. **Ztráta provenance u vendored komponent** (Z-Box ECB: mbed-os fork, `lib/zcbor`, `lib/fat/ChaN` bez upstream vazby) → `source_tree_sha256` jako minimální stabilní identita, `identify-component`, release policy FAIL, `heuristic` komponenty mimo scanner projection.
9. **Scanner matchne interní verzi s suffixem špatně** → scanner projection používá výhradně `security_match_*`; validace zakazuje interní suffix v projekci.
10. **Falešné `fixed` z patch detekce** → `fix confidence` capped na medium, návrh `fixed` vždy vyžaduje lidský approve s evidence bundle.

---

## 11. Celkový roadmap přehled

| Fáze | Výstup | PR | Závislosti |
|---|---|---|
| A | SBOM z aktivního buildu, CDX export (CRA Annex I II(1) povinnost, CRA klasifikace, support period 5 let, CISA 2026 ME metadata) | A1, A2, A3 | — |
| B | `linked`/`retained`, GNU map parser, adaptéry generic/mbed | B1, B2, B3 | A3 |
| C | Grype scan, security.db (vč. `euvd_id`), normalizace, vuln gate, enrich, OSV + identify-component | C1, C2, C3 | A3 (C1 kdykoli po A3) |
| D | applicability, VEX, **`vuln report-srp` draft (SRP bez API)**, MCP | D1, D2, D3, (D4 report-srp) | C2 (+B pro plné kontroly) |
| E | zephyr/PIO/ESP-IDF adaptéry, multi-image, provenance (signing = CISA ME Author Signature), SPDX export (future) | E1, E2, E3, (E4) | B2 |
| G | DPP exportér (**battery 18. 2. 2027 před ESPR elektronikou 2029**), `sbom export doc` (EU DoC), `sbom export psti-soc`, `sbom export scip` | G1–G3 | D2 |

C a B lze po A3 vést paralelně. D1 bez B funguje degradovaně (linker kontroly `unknown`). **Priorita D vzhledem k termínům:** CRA reporting + SRP startují **11. 9. 2026** — D1+D2 (`vuln analyze`+VEX) a `vuln report-srp` by měly být k dispozici před tímto datem; PLD (9. 12. 2026) je pokryt přirozeně evidence reportem.

## 12. Bezprostřední další krok

Implementovat **PR A1** (`sbom/model.py` + `sbom/registry.py` + testy) dle §4.1–4.2 a §4.10 (vč. rev. 11 polí: `producer`, `cra_class`/`cra_category`, `security_contact`, `eu_responsible_operator`, `psti_route`, `euvd_id` v findings, CISA 2026 ME metadata, `dataset_*`/`ai_security_controls`/`ai_performance_metrics`). Po A3 ověřit end-to-end na HA_Boiler a FM projektech. Prioritizovat D1/D2, aby VEX + `vuln report-srp` byly připravené před 11. 9. 2026.
