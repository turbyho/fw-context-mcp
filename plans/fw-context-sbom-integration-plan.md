# Implementační plán: SBOM a vulnerability analýza pro `fw-context`

**Projekt:** `turbyho/fw-context-mcp`
**Typ dokumentu:** implementační plán (ukotvený do reálné codebase)
**Stav:** všechny fáze detailně specifikovány; připraveno k implementaci Fáze A
**Datum:** 2026-08-03 (revize 3 — zapracované nálezy review vůči cookbook: prefix-glob matching, blob hash v A, header-only evidence, EOL/fork metadata, vuln gate, artifact binding, drobné odchylky)
**Východiskový dokument:** `plans/embedded-sbom-cookbook.md` (normativní reference — framework recepty, formáty, release gates; tento plán jej neduplikuje)

---

## 1. Rozsah a cíle

Rozšířit `fw-context` o build-aware SBOM:

1. identifikace komponent z **aktivního buildu** (ne scan zdrojového stromu);
2. export CycloneDX JSON navázaný na konkrétní ELF/BIN přes SHA-256;
3. linkerová evidence (`linked`/`retained`) z GNU ld map a ELF;
4. externí vulnerability scanner (Grype) jako volitelný subprocess;
5. build-aware evidence pro posouzení aplikovatelnosti CVE;
6. člověkem schvalovaný VEX;
7. framework adaptéry (Mbed OS, Zephyr, PlatformIO, ESP-IDF) a release provenance.

Klíčová diferenciace: `declared → present → compiled → linked → retained → deployed`.
`fw-context` zná `compile_commands.json`, aktivní TUs, makra, include graph a `config_hash` — SBOM je další vrstva nad tímto indexem, nikoli obecný directory scanner.

---

## 2. Korekce ideového plánu vůči reálné codebase

| Ideový plán | Reálný stav | Rozhodnutí |
|---|---|---|
| Fáze 0: „přesunout subcommands mimo rostoucí `cli.py`" | CLI je **již modularizované**: `cli/__init__.py:main()` registruje subcommandy z modulů `cli/_index.py`, `_export.py`, `_db.py` … (vzor: `cmd_*(args) → int`) | **Fáze 0 se ruší.** Nové příkazy = nové moduly `cli/_sbom.py`, `_vuln.py`, `_vex.py` |
| Rozšířit `BuildSystem` protokol o 3 SBOM metody | Protokol v `indexer/builders/protocol.py` implementuje **11 builderů**; povinné metody by všechny rozbily | **Nový opt-in protokol** `sbom/adapters/base.py:SbomSupport`; buildere se nedotýká |
| Přidat `files.component_id` do index.db | Znamenalo by schema migraci + reindex | **Fáze A–B bez DB změn.** SBOM je čistá funkce vstupů; tabulky až `security.db` (Fáze C) |
| `BuildConfig` doplnit PlatformIO `environment` | `build.py:BuildConfig` (slots dataclass) pole nemá | Fáze B: přidání pole je additive change |
| Automatická detekce ELF/map artefaktů | Více image = nejednoznačnost (Mbed `BUILD/**`) | **Fáze A vyžaduje explicitní `[sbom.artifact]`**; auto-discovery přes adaptéry ve Fázi B |
| `sbom init` generuje registry z detekce | Git root + vendor paths známe | MVP: konzervativní šablona (projekt + vendor rooty z `index.vendor_paths`); framework návrhy až adaptéry |
| MCP nástroje | Další povrch k testování | **Fáze A–C CLI-only.** MCP read-only nástroje Fáze D |

### Co platí beze změny

- Evidence ladder a povinnost deklarovat neúplnost.
- Interní kanonický model ≠ CycloneDX (exporter je tenká vrstva).
- Offline-by-default; online enrichment pouze explicitně.
- Scanner jako volitelný executable, ne Python dependency.
- `not_affected` vždy vyžaduje člověka; `fw-context` navrhuje a dokládá.
- `security.db` odděleně od `index.db`.
- Root identity přes `sbom_input_hash`, ne jen `config_hash`.

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
│   ├── validation.py          # A3: interní kontroly výstupu
│   ├── diff.py                # D3: porovnání dvou CDX dokumentů
│   ├── exporters/
│   │   ├── __init__.py
│   │   └── cyclonedx.py       # A3: CDX 1.6/1.7
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
    component_type: str         # library|framework|operating-system|application|firmware
    purl: str | None
    cpe: str | None
    source_url: str | None
    licenses: tuple[str, ...]
    parent: str | None          # component_id nadřazené komponenty
    version_source: str         # "git"|"header"|"manual"|"hash-fallback"
    warnings: tuple[str, ...]
    support_status: str = "unknown"        # "supported"|"eol"|"internal-fork"|"unknown" (cookbook §6)
    owner: str | None = None               # interní vlastník komponenty
    internal_version: str | None = None    # interní fork "2.28.8+company.3" (cookbook §16.5)

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
    kind: str                   # "compile-command"|"linker-map"|"registry"|"git"|"version-header"|"blob"
    detail: str                 # např. "manifest.json entry #128", "firmware.map line 8123"

@dataclass(frozen=True, slots=True)
class FirmwareArtifact:
    path: str                   # relativní k project_root
    sha256: str
    artifact_type: str          # "elf"|"bin"|"hex"
    role: str                   # "application" (Fáze A); Fáze E: "bootloader"|"network-core"|…
    signed: bool = False        # podepsaný/šifrovaný image (Fáze E, cookbook §16.6)
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
type = "firmware"

[[components]]
id = "app"
type = "application"
paths = ["src/**", "include/**"]
version_source = "git"
git_root = "."

[[components]]
id = "mbedtls"
name = "Mbed TLS"
type = "library"
supplier = "TrustedFirmware.org"
parent = "mbed-os"
paths = ["mbed-os/connectivity/mbedtls/**"]
version_source = "header"
version_file = "mbed-os/connectivity/mbedtls/include/mbedtls/build_info.h"
version_pattern = 'MBEDTLS_VERSION_STRING\s+"([^"]+)"'
purl_template = "pkg:generic/mbedtls@{version}"

[components.security]               # váže se k předchozí [[components]] (mbedtls)
owner = "embedded-security"
support_status = "supported"
monitoring = ["NVD", "OSV", "upstream advisories"]

[[components]]
id = "modem-fw"
type = "firmware"
version_source = "manual"
version = "EG91EFBR06A08M4G"
deployed_always = true
blob_path = "blobs/eg91.bin"        # Fáze A: blob se hashuje (SHA-256), evidence "deployed"
```

Pravidla (unit-testované):

1. **Validace při load:** duplicitní `id` → error; `parent` neexistuje → error; `version_source` ∈ {git, header, manual, hash-fallback}; `version_file` vyžadován pro `header`; `version` vyžadován pro `manual`; neznámé klíče → warning (forward-compat); neznámé `schema` > 1 → error. `[components.security]` validace: `support_status` ∈ {supported, eol, internal-fork, unknown}; `owner`/`monitoring`/`remediation`/`migration_target` volitelné.
2. **Path matching — prefix-glob, ne `PurePath.match`:** `PurePath.match` je right-anchored (`PurePath("app/src/main.c").match("src/**")` → `True` → falešné vlastnictví). Glob se proto překládá na regex ukotvený na **začátek** relativní cesty (`**` = libovolná hloubka, `*` = jedna úroveň) a matchuje celou cestu zleva. **Nejdelší (nejspecifičtější) matching prefix vyhrává** — vnořená komponenta má přednost; konflikt dvou pravidel stejné specificity → error s oběma pravidly.
3. Jeden soubor = jeden primární vlastník.
4. `deployed_always = true` → komponenta v SBOM i bez aktivních zdrojů, evidence `deployed`. `blob_path` → SHA-256 blobu se počítá **již ve Fázi A** (streaming) a zapisuje se do evidence reportu s `EvidenceRef(kind="blob")`; blob chybí na disku → FAIL v `--release`, jinak warning (cookbook gate §8.8: „FAIL: binary blob nemá SHA-256").

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
4. `hash-fallback` — SHA-256 nad setříděnými `source_hash` vlastněných zdrojů z manifestu (žádné čtení souborů): `legacy+sha256.<prefix16>`.

Komponenta z registry **bez aktivních zdrojů** a bez `deployed_always` → není v grafu; v reportu jako `declared-not-compiled` (info).

**Interní fork a EOL (cookbook §10.6–10.7, §16.5):**

- `support_status = "internal-fork"` → komponenta musí mít `version_source = "manual"` s interní verzí (`+company.N`, uloženou do `internal_version`) nebo `hash-fallback`; čistá upstream verze → **FAIL** („patchovaná komponenta používá pouze upstream verzi").
- `support_status = "eol"` → warning `eol-component` v reportu.
- `support_status` a `owner` se exportují jako CDX properties (`fw-context:support-status`, `fw-context:owner`).

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
Exit code:           0 OK | 1 warnings v --strict | 2 fail_unmapped/errors
```

`sbom explain <id>` — identity source, version source, počet zdrojů/objektů, ukázka evidence refs, warnings.

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
  "build_system": "mbed-os",
  "build_target": "CUSTOM_TARGET",
  "build_profile": "release",
  "sbom_input_hash": "SHA256(config_hash+manifest+registry+artifact)"
}
```

   (`linker_map_sha256` a `parser_version` přibydou ve Fázi B → `_format` se bumpne na `fw-context-sbom-input/2`; pole jsou výhradně přidáváná, reader musí ignorovat neznámá.)
5. Export → `--output`.
6. Interní kontroly (`validation.py`): unikátní bom-refs, dependency refs existují, root má hash, žádná komponenta bez `version`, **secrets denylist** nad serializovaným výstupem (privátní klíče, tokeny, hesla — cookbook §8.12); `--release` ⇒ `fail_unmapped` + clean worktree + žádný warning.

### 4.7 CycloneDX exporter — `sbom/exporters/cyclonedx.py` (PR A3)

- `spec_version` `"1.6"`/`"1.7"`; **default 1.7**; 1.6 = tenký downgrade (1.7-only pole se vynechají).
- `metadata.component` = root (`type: firmware`, `hashes[SHA-256]`, properties `fw-context:config-hash`, `fw-context:evidence-level`, `fw-context:board/target`).
- `components[]`; `bom-ref = "urn:fw-context:<project_id>:<component_id>"` (stabilní přes releasy → diff funguje).
- `dependencies[]`: root → top-level; parent → nested.
- `compositions`: jediná s `aggregate: "incomplete"` (Fáze A hardcoded).
- Properties komponent: `fw-context:evidence`, `fw-context:version-source`, `fw-context:revision`, `fw-context:support-status`, `fw-context:owner`.
- `purl_template` render: s opt-in `packageurl-python` (extra `sbom`, §5.4) se výsledek validuje jako PURL; bez ní jen konzervativní string render + warning `purl-unvalidated`. CPE se nikdy negeneruje.
- Serial `urn:uuid:<uuid4>`, timestamp UTC. **Testy normalizují** (golden jen stabilní podmnožina).
- JSON schema validace jen s extra `sbom-validation` (`jsonschema`); jinak přeskočena s info. CI validuje externě `cyclonedx-cli`.
- `source_paths` neexportovat (`include_source_paths = false`); jdou do interního `--evidence-output` JSON.

### 4.8 CLI — `cli/_sbom.py` (PR A2+A3)

Vzor `cli/_index.py`: `cmd_sbom(args) → int`, registrace v `cli/__init__.py:main()`:

```text
fw-context sbom init      # A2: components.toml šablona (product + git root + vendor rooty);
                          #     existující soubor nikdy nepřepíše
fw-context sbom check     # A2 [--json] [--strict]
fw-context sbom explain <id>   # A2
fw-context sbom generate  # A3 [--output PATH] [--release-version V] [--release] [--evidence-output PATH]
fw-context sbom validate <file>  # A3: interní kontroly §4.6 nad existujícím CDX (bez generate);
                                 #     JSON schema validace s extra sbom-validation, jinak přeskočena
```

### 4.9 Konfigurace — `config/settings.py`

Nový dataclass `SbomConfig` + sekce v `Config`, naplněná stávajícím `_apply_section` mechanismem:

```toml
[sbom]
component_registry = ".fw-context/components.toml"
spec_version = "1.7"
output = "release/firmware.cdx.json"
fail_unmapped = true
include_source_paths = false
include_headers = false   # detail hlaviček v exportu; detekce header-only komponent běží vždy

[sbom.product]
version_command = "git describe --tags --always --dirty"

[sbom.artifact]
elf = "BUILD/CUSTOM_TARGET/GCC_ARM/firmware.elf"
binary = "BUILD/CUSTOM_TARGET/GCC_ARM/firmware.bin"
map = "BUILD/CUSTOM_TARGET/GCC_ARM/firmware.map"
role = "application"
```

`[sbom.artifact]` povinná pro `generate` v Fázi A (jinak FAIL s návodem).

### 4.10 Testy — `tests/test_sbom/`

```text
tests/test_sbom/
├── fixtures/
│   ├── registry_basic.toml / registry_conflict.toml / registry_nested.toml
│   ├── manifest_basic.json        # 6 TUs napříč 3 komponentami
│   ├── version_header.h
│   └── artifact.bin               # pevný obsah → pevný SHA-256
├── test_model.py / test_registry.py / test_collector.py / test_resolver.py
├── test_report.py / test_generate.py / test_cyclonedx.py / test_cli_sbom.py
```

Povinné scénáře: nested precedence; konflikt stejné specificity; prefix-glob negativ (`app/src/main.c` nesmí matchnout `src/**`); chybějící verze → FAIL; hash-fallback bez gitu; unmapped → exit 2 při `fail_unmapped`; změna artefaktu → změna `sbom_input_hash`; deterministický export (2× generate → stejné `components`+`dependencies`); blob hash + chybějící blob → FAIL v `--release`; header-only komponenta doložená include graphem; `[components.security]` validace; internal-fork s čistou upstream verzí → FAIL; secrets denylist → FAIL. Git testy přes `git init` v `tmp_path`.

### 4.11 PR rozpad Fáze A

| PR | Obsah | Závislost |
|---|---|---|
| **A1** | `sbom/model.py`, `sbom/registry.py` + testy. Žádné CLI. | — |
| **A2** | `collector.py`, `resolver.py`, `report.py`, `SbomConfig`, `cli/_sbom.py` (`init`, `check`, `explain`) + testy | A1 |
| **A3** | `exporters/cyclonedx.py`, `generate.py`, `validation.py`, `sbom generate` + testy, docs | A2 |

### 4.12 Acceptance criteria Fáze A

1. Každá aktivní TU má vlastníka nebo je reportována (exit 2 v release režimu).
2. Verze ze 4 zdrojů + zaznamenaný `version_source`.
3. CDX 1.7 i 1.6 projdou `cyclonedx-cli validate`.
4. Root nese SHA-256 artefaktu; `sbom_input_hash` v `sbom-input.json`.
5. `composition: incomplete` — žádné falešné `complete`.
6. Celý pipeline offline.
7. Každý `deployed_always` blob má SHA-256 nebo FAIL v `--release` (cookbook §8.8).
8. Header-only komponenta doložená include graphem je v SBOM s evidence `compiled`.
9. Interní fork s čistou upstream verzí → FAIL; EOL komponenta → warning.
10. Export prošel secrets denylist kontrolou.
11. Žádná změna DB schématu; index a MCP netknuté (`make test` zelené).
12. Ověřeno na **HA_Boiler** a **FM** (read-only).

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
    scanner           TEXT NOT NULL,          -- "grype"
    scanner_version   TEXT NOT NULL,
    db_timestamp      TEXT,                   -- stáří vulnerability DB scanneru
    artifact_sha256   TEXT NOT NULL REFERENCES release_artifacts(artifact_sha256),
    raw_report_path   TEXT NOT NULL,
    config_json       TEXT NOT NULL,          -- scanner flags, policy
    created_at        TEXT NOT NULL
);

CREATE TABLE vulnerability_findings (
    scan_id           INTEGER NOT NULL REFERENCES vulnerability_scans(scan_id),
    vulnerability_id  TEXT NOT NULL,
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
    assessment_id     INTEGER PRIMARY KEY,
    artifact_sha256   TEXT NOT NULL REFERENCES release_artifacts(artifact_sha256),
    vulnerability_id  TEXT NOT NULL,
    component_id      TEXT NOT NULL,
    state             TEXT NOT NULL,          -- affected|not_affected|fixed|under_investigation
    lifecycle         TEXT NOT NULL,          -- unreviewed|proposed|approved|rejected|superseded
    justification     TEXT,
    detail            TEXT,
    evidence_json     TEXT NOT NULL DEFAULT '{}',
    approver          TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
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
    def probe(self) -> ScannerInfo: ...        # executable, verze, DB timestamp; nenalezen → None
    def scan(self, sbom_path: Path, output_path: Path, timeout_seconds: int) -> ScanExecution: ...
    def parse(self, output_path: Path) -> list[VulnerabilityFinding]: ...
```

`vulnerabilities/grype.py`:

- `shutil.which("grype")`; chybí → srozumitelná chyba s instalačním návodem (nikdy auto-install).
- `grype version -o json` → `scanner_version`; `grype db status -o json` → `db_timestamp`; stáří > `[vuln] max_db_age_days` (default 7) → warning.
- Scan: `grype sbom:<path> -o json --file <out>` (syntax přesně dle detekované major verze; při neznámé verzi FAIL s hláškou, ne hádat flagy). Subprocess: list argv, bez shellu, timeout (default 600 s), env passthrough minimal (`HOME`, `PATH`, `GRYPE_DB_*`).
- `ScanExecution` zaznamená: argv, exit code, duration, stderr tail (do logu, ne do DB).

### 6.3 Normalizace — `vulnerabilities/normalize.py` (PR C1)

```python
@dataclass(frozen=True, slots=True)
class VulnerabilityFinding:
    scanner: str
    vulnerability_id: str       # CVE-…, GHSA-…
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
exact-purl > exact-cpe > generated-cpe > name-version > fuzzy > unknown
```

Pravidla:

- `generated-cpe` a níže → finding v reportech viditelně označen `[LOW CONFIDENCE]`.
- Finding, jehož komponentu nelze spárovat zpět na bom-ref z našeho SBOM → `component_id = ""` + warning `unmatched-finding` (nikdy zahodit — může jít o naši chybu v SBOM).
- Duplicitní CVE napříč namespacy → sloučit, namespace zaznamenat.

### 6.4 CLI — `cli/_vuln.py` (PR C2)

```text
fw-context vuln scan [--scanner grype] [--sbom PATH] [--timeout N]
    # default SBOM = [sbom].output; po scanu: release_artifacts + scans + findings insert
fw-context vuln list [--component ID] [--severity LEVEL+] [--artifact-sha PREFIX]
fw-context vuln show <CVE-ID>            # normalizovaný detail + raw JSON (--raw)
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
max_db_age_days = 7
archive_dir = "release/vulnerabilities"   # raw reporty
timeout_seconds = 600

[vuln.gate]
min_severity = "high"                     # findings >= této severity vyžadují assessment
max_under_investigation_days = 30         # Fáze D: termín pro under_investigation
```

Součástí Fáze C je i **`fw-context sbom enrich --online`** (rozšíření `cli/_sbom.py`): explicitní opt-in online krok, který pro komponenty bez PURL/CPE navrhne identifikátory z veřejných zdrojů a vypíše je jako návrhy — nikdy je nezapíše do registry automaticky (cookbook §13.16). Default zůstává plně offline.

### 6.5 Testy Fáze C

- `test_normalize.py`: fixture `grype-report.json` (reálný zmenšený výstup) → findings; match_type odvození; unmatched finding → warning; deduplikace namespaců.
- `test_db.py`: migrace z prázdné DB; CRUD; cizí klíče; dva scany nad stejným artefaktem.
- `test_grype.py`: parse fáze offline z fixture; probe/scan za `pytest.mark.skipif(shutil.which("grype") is None)` (stejný vzor jako existující skipy ollama/libclang).
- `test_cli_vuln.py`: exit kódy, defaulty z configu.

### 6.6 PR rozpad Fáze C

| PR | Obsah | Závislost |
|---|---|---|
| **C1** | `vulnerabilities/db.py` (schema+migrace+CRUD), `normalize.py`, fixtures, testy | B1 (libovolný bod po A3) |
| **C2** | `scanner.py`, `grype.py`, `cli/_vuln.py`, `[vuln]` config, testy, docs | C1 |

### 6.7 Acceptance criteria Fáze C

1. `vuln scan` funguje nad naším CDX 1.7 i 1.6 výstupem; raw report archivován.
2. Každý finding má `match_type`; low-confidence viditelně označen.
3. Findings navázány na `artifact_sha256` (ne `config_hash`).
4. Stará scanner DB → warning; chybějící grype → čistá chyba bez tracebacku.
5. `security.db` nezávislá na `index.db` — reindex nemění vulnerability data.
6. `vuln gate` vrací exit 2 při porušení gate pravidel z §6.4; pravidla odpovídají cookbook §17.2.

---

## 7. Fáze D — applicability evidence + VEX + MCP

**Cíl:** největší přidaná hodnota fw-context — pro konkrétní CVE připravit build-aware důkazy (kompilováno? linkováno? symbol přítomen? makro aktivní? call path? patch?) a workflow pro člověkem schválený VEX.

### 7.1 Applicability — `vulnerabilities/applicability.py` (PR D1)

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
| Lokální patch dotčené funkce | `source_hash` symbolu vs. očekávání? Ne — prakticky: `git log -L`/blame je křehké; MVP: detekce `dirty-worktree` nebo `internal-fork` verze → `unknown` s návodem |
| Verze vs. fixed_versions | string/SemVer-ish porovnání; nejednoznačné → `unknown` |

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

Návrhová heuristika je **konzervativní**: návrh `not_affected` jen když všechny kontroly kódu jsou `NO` (žádné `unknown`); jinak `under_investigation`. `affected` se nikdy nenavrhne automaticky.

CLI: `fw-context vuln analyze <CVE> [--files …] [--symbols …] [--macros …] [--json]` → vypíše bundle; `--save-hints` doplní hints do `components.toml` (jediný write do registry mimo `sbom init`, potvrzeno uživatelem).

### 7.2 VEX — `vex/` (PR D2)

`vex/model.py` — stavy a lifecycle (přechody vynucené v kódu, unit-testy):

```text
lifecycle: unreviewed → proposed → approved | rejected → (nový artifact) superseded
state:     affected | not_affected | fixed | under_investigation
```

`vex/store.py` — CRUD nad `vulnerability_assessments` (DDL z §6.1). Pravidla:

- `approved` vyžaduje: `artifact_sha256` (musí existovat v `release_artifacts`), `justification` (pro not_affected: CDX justification enum — `vulnerable_code_not_present` / `…_not_in_execute_path` / `requires_configuration`…), `detail`, `approver`, timestamp.
- Nový `artifact_sha256` → dosavadní assessments zůstávají, ale `vex status` je ukáže jako `stale` (out-of-scope artifact) — nikdy se tiché nepřenášejí; přenos = explicitní `vex copy --from <sha>` s označením `proposed`.
- PK je autoincrement `assessment_id`; každý update vloží nový řádek se stejným (artifact, CVE, component) klíčem a předchozí přepne do `superseded` — historie je append-only.

CLI — `cli/_vex.py`:

```text
fw-context vex propose <CVE> --state not_affected --justification vulnerable_code_not_present \
     --detail "…" --evidence evidence.json [--artifact-sha PREFIX]
     # zapíše lifecycle=proposed; artifact: default z posledního scanu daného CVE,
     # více kandidátů → FAIL se seznamem (vazba na artifact je povinná, §6.1 FK)
fw-context vex approve <CVE> --by "j.novak"      # proposed → approved (výzva k potvrzení artifact sha)
fw-context vex reject <CVE> --by "…" --reason "…"
fw-context vex status [--artifact-sha PREFIX]    # matice CVE × lifecycle, stale označeny
fw-context vex export --output release/firmware.vex.cdx.json
fw-context vex export-yaml --output assessments.yaml   # reviewovatelný snapshot
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

### 7.6 Acceptance criteria Fáze D

1. `vuln analyze` doloží každou kontrolu evidence refem; `unknown` je explicitní, ne tiché.
2. Žádná cesta v kódu nevede k `approved` bez `--by` a justification.
3. VEX export je validní CycloneDX a `affects` sedí na bom-refs SBOM.
4. Nový release artifact → staré approvals viditelně `stale`, nikdy tiché přenesené.
5. `vuln gate` (rozšíření z Fáze C) selže, když kritické CVE nemá rozhodnutí a vlastníka, nebo je překročen termín `under_investigation` (cookbook §17.2).
6. MCP nástroje read-only; index použitý pro applicability je stejný jako pro ostatní fw-context dotazy.

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
- Signing **externě orchestrávané** (cosign/gpg) — fw-context nepodepisuje; `sbom check --release` jen ověří přítomnost `*.sig`/`*.att` pokud `[sbom.release] require_signature = true`. Derivační řetězec unsigned → signed → encrypted se modeluje přes `FirmwareArtifact.signed`/`derived_from` (§4.1, cookbook §16.6–16.7); gate ověří, že podepsané/šifrované artefakty mají vyplněného předka a hash.
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

### 8.6 Acceptance criteria Fáze E

1. Zephyr projekt: SBOM obsahuje moduly z frozen manifestu se správnými revizemi; sysbuild → více image v jednom product BOM.
2. PlatformIO: bez `environment` u multi-env projektu FAIL; s env kompletní package metadata.
3. Blob má SHA-256 a evidence `deployed`.
4. `SHA256SUMS` + CDX formulation umožní CI attestaci bez dalších změn fw-context.
5. Ověřeno na **Z-Box ECB** (Mbed, nested komponenty, blob modem firmware) — pozor: indexing trvá hodiny, test pouze sbom vrstvy nad existujícím indexem.

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
| Pořadí fází | C (vuln scan) před E (adaptéry) — odchylka od cookbook §13.18: scanner přináší hodnotu dřív a nezávisí na framework metadatech |
| Vulnerability gate | `vuln gate` (C: scanner metadata + confidence; D: assessment pravidla) dle cookbook §17.2 |

---

## 10. Rizika (nejvyšší → mitigace)

1. **Nesprávná identita komponenty** → explicitní registry jako autorita, `version_source` v exportu, zákaz generovaného CPE, PURL jen přes `purl_template`.
2. **Falešné `complete`** → Fáze A hardcoded `incomplete`; `complete` jen při 100 % mapování linker vstupů (Fáze B).
3. **Variabilita linker map** → fixture corpus, `PARSER_VERSION`, konzervativní fallback, >50 % parse failure → celý fallback.
4. **False positives scanneru** → `match_type` confidence, low-confidence označení, applicability evidence, VEX.
5. **Automatické `not_affected`** → návrh je konzervativní heuristika; approve vyžaduje člověka + artifact scope + audit trail.
6. **Scope creep** → PR rozpad; každá fáze má vlastní acceptance criteria a neblokuje předchozí.
7. **Rozbití stávajícího indexu** → Fáze A–B nesahají na `indexer/` mimo čtení manifestu; regrese hlídaná `make test`.

---

## 11. Celkový roadmap přehled

| Fáze | Výstup | PR | Závislosti |
|---|---|---|---|
| A | SBOM z aktivního buildu, CDX export | A1, A2, A3 | — |
| B | `linked`/`retained`, GNU map parser, adaptéry generic/mbed | B1, B2, B3 | A3 |
| C | Grype scan, security.db, normalizace, vuln gate, enrich | C1, C2 | A3 (C1 kdykoli po A3) |
| D | applicability, VEX, MCP | D1, D2, D3 | C2 (+B pro plné kontroly) |
| E | zephyr/PIO/ESP-IDF adaptéry, multi-image, provenance | E1, E2, E3 | B2 |

C a B lze po A3 vést paralelně. D1 bez B funguje degradovaně (linker kontroly `unknown`).

## 12. Bezprostřední další krok

Implementovat **PR A1** (`sbom/model.py` + `sbom/registry.py` + testy) dle §4.1–4.2 a §4.10. Po A3 ověřit end-to-end na HA_Boiler a FM projektech.
